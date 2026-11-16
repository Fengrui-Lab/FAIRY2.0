"""Regression evidence for issues found in the full-version review."""
from dataclasses import replace
from datetime import date, timedelta
from types import SimpleNamespace as NS
import pytest
from evaluate.scenarios.lib.l1.soybean_irrigation import soybean_irrigation
from evaluate.metrics import evaluate
from fairy.acps.backends.biology.contracts import WeatherDay
from fairy.acps.ontology.contracts import OperationRequest
from fairy.agents.loop.context.public import build_context
from fairy.agents.loop.controller.run import run_agent
from fairy.agents.loop.controller.oracle import IrrigationOracle
from fairy.agents.models.llm.adapter import LLMAdapter
from fairy.events.artifacts import artifact, replay
from fairy.events.session import Runtime


@pytest.mark.parametrize('keyword,value', [
    ('max_steps',True),('max_steps',1.5),('max_errors',0),('max_history_turns',False),
    ('max_context_chars',float('inf')),('wall_timeout',float('nan')),('wall_timeout',0),
])
def test_loop_budget_validation_precedes_agent_and_state_changes(keyword,value):
    r=Runtime(soybean_irrigation());before=r.export_state()
    with pytest.raises(ValueError): run_agent(r,object(),**{keyword:value})
    assert r.export_state()==before


def test_exhausted_cumulative_error_budget_never_calls_model_after_restore():
    r=Runtime(soybean_irrigation())
    assert not r.invoke(OperationRequest('unknown')).ok
    r=Runtime.from_state(r.export_state())
    class Forbidden:
        def decide(self, context): raise AssertionError('Model called after budget was exhausted')
    run_agent(r,Forbidden(),max_errors=1)
    assert r.irrigation_used_mm==0
    assert r.events[-1].payload['reason']=='tool_error_budget'
    assert replay(artifact(r))[1]['replay_verified']


def test_oracle_search_respects_segment_limit_and_longer_task():
    spec=soybean_irrigation()
    weather=tuple(WeatherDay((date(2026,5,1)+timedelta(days=i)).isoformat()) for i in range(12))
    spec=replace(spec,deadline='2026-05-12T07:00:00+08:00',weather=weather,max_advance_hours=12)
    r=run_agent(Runtime(spec),IrrigationOracle(),max_steps=20)
    assert evaluate(r)['task_success'] and r.tool_errors==0
    assert replay(artifact(r))[1]['replay_verified']


def test_oracle_considers_fractional_positive_water_budget():
    spec=replace(soybean_irrigation(),irrigation_quota_mm=.5,target_vwc_min=.14,target_vwc_max=.45)
    r=run_agent(Runtime(spec),IrrigationOracle())
    assert evaluate(r)['task_success'] and r.irrigation_used_mm==.5


@pytest.mark.parametrize('field', ['weather','prefix_irrigation'])
def test_scenario_rejects_mutable_input_containers(field):
    spec=soybean_irrigation()
    with pytest.raises(ValueError): replace(spec,**{field:list(getattr(spec,field))})


def test_model_configuration_provenance_redacts_endpoint_secrets():
    client=NS(base_url='http://user:password@localhost:9000/v1?key=secret#fragment')
    model=LLMAdapter(model='test',client=client,timeout=45)
    assert model.metadata['base_url']=='http://localhost:9000/v1'
    assert model.metadata['request_timeout_seconds']==45
    assert 'secret' not in str(model.metadata) and 'password' not in str(model.metadata)


@pytest.mark.parametrize('timeout',[float('nan'),float('inf'),0,-1,True])
def test_model_request_timeout_must_be_finite_positive(timeout):
    with pytest.raises(ValueError): LLMAdapter(model='test',client=object(),timeout=timeout)


@pytest.mark.parametrize('kwargs',[{'max_chars':0},{'max_turns':0},{'max_chars':True}])
def test_context_budget_validation(kwargs):
    with pytest.raises(ValueError): build_context(Runtime(soybean_irrigation()),[],**kwargs)


@pytest.mark.parametrize('values',[{'length_m':1e308,'width_m':1e308}, {'root_depth_m':1e308}, {'farm_id':12}, {'plot_id':' '}])
def test_farm_rejects_invalid_identity_and_overflowing_derived_geometry(values):
    from fairy.acps.ontology.farm.spec import FarmSpec
    with pytest.raises(ValueError): FarmSpec(**values)


def test_harvest_is_bound_to_combine_and_planting_to_tractor():
    from evaluate.scenarios.lib.l3.soybean_full_season import soybean_full_season
    r=Runtime(soybean_full_season())
    bindings={item['name']:item['provider_id'] for item in r.operations.manifest()}
    assert bindings['harvest']==bindings['unload']=='combine-1'
    assert bindings['plant']==bindings['prepare_seedbed']=='tractor-1'
    assert any(o['object_id']=='combine-1' and o['type_id']=='combine' for o in r.public_objects())


@pytest.mark.parametrize('day',['2026-05-02junk','20260502','2026-05-32'])
def test_prefix_rejects_unmatched_or_noncanonical_dates(day):
    with pytest.raises(ValueError): replace(soybean_irrigation(),prefix_irrigation=((day,10.0),))


def test_farm_rejects_underflowed_zero_area():
    from fairy.acps.ontology.farm.spec import FarmSpec
    with pytest.raises(ValueError): FarmSpec(length_m=1e-300,width_m=1e-300)


def test_oracle_candidate_memory_is_bounded_and_small_grid_unchanged():
    from fairy.agents.loop.controller.oracle import depth_candidates
    assert depth_candidates(45)==list(range(1,46))
    assert depth_candidates(.5)==[.5]
    for quota in (1000000,1e12,1e308):
        depths=depth_candidates(quota)
        assert len(depths)<=128 and depths[-1]==quota
