"""Integration and invariant checks for L3 in the same runtime as L1."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
from dataclasses import replace
import pytest
from evaluate.scenarios.lib.l3.soybean_full_season import soybean_full_season
from evaluate.metrics import evaluate
from fairy.events.session import Runtime
from fairy.events.artifacts import artifact,replay,assert_equivalent
from fairy.acps.ontology.contracts import OperationRequest
from fairy.agents.loop.controller.run import run_agent
from fairy.agents.loop.controller.strategies import oracle_for
from fairy.agents.loop.context.public import build_context


def invoke(r,name,**args):
    return r.invoke(OperationRequest(name,args))


def run():
    r=Runtime(soybean_full_season())
    run_agent(r,oracle_for(r.scenario),max_steps=400,max_context_chars=60000)
    return r


def test_new_runtime_full_season_oracle_replay_and_material_conservation():
    r=run()
    assert type(r) is Runtime
    m=evaluate(r)
    assert m['task_success'] and m['harvested_ridges']==64 and m['tool_errors']==0
    assert m['water_used_mm']<=4 and abs(m['dry_mass_balance_error_kg'])<1e-8
    p=r.physical.state
    assert p['seeds']+p['seeds_used']==r.scenario.season.initial_seed_count
    assert p['fuel_l']+p['fuel_used_l']==pytest.approx(r.scenario.season.initial_fuel_l)
    removed=sum(x['grain_removed_g_m2']*r.backend.ridge_area/1000 for x in r.backend.state['ridges'])
    assert removed==pytest.approx(p['harvested_dry_kg']+p['harvest_loss_dry_kg'])
    assert replay(artifact(r))[1]['replay_verified']
    assert not any(k in m for k in ('biological_yield','biomass','yield_score','fos'))


def test_pending_spatial_water_segmented_midnight_and_crossprocess(tmp_path):
    r=Runtime(soybean_full_season())
    result=invoke(r,'irrigate_range',start_ridge=22,end_ridge=43,depth_mm=8)
    assert result.ok
    assert result.data['receipt']['volume_m3']==pytest.approx(22*268*1.1*.008)
    state=r.export_state()
    before=copy.deepcopy(r.backend.state)
    for _ in range(2):
        assert invoke(r,'observe_soil_range',start_ridge=22,end_ridge=43).ok
    assert before==r.backend.state
    split=Runtime.from_state(state)
    invoke(split,'advance_time',hours=10)
    invoke(split,'advance_time',hours=10)
    direct=Runtime.from_state(state)
    invoke(direct,'advance_time',hours=20)
    assert_equivalent(direct.audit_state(),split.audit_state())
    src=tmp_path/'state.json';dst=tmp_path/'restored.json'
    src.write_text(json.dumps(state))
    script='''import json,sys
from fairy.events.session import Runtime
from fairy.acps.ontology.contracts import OperationRequest
r=Runtime.from_state(json.load(open(sys.argv[1])))
r.invoke(OperationRequest('advance_time',{'hours':20}))
json.dump(r.export_state(),open(sys.argv[2],'w'))
'''
    subprocess.run([sys.executable,'-c',script,str(src),str(dst)],check=True,env={**os.environ,'PYTHONHASHSEED':'924'})
    assert_equivalent(direct.export_state(),json.loads(dst.read_text()))
    assert direct.pending_irrigation_mm==0
    assert sum(x['pending_mm'] for x in direct.backend.state['ridges'])==0
    assert max(abs(e.payload['flux']['balance_error_mm']) for e in direct.events if e.kind=='environment_day_completed')<1e-10


@pytest.mark.parametrize('name,args',[
    ('irrigate_range',dict(start_ridge=0,end_ridge=63,depth_mm=5)),
    ('irrigate_range',dict(start_ridge=8,end_ridge=3,depth_mm=1)),
    ('harvest',dict(start_ridge=0,end_ridge=1)),
    ('plant',dict(start_ridge=0,end_ridge=1,spacing_m=.079,depth_mm=40)),
    ('unload',{}),('store_grain',{}),('charge_equipment',dict(equipment='hidden')),
    ('irrigate_range',dict(start_ridge=True,end_ridge=1,depth_mm=1)),
])
def test_rejected_actions_change_no_physics_inventory_or_clock(name,args):
    r=Runtime(soybean_full_season())
    b,p,t=r.backend.export_state(),r.physical.export_state(),r.now
    result=invoke(r,name,**args)
    assert not result.ok
    assert r.backend.export_state()==b and r.physical.export_state()==p and r.now==t
    assert r.pending_irrigation_mm==r.irrigation_used_mm==0


def test_late_failure_rolls_back_both_domains_and_crossed_midnight(monkeypatch):
    r=Runtime(soybean_full_season())
    invoke(r,'advance_time',hours=16.9)
    initial=r.export_state()
    def fail(*a,**kw):
        r.physical.state['seeds']=0
        r.backend.state['ridges'][0]['planted']=True
        raise RuntimeError('private backend state')
    monkeypatch.setattr(r.physical,'execute',fail)
    with pytest.raises(RuntimeError): invoke(r,'prepare_seedbed',start_ridge=0,end_ridge=7)
    assert_equivalent(json.loads(json.dumps(r.backend.export_state())),initial['backend_state'])
    assert_equivalent(json.loads(json.dumps(r.physical.export_state())),initial['physical_state'])
    assert r.now.isoformat()==initial['now']
    assert not any(e.kind=='job_completed' for e in r.events)


def test_water_changes_actual_soil_without_copying_hidden_truth_into_context():
    a,b=Runtime(soybean_full_season()),Runtime(soybean_full_season())
    invoke(a,'advance_time',hours=48);invoke(b,'advance_time',hours=48)
    invoke(a,'irrigate_range',start_ridge=22,end_ridge=43,depth_mm=8)
    invoke(a,'advance_time',hours=24);invoke(b,'advance_time',hours=24)
    assert a.backend.state['ridges'][22]['storage_mm']>b.backend.state['ridges'][22]['storage_mm']
    context=build_context(a,[],max_chars=60000)
    text=json.dumps(context)
    assert 'target_vwc' not in context and 'weather' not in context
    for key in ('grain_dry_g_m2','dry_matter_g_m2','rue_g_mj_par','detailed_briefing','library_context'):
        assert key not in text
    before=a.backend.export_state()
    invoke(a,'inspect_crop',start_ridge=0,end_ridge=63)
    assert a.backend.export_state()==before


def test_unsupported_season_backend_does_not_fallback():
    with pytest.raises(ValueError,match='no fallback'):
        Runtime(soybean_full_season(),'wofost')


def test_restore_rejects_tampered_bindings_and_replay_detects_material_tamper():
    r=run();record=artifact(r)
    corrupt=copy.deepcopy(record);corrupt['state']['physical_state']['state']['seeds']+=1
    with pytest.raises(ValueError): replay(corrupt)
    corrupt=copy.deepcopy(record);corrupt['state']['binding_fingerprint']='other'
    with pytest.raises(ValueError): Runtime.from_state(corrupt['state'])

@pytest.mark.parametrize('mutation', ['negative_seeds','pending_water','stored_moisture','harvest_mass','soil_storage'])
def test_restore_rejects_inconsistent_resource_state(mutation):
    r=run() if mutation in ('stored_moisture','harvest_mass') else Runtime(soybean_full_season())
    state=r.export_state()
    if mutation=='negative_seeds': state['physical_state']['state']['seeds']=-1
    elif mutation=='pending_water': state['backend_state']['state']['ridges'][0]['pending_mm']=64
    elif mutation=='stored_moisture': state['physical_state']['state']['stored_batches'][0]['moisture']=.9
    elif mutation=='harvest_mass': state['physical_state']['state']['harvested_dry_kg']+=1
    else: state['backend_state']['state']['ridges'][0]['storage_mm']+=1000
    with pytest.raises(ValueError): Runtime.from_state(state)


def test_spatial_irrigation_declares_midnight_not_job_and_hides_harvest_loss():
    r=Runtime(soybean_full_season())
    declaration=next(x for x in r.operations.manifest() if x['name']=='irrigate_range')
    assert declaration['timing']=='next_local_midnight'
    invoke(r,'irrigate_range',start_ridge=0,end_ridge=0,depth_mm=1)
    assert not any(e.kind=='job_scheduled' for e in r.events)
    complete=run()
    for e in complete.events:
        if e.kind=='operation_result':
            assert 'harvest_loss_dry_kg' not in json.dumps(e.payload)
            assert 'harvest_loss_water_kg' not in json.dumps(e.payload)


def test_fullseason_subprocess_recovery_after_material_transfer(tmp_path):
    r=Runtime(soybean_full_season())
    agent=oracle_for(r.scenario)
    for _ in range(400):
        d=agent.decide(r)
        assert d.request is not None
        assert r.invoke(d.request,actor='oracle').ok
        if d.request.tool=='unload': break
    else: pytest.fail('No material transfer')
    source=tmp_path/'resume.json';dest=tmp_path/'end.json'
    source.write_text(json.dumps(r.export_state()))
    code='''import json,sys
from fairy.events.session import Runtime
from fairy.agents.loop.controller.run import run_agent
from fairy.agents.loop.controller.strategies import oracle_for
r=Runtime.from_state(json.load(open(sys.argv[1])))
run_agent(r,oracle_for(r.scenario),max_steps=400,max_context_chars=60000)
json.dump(r.export_state(),open(sys.argv[2],'w'))
'''
    subprocess.run([sys.executable,'-c',code,str(source),str(dest)],check=True,env={**os.environ,'PYTHONHASHSEED':'211'})
    run_agent(r,agent,max_steps=400,max_context_chars=60000)
    assert_equivalent(r.export_state(),json.loads(dest.read_text()))
    assert evaluate(r)['task_success']


def test_bounded_fake_model_runs_same_l3_without_credentials():
    from types import SimpleNamespace
    from fairy.agents.models.llm.adapter import LLMAdapter
    oracle_run=run()
    requests=[e.payload for e in oracle_run.events if e.kind=='operation_requested']
    class FakeCompletions:
        def __init__(self): self.index=0
        def create(self,**kwargs):
            prompt=json.loads(kwargs['messages'][1]['content'])
            assert 'target_vwc' not in prompt
            assert 'rue_g_mj_par' not in json.dumps(prompt)
            if self.index==len(requests):
                msg=SimpleNamespace(content='Finished',tool_calls=[])
                reason='stop'
            else:
                entry=requests[self.index];self.index+=1
                msg=SimpleNamespace(content=None,tool_calls=[SimpleNamespace(function=SimpleNamespace(
                    name=entry['tool'],arguments=json.dumps(entry['arguments'])))])
                reason='tool_calls'
            return SimpleNamespace(usage=None,choices=[SimpleNamespace(message=msg,finish_reason=reason)])
    model=LLMAdapter(model='mock',client=SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions())))
    r=Runtime(soybean_full_season())
    run_agent(r,model,max_steps=400,max_context_chars=60000)
    assert evaluate(r)['task_success']
    assert all(e.payload['actor']=='agent' for e in r.events if e.kind=='operation_requested')
    assert replay(artifact(r,model.metadata))[1]['replay_verified']


def test_l1_irrigation_operation_keeps_same_water_semantics_on_l3():
    from fairy.acps.ontology.contracts import ObjectSpec,OperationBinding
    spec=soybean_full_season()
    spec=replace(spec,objects=spec.objects+(ObjectSpec('whole-field-irrigator','irrigation_system','plot-1'),),
                 tools=spec.tools+('irrigate',),operation_bindings=spec.operation_bindings+(OperationBinding('irrigate','whole-field-irrigator'),))
    r=Runtime(spec)
    assert invoke(r,'irrigate',depth_mm=1).ok
    assert sum(row['pending_mm'] for row in r.backend.state['ridges'])/64==r.pending_irrigation_mm==1
    restored=Runtime.from_state(r.export_state())
    assert invoke(restored,'advance_time',hours=24).ok
    assert sum(row['irrigation_mm'] for row in restored.backend.state['ridges'])/64==restored.irrigation_used_mm==1
    assert replay(artifact(restored))[1]['replay_verified']


def test_long_and_segmented_time_advancement_agree():
    a,b=Runtime(soybean_full_season()),Runtime(soybean_full_season())
    for r in (a,b):
        assert invoke(r,'prepare_seedbed',start_ridge=0,end_ridge=63).ok
        assert invoke(r,'plant',start_ridge=0,end_ridge=63,spacing_m=.079,depth_mm=40).ok
    assert invoke(a,'advance_time',hours=720).ok
    for _ in range(15): assert invoke(b,'advance_time',hours=48).ok
    assert_equivalent(a.audit_state(),b.audit_state())
    assert a.now==b.now


def test_capacity_and_storage_moisture_rejections_are_atomic():
    r=Runtime(soybean_full_season());policy=oracle_for(r.scenario)
    for _ in range(400):
        decision=policy.decide(r)
        assert decision.request
        if decision.request.tool=='harvest': break
        assert r.invoke(decision.request,actor='oracle').ok
    else: pytest.fail('No harvest window')
    # A small capacity is an explicit scenario input, not an altered stock.
    small=replace(r.scenario,season=replace(r.scenario.season,combine_capacity_kg=.001))
    other=Runtime(small)
    for event in r.events:
        if event.kind=='operation_requested':
            p=event.payload
            assert other.invoke(OperationRequest(p['tool'],p['arguments']),actor=p['actor']).ok
    prior=other.audit_state();now=other.now
    result=other.invoke(decision.request)
    assert not result.ok and result.error=='COMBINE_CAPACITY_EXCEEDED'
    assert_equivalent(other.audit_state(),prior);assert other.now==now
    assert r.invoke(decision.request).ok
    assert invoke(r,'unload').ok
    assert r.physical.moisture(r.physical.state['trailer'])>r.scenario.season.safe_moisture
    prior=r.audit_state();now=r.now
    result=invoke(r,'store_grain')
    assert not result.ok and result.error=='UNSAFE_STORAGE_MOISTURE'
    assert_equivalent(r.audit_state(),prior);assert r.now==now
    dry=r.physical.state['trailer']['dry_kg']
    assert invoke(r,'dry_grain',target_moisture=.13).ok
    assert r.physical.state['trailer']['dry_kg']==dry
    assert invoke(r,'store_grain').ok


def test_equipment_readings_consume_only_declared_time_energy():
    r=Runtime(soybean_full_season())
    initial=r.now
    result=invoke(r,'survey_thermal',start_ridge=0,end_ridge=7)
    assert result.ok
    assert (r.now-initial).total_seconds()==pytest.approx(8*.01*3600)
    assert r.physical.state['battery']['thermal']==92
    assert all(not row['available'] and row['canopy_air_delta_c'] is None for row in result.data['receipt']['observations'])
    assert invoke(r,'charge_equipment',equipment='thermal').ok
    assert r.physical.state['battery']['thermal']==100
    assert r.physical.state['electricity_kwh']==pytest.approx(.08)


def test_model_history_uses_linked_public_tool_messages():
    from types import SimpleNamespace
    from fairy.agents.models.llm.adapter import LLMAdapter
    captured={}
    def create(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(usage=None,choices=[SimpleNamespace(finish_reason='stop',message=SimpleNamespace(content='done',tool_calls=[]))])
    r=Runtime(soybean_full_season())
    req=OperationRequest('inspect_inventory')
    result=r.invoke(req)
    from dataclasses import asdict
    context=build_context(r,[{'request':asdict(req),'result':asdict(result)}],max_chars=60000)
    model=LLMAdapter(model='mock',client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    model.decide(context)
    messages=captured['messages']
    assert [m['role'] for m in messages]==['system','user','assistant','tool']
    call=messages[2]['tool_calls'][0]
    assert call['id']==messages[3]['tool_call_id'] and call['function']['name']=='inspect_inventory'
    assert 'history' not in json.loads(messages[1]['content'])
    assert json.loads(messages[3]['content'])==asdict(result)
    assert 'grain_dry_g_m2' not in json.dumps(messages)


def test_public_progress_survives_history_truncation_without_hidden_state():
    r=Runtime(soybean_full_season())
    assert invoke(r,'prepare_seedbed',start_ridge=0,end_ridge=63).ok
    assert invoke(r,'plant',start_ridge=0,end_ridge=63,spacing_m=.079,depth_mm=40).ok
    # No short-term history and no access to backend truth is needed.
    r.audit_state=lambda: (_ for _ in ()).throw(AssertionError('Private audit read'))
    context=build_context(r,[],max_chars=60000,max_turns=1)
    assert context['progress']['planted_ridges']==list(range(64))
    assert context['progress']['remaining_to_plant']==[]
    assert context['progress']['remaining_to_harvest']==list(range(64))
    assert context['history']==[]
    rejected=invoke(r,'plant',start_ridge=0,end_ridge=63,spacing_m=.079,depth_mm=40)
    assert rejected.error=='RIDGE_ALREADY_PLANTED'
    assert build_context(r,[],max_chars=60000)['progress']==context['progress']


@pytest.mark.parametrize('reason',['length','content_filter','stop',None])
def test_incomplete_tool_response_never_executes_even_with_valid_json(reason):
    from types import SimpleNamespace as NS
    from fairy.agents.models.llm.adapter import LLMAdapter
    response=NS(usage=None,choices=[NS(finish_reason=reason,message=NS(content=None,tool_calls=[
        NS(function=NS(name='irrigate_range',arguments='{"start_ridge":0,"end_ridge":0,"depth_mm":1}'))]))])
    model=LLMAdapter(model='mock',client=NS(chat=NS(completions=NS(create=lambda **kw:response))))
    r=Runtime(soybean_full_season());before=r.audit_state()
    decision=model.decide(build_context(r,[],max_chars=60000))
    assert decision.request.tool=='invalid_model_response'
    assert not r.invoke(decision.request).ok
    assert r.audit_state()==before and r.irrigation_used_mm==0


def test_shared_cli_resumes_l3_with_task_specific_oracle(tmp_path,capsys):
    from interface.cli.__main__ import main
    from fairy.events.artifacts import write_json,load_json
    r=Runtime(soybean_full_season())
    assert invoke(r,'prepare_seedbed',start_ridge=0,end_ridge=63).ok
    assert invoke(r,'plant',start_ridge=0,end_ridge=63,spacing_m=.079,depth_mm=40).ok
    source=tmp_path/'checkpoint.json';target=tmp_path/'resumed.json'
    write_json(source,artifact(r))
    assert main(['resume',str(source),'--out',str(target)])==0
    assert load_json(target)['agent']['resume']['through_event_seq']==len(r.events)
    assert main(['replay',str(target)])==0


@pytest.mark.parametrize('operation,equipment,cost,field',[
    ('survey_multispectral','multispectral',8.0,'ndvi'),
    ('ground_inspect','robot',9.6,'stage'),
])
def test_other_equipment_projections_have_scoped_energy_and_no_hidden_state(operation,equipment,cost,field):
    r=Runtime(soybean_full_season())
    before=r.backend.export_state()
    result=invoke(r,operation,start_ridge=0,end_ridge=7)
    assert result.ok
    assert r.physical.state['battery'][equipment]==pytest.approx(100-cost)
    assert all(field in row for row in result.data['receipt']['observations'])
    assert r.backend.export_state()==before  # same local day, no autonomous crop tick
    assert 'grain_dry_g_m2' not in json.dumps(result.data)
    assert 'dry_matter_g_m2' not in json.dumps(result.data)


def test_controller_stops_at_success_without_model_finish_or_extra_call():
    from fairy.agents.models.decision import Decision
    completed=run()
    requests=[OperationRequest(e.payload['tool'],e.payload['arguments']) for e in completed.events if e.kind=='operation_requested']
    class ReceiptSequence:
        def __init__(self): self.calls=0
        def decide(self, context):
            assert self.calls<len(requests), "Controller called model after goal was met"
            request=requests[self.calls];self.calls+=1
            return Decision(request=request)
    agent=ReceiptSequence()
    fresh=Runtime(soybean_full_season())
    run_agent(fresh,agent,max_steps=400,max_context_chars=60000)
    assert evaluate(fresh)['task_success'] and fresh.done
    assert fresh.events[-1].payload['reason']=='task_success'
    assert agent.calls==len(requests)
    # A restored unfinished episode may already satisfy the task predicate.
    checkpoint=Runtime(soybean_full_season())
    for request in requests: assert checkpoint.invoke(request).ok
    restored=Runtime.from_state(checkpoint.export_state())
    calls=agent.calls
    run_agent(restored,agent,max_steps=1,max_context_chars=60000)
    assert agent.calls==calls and restored.done
    assert replay(artifact(restored))[1]['replay_verified']
