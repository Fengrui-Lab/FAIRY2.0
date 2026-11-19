"""Full-season PCSE acceptance through the shared Runtime and public operations."""
from copy import deepcopy
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
import pytest
pytest.importorskip('pcse')
from fairy.events.session import Runtime
from fairy.events.artifacts import artifact,replay,assert_equivalent
from fairy.acps.ontology.contracts import OperationRequest
from evaluate.scenarios.lib.l3.soybean_wofost import soybean_wofost_normal,soybean_wofost_reproductive_drought
from evaluate.oracle.environment import call,observe,IrrigationEnv


def planted():
    r=Runtime(soybean_wofost_normal(),'wofost')
    call(r,'prepare_seedbed',start_ridge=0,end_ridge=63)
    call(r,'plant',start_ridge=0,end_ridge=63,spacing_m=.079,depth_mm=40)
    return r


def test_backend_and_cultivar_are_explicit():
    from evaluate.scenarios.lib.l3.soybean_normal import soybean_normal
    with pytest.raises(ValueError,match='explicitly declared'):Runtime(soybean_normal(),'wofost')
    with pytest.raises(ValueError,match='HEINONG58'):Runtime(soybean_wofost_normal(),'native')


def test_observation_is_pure_and_public_semantics_hide_model_state():
    r=planted();before=r.backend.export_state();clock=r.now;c=observe(r)
    assert r.now==clock and r.backend.export_state()==before
    rows=r.backend.field_observation((0,))
    assert rows[0]['stage']=='pre_emergence'
    assert 'TWSO' not in json.dumps(c) and 'mature_dry_kg' not in json.dumps(c)
    assert c['constraints']['cultivar']=='Soybean_901_proxy'
    assert 'R5' not in json.dumps(rows)


def test_partial_irrigation_units_restore_and_split_midnight(tmp_path):
    a=planted();call(a,'irrigate_range',start_ridge=0,end_ridge=0,depth_mm=32)
    assert a.pending_irrigation_mm==.5
    assert a.irrigation_used_mm==.5
    before=a.export_state()
    source=tmp_path/'state.json';target=tmp_path/'continued.json';source.write_text(json.dumps(before))
    script="""import json,sys
from fairy.events.session import Runtime
from fairy.acps.ontology.contracts import OperationRequest
r=Runtime.from_state(json.load(open(sys.argv[1])))
assert r.invoke(OperationRequest('advance_time',{'hours':24}),actor='numeric_training_driver').ok
json.dump(r.export_state(),open(sys.argv[2],'w'))
"""
    subprocess.run([sys.executable,'-c',script,str(source),str(target)],check=True,env=os.environ.copy())
    b=Runtime.from_state(before)
    call(a,'advance_time',hours=24)
    call(b,'advance_time',hours=6);call(b,'advance_time',hours=18)
    assert_equivalent(a.backend.export_state(),b.backend.export_state())
    assert_equivalent(a.export_state(),json.loads(target.read_text()))
    rows=a.backend.management_state()
    assert rows[0].irrigation_mm==32 and rows[1].irrigation_mm==0
    assert a.pending_irrigation_mm==0
    flux=next(e.payload['flux'] for e in a.events if e.kind=='environment_day_completed')
    assert abs(flux['balance_error_mm'])<1e-7
    assert a.scenario.farm.volume_m3(.5)==pytest.approx(9.4336)


def test_sowing_keeps_preexisting_soil_and_late_sowing_is_independent():
    r=Runtime(soybean_wofost_normal(),'wofost')
    call(r,'prepare_seedbed',start_ridge=0,end_ridge=63)
    call(r,'advance_time',hours=48)
    before=r.backend.management_state()[0].root_vwc
    call(r,'plant',start_ridge=0,end_ridge=0,spacing_m=.079,depth_mm=40)
    assert r.backend.management_state()[0].root_vwc==before
    call(r,'advance_time',hours=24*8)
    call(r,'plant',start_ridge=1,end_ridge=1,spacing_m=.079,depth_mm=40)
    rows=r.backend.management_state()
    assert rows[0].phase=='vegetative' and rows[1].phase=='pre_emergence' and rows[2].phase=='unplanted'


@pytest.mark.parametrize('depth',[0,-1,51,float('nan'),True])
def test_illegal_inputs_have_no_physical_effect(depth):
    r=planted();before=r.backend.export_state()
    if type(depth) is float and depth!=depth:
        with pytest.raises(ValueError):r.invoke(OperationRequest('irrigate_range',dict(start_ridge=0,end_ridge=63,depth_mm=depth)))
        assert before==r.backend.export_state()
        return
    result=r.invoke(OperationRequest('irrigate_range',dict(start_ridge=0,end_ridge=63,depth_mm=depth)))
    assert not result.ok and before==r.backend.export_state()


def test_failure_after_day_update_rolls_back_all_columns(monkeypatch):
    r=planted();call(r,'irrigate_range',start_ridge=0,end_ridge=63,depth_mm=5)
    before=r.backend.export_state();clock=r.now;original=r.backend.advance_day
    def fail(*args):
        original(*args);raise RuntimeError('private injected failure')
    monkeypatch.setattr(r.backend,'advance_day',fail)
    with pytest.raises(RuntimeError):call(r,'advance_time',hours=24)
    assert r.now==clock and r.pending_irrigation_mm==5
    assert_equivalent(before,r.backend.export_state())
    assert 'private' not in json.dumps(r.events[-1].payload)


def test_restore_rejects_changed_summary_and_operations():
    r=planted();call(r,'advance_time',hours=24)
    payload=r.export_state();bad=deepcopy(payload)
    bad['backend_state']['summary']['rows'][0]['pending_mm']=1
    with pytest.raises(ValueError):Runtime.from_state(bad)
    bad=deepcopy(payload);bad['backend_state']['inputs'][0]['indices']=[-1]
    with pytest.raises(ValueError):Runtime.from_state(bad)


def test_complete_delivery_replay_maturity_and_pending_state():
    env=IrrigationEnv(backend='wofost',canonical=True);env.reset();done=False
    while not done:
        _,_,terminated,truncated,info=env.step(0);done=terminated or truncated
    assert not truncated and info['metrics']['task_success']
    r=env.runtime
    assert all(row.harvested for row in r.backend.management_state())
    record=artifact(r);restored=Runtime.from_state(record['state'])
    assert_equivalent(restored.audit_state(),r.audit_state())
    assert replay(record)[1]['replay_verified']
    assert info['metrics']['tool_errors']==0
    assert info['metrics']['stored_dry_grain_kg']>0
    assert all(e.model.crop is None for e in r.backend.engines)
    assert r.backend.project_harvest(())=={'gross_dry_kg':0.,'gross_water_kg':0.}
    weather=next(d for d in r.scenario.weather if d.day==r.backend.next_day)
    flux=r.backend.advance_day(weather,0.)
    assert abs(flux['transpiration_mm'])<1e-10
    assert abs(flux['balance_error_mm'])<1e-7
    with pytest.raises(Exception,match='CROP_NOT_HARVESTABLE'):r.backend.harvest((0,))


def test_frozen_drought_rejects_changed_forcing():
    spec=soybean_wofost_reproductive_drought()
    weather=list(spec.weather);weather[0]=replace(weather[0],rain_mm=0)
    with pytest.raises(ValueError,match='forcing'):replace(spec,weather=tuple(weather),public_weather=tuple(weather))


def test_policy_backend_scope_checked_without_inference():
    from fairy.agents.models.numeric.irrigation import PPOAdvisor
    advisor=PPOAdvisor.__new__(PPOAdvisor);advisor.manifest={'backend':'native','crop':'HEINONG58'}
    with pytest.raises(ValueError,match='scope mismatch'):advisor.advise(observe(planted()))


def test_selection_separates_backend_and_scenario_domains():
    from evaluate.oracle.qualification import select
    base=dict(reference_eligible=True,trained_stack_eligible=False,selection_objective={'score':1},metrics={'tool_errors':0})
    report=select({'native':{**base,'comparison_domain':{'backend':'native','scenario':'a'}},
                   'wofost':{**base,'comparison_domain':{'backend':'wofost','scenario':'b'}}})
    assert report['selected'] is None and len(report['groups'])==2


def test_trained_manifest_must_match_recorded_policy_fingerprint(monkeypatch):
    import evaluate.oracle.qualification as qualification
    from evaluate.scenarios.lib.l3.soybean_normal import soybean_normal
    r=Runtime(soybean_normal());record=artifact(r)
    policy=dict(algorithm='MaskablePPO',actor_l1_change=1,source_fingerprint=record['state']['source_fingerprint'],
        qualification_scenario_fingerprint=record['state']['scenario_fingerprint'],backend='native',crop='HEINONG58',
        timesteps=128,weights_sha256='a'*64)
    record['agent']={'autogen':{'numeric_policy_manifest':policy,'policy_fingerprint':'unrelated'}}
    monkeypatch.setattr(qualification,'replay',lambda _: (r,record['metrics']))
    assert not qualification.qualify(record)['trained_numeric_policy_attested']


@pytest.mark.parametrize('backend',['native','wofost'])
def test_partial_harvest_phase_stays_harvest_after_fresh_observation(backend):
    from fairy.agents.loop.controller.autogen.public import PublicView
    from fairy.agents.loop.context.public import build_context
    from fairy.agents.loop.context.history import public_history
    from evaluate.scenarios.lib.l3.soybean_normal import soybean_normal
    r=Runtime(soybean_wofost_normal() if backend=='wofost' else soybean_normal(),backend)
    call(r,'prepare_seedbed',start_ridge=0,end_ridge=63)
    call(r,'plant',start_ridge=0,end_ridge=63,spacing_m=.079,depth_mm=40)
    for _ in range(3):call(r,'advance_time',hours=720)
    call(r,'harvest',start_ridge=0,end_ridge=0)
    call(r,'inspect_crop',start_ridge=0,end_ridge=63)
    c=build_context(r,public_history(r),max_chars=60000,max_turns=4)
    assert c['progress']['harvested_ridges']==[0]
    assert PublicView(c).phase()=='harvest'


def test_public_container_projection_tracks_only_recorded_receipts():
    from fairy.agents.loop.context.public import build_context
    from fairy.agents.loop.context.history import public_history
    from evaluate.scenarios.lib.l3.soybean_normal import soybean_normal
    r=Runtime(soybean_normal())
    def c():return build_context(r,public_history(r),max_chars=60000,max_turns=4)['progress']
    assert c()['public_containers']['amounts']=={'combine':None,'trailer':None}
    call(r,'inspect_inventory')
    call(r,'prepare_seedbed',start_ridge=0,end_ridge=63)
    call(r,'plant',start_ridge=0,end_ridge=63,spacing_m=.079,depth_mm=40)
    for hours in (720,720,480):call(r,'advance_time',hours=hours)
    call(r,'harvest',start_ridge=0,end_ridge=0)
    assert c()['latest_harvest_receipt']['completed_ridges']==[0]
    for tool,args in [('unload',{}),('dry_grain',{'target_moisture':.13}),('store_grain',{})]:
        amounts=c()['public_containers']['amounts'];physical=r.physical.inventory()
        for name in ('combine','trailer'):
            for key in ('dry_kg','water_kg'):assert amounts[name][key]==pytest.approx(physical[name][key])
        call(r,tool,**args)
    assert c()['public_containers']['amounts']=={'combine':{'dry_kg':0.,'water_kg':0.},'trailer':{'dry_kg':0.,'water_kg':0.}}


def test_container_projection_never_reads_unobserved_private_inventory():
    from fairy.agents.loop.context.public import build_context
    from fairy.agents.loop.context.history import public_history
    r=planted()
    r.physical.state['combine']={'dry_kg':12.,'water_kg':3.}
    c=build_context(r,public_history(r),max_chars=60000,max_turns=4)
    assert c['progress']['public_containers']['amounts']['combine'] is None
