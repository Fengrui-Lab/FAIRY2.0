"""Regression evidence for equipment recovery, resource limits and timed jobs."""
import copy
import json
from dataclasses import replace
from datetime import datetime, time, timedelta

import pytest

from evaluate.scenarios.lib.l3.soybean_full_season import soybean_full_season
from evaluate.scenarios.season import SeasonSpec
from fairy.acps.backends.biology.native.season import NativeSeasonBackend
from fairy.acps.backends.physical.native.backend import NativePhysicalBackend
from fairy.acps.ontology.contracts import OperationRequest, OperationRejected
from fairy.agents.loop.controller.season_oracle import SeasonOracle
from fairy.events.session import Runtime


def invoke(runtime, operation, **arguments):
    return runtime.invoke(OperationRequest(operation, arguments))


def mature_fixture(recovery):
    """A process-unit fixture, deliberately not a production scene initial state."""
    spec=soybean_full_season()
    config=replace(spec.season,harvest_recovery_fraction=recovery)
    biology=NativeSeasonBackend(spec.farm,spec.crop,spec.sowing_date,config)
    physical=NativePhysicalBackend(config,spec.farm)
    for i in (0,1):
        biology.state['ridges'][i].update(planted=True,gdd=config.maturity_gdd,
            grain_dry_g_m2=100.0,dry_matter_g_m2=300.0,moisture=.25)
    return spec,biology,physical


@pytest.mark.parametrize('fraction',[.5,.96,1.0])
def test_biology_removes_gross_grain_and_physical_alone_applies_recovery(fraction):
    spec,b,p=mature_fixture(fraction)
    expected_dry=2*b.ridge_area*.1
    expected_water=expected_dry/.75-expected_dry
    before=copy.deepcopy(b.state)
    projected=b.project_harvest([0,1])
    assert b.state==before
    assert projected==pytest.approx(dict(gross_dry_kg=expected_dry,gross_water_kg=expected_water))
    result=p.execute('harvest',dict(start_ridge=0,end_ridge=1),b,spec.weather[0])
    assert result['dry_kg']==pytest.approx(expected_dry*fraction)
    assert result['water_kg']==pytest.approx(expected_water*fraction)
    assert p.state['harvest_loss_dry_kg']==pytest.approx(expected_dry*(1-fraction))
    assert p.state['harvest_loss_water_kg']==pytest.approx(expected_water*(1-fraction))
    assert 'loss' not in json.dumps(result)
    assert sum(r['grain_removed_g_m2'] for r in b.state['ridges'])==200
    with pytest.raises(OperationRejected,match='CROP_NOT_HARVESTABLE'):
        b.harvest([0])
    restored=NativePhysicalBackend(p.config,spec.farm)
    restored.load_state(p.export_state())
    assert restored.state==p.state


def test_capacity_preflight_uses_configured_recovery_before_any_removal():
    spec,b,p=mature_fixture(.5)
    # Gross wet mass is about 79 kg, recovered wet mass about 39 kg.
    p.config=replace(p.config,combine_capacity_kg=50)
    args=dict(start_ridge=0,end_ridge=1)
    before=copy.deepcopy(b.state)
    p.plan('harvest',args,b,spec.weather[0])
    assert b.state==before
    p.config=replace(p.config,harvest_recovery_fraction=1)
    with pytest.raises(OperationRejected,match='COMBINE_CAPACITY_EXCEEDED'):
        p.plan('harvest',args,b,spec.weather[0])
    assert b.state==before


@pytest.mark.parametrize('bad',[0,-.1,1.01,float('nan'),float('inf'),True])
def test_recovery_fraction_is_a_validated_machine_parameter(bad):
    with pytest.raises(ValueError):
        SeasonSpec(harvest_recovery_fraction=bad)


@pytest.mark.parametrize('values',[dict(seed_spacing_cm=.5),dict(seed_spacing_cm=51),
                                   dict(sowing_depth_cm=.5),dict(sowing_depth_cm=11)])
def test_scene_planting_constraints_fit_public_operation_ranges(values):
    with pytest.raises(ValueError): SeasonSpec(**values)


def test_exact_fuel_exhaustion_can_restore_and_reject_without_rollback_failure():
    spec=soybean_full_season()
    r=Runtime(replace(spec,season=replace(spec.season,initial_fuel_l=38.4)))
    for first in range(0,64,8):
        assert invoke(r,'prepare_seedbed',start_ridge=first,end_ridge=first+7).ok
    assert r.physical.state['fuel_l']==0
    assert r.physical.state['fuel_used_l']==38.4
    restored=Runtime.from_state(r.export_state())
    before=restored.audit_state()
    result=invoke(restored,'prepare_seedbed',start_ridge=0,end_ridge=0)
    assert not result.ok
    assert restored.audit_state()==before
    corrupt=r.physical.export_state()
    corrupt['state']['fuel_l']=-.01
    with pytest.raises(ValueError): r.physical.load_state(corrupt)


def test_battery_roundoff_at_exhaustion_is_normalized_but_negative_restore_rejected():
    spec,b,p=mature_fixture(.96)
    p.state['battery']['robot']=1.2-1e-14
    p.execute('ground_inspect',dict(start_ridge=0,end_ridge=0),b,spec.weather[0])
    assert p.state['battery']['robot']==0
    restored=NativePhysicalBackend(p.config,spec.farm)
    restored.load_state(p.export_state())
    corrupt=p.export_state();corrupt['state']['battery']['robot']=-.01
    with pytest.raises(ValueError): restored.load_state(corrupt)


def test_harvest_loss_tamper_fails_recovery_invariant():
    spec,b,p=mature_fixture(.96)
    p.execute('harvest',dict(start_ridge=0,end_ridge=1),b,spec.weather[0])
    for field in ('harvest_loss_dry_kg','harvest_loss_water_kg'):
        corrupt=p.export_state();corrupt['state'][field]+=1
        with pytest.raises(ValueError,match='Harvest recovery'):
            NativePhysicalBackend(p.config,spec.farm).load_state(corrupt)


def test_drying_crosses_midnight_with_schedule_duration_and_both_material_balances():
    r=Runtime(soybean_full_season());oracle=SeasonOracle()
    for _ in range(400):
        d=oracle.decide(r)
        assert d.request is not None and r.invoke(d.request).ok
        if d.request.tool=='unload': break
    else: pytest.fail('Reference strategy never unloaded')
    assert r.physical.moisture(r.physical.state['trailer'])>.13
    end_of_day=datetime.combine(r.now.date(),time(23,54),r.zone)
    if end_of_day<=r.now: end_of_day+=timedelta(days=1)
    assert invoke(r,'advance_time',hours=(end_of_day-r.now).total_seconds()/3600).ok
    initial_time=r.now
    dry_before=r.physical.state['trailer']['dry_kg']
    water_before=r.physical.state['trailer']['water_kg']
    result=invoke(r,'dry_grain',target_moisture=.13)
    assert result.ok and r.now.date()>initial_time.date()
    scheduled=[e.payload for e in r.events if e.kind=='job_scheduled'][-1]
    duration=result.data['receipt']['duration_hours']
    assert duration==scheduled['duration_hours']
    assert (r.now-initial_time).total_seconds()/3600==pytest.approx(duration,abs=1e-8)
    assert r.physical.state['trailer']['dry_kg']==dry_before
    assert r.physical.state['trailer']['water_kg']+result.data['receipt']['removed_water_kg']==pytest.approx(water_before)
    assert r.physical.audit()['dry_mass_balance_error_kg']==pytest.approx(0)
    assert r.physical.audit()['water_mass_balance_error_kg']==pytest.approx(0)
    assert Runtime.from_state(r.export_state()).physical.state==r.physical.state
