"""Oracle MVP: evidence boundaries, non-physical services, training and qualification."""
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace as NS
import json
import pytest
from fairy.acps.ontology.contracts import OperationRequest
from fairy.events.session import Runtime
from fairy.events.artifacts import artifact,replay
from fairy.agents.loop.context.public import build_context
from fairy.agents.loop.context.history import public_history
from fairy.agents.loop.controller.run import run_agent
from fairy.agents.models.llm.adapter import LLMAdapter
from fairy.agents.models.decision import Decision
from fairy.agents.loop.controller.autogen.public import PublicView,grouped_context
from fairy.agents.loop.controller.autogen.guards import guard
from fairy.agents.loop.controller.autogen.extension import AutogenExtension
from fairy.agents.loop.controller.autogen.memory import MemorySnapshot,induce,append
from fairy.agents.loop.context.kg.lifecycle import seed_knowledge,validate_knowledge
from fairy.agents.models.numeric.irrigation import features,action_masks
from evaluate.scenarios.lib.l3.soybean_normal import soybean_normal
from evaluate.oracle.qualification import qualify,select


def call(r,tool,**args):
    value=r.invoke(OperationRequest(tool,args));assert value.ok,value.error;return value.data


def context(r):return build_context(r,public_history(r),max_chars=60000,max_turns=4)


def fresh(r):
    for tool in ('inspect_crop','observe_soil_range'):call(r,tool,start_ridge=0,end_ridge=63)
    call(r,'get_current_weather');call(r,'get_forecast',days=3)
    return context(r)


def planted():
    r=Runtime(soybean_normal());call(r,'prepare_seedbed',start_ridge=0,end_ridge=63)
    call(r,'plant',start_ridge=0,end_ridge=63,spacing_m=.079,depth_mm=40)
    return r


class TestAdvisor:
    __test__=False
    fingerprint='explicit-test-only-policy'
    def advise(self,c):return {'dose_mm':5.,'action':1,'features':features(c),'policy_fingerprint':self.fingerprint}


def extension(r):return AutogenExtension(seed_knowledge(context(r)),TestAdvisor())


@pytest.mark.parametrize('text',['<tool_call>def call="inspect_inventory">\n</function>\n</tool_call>',
                                  '<function=inspect_inventory>{}</function>'])
def test_actual_failed_text_is_protocol_error_not_normal_finish(text):
    response=NS(model='mock',usage=None,choices=[NS(finish_reason='stop',message=NS(content=text,tool_calls=[]))])
    class Client:
        base_url='https://example.invalid/v1'
        def __init__(self):self.chat=self;self.completions=self
        def create(self,**kwargs):return response
    model=LLMAdapter(model='mock',client=Client())
    decision=model.decide(context(Runtime(soybean_normal())))
    assert decision.protocol_error=='UNSTRUCTURED_TOOL_CALL'
    assert model.metadata['protocol_errors']==1


def test_evidence_and_phase_cannot_claim_emergence_without_observation():
    r=planted();c=context(r)
    assert PublicView(c).phase()=='establishment'
    assert c['progress']['prepared_ridges']==list(range(64))
    fresh(r);call(r,'advance_time',hours=24*8)
    assert PublicView(context(r)).phase()=='establishment'
    fresh(r);assert PublicView(context(r)).phase()=='growth_management'
    restored=Runtime.from_state(json.loads(json.dumps(r.export_state())))
    assert context(restored)==context(r)


def test_grouping_preserves_ids_values_and_does_not_mutate_receipts():
    r=planted();raw=fresh(r);before=deepcopy(raw);c=grouped_context(raw)
    assert raw==before
    turns=c['history']+c['public_memory']['observations']
    groups=next(t['result']['data']['receipt']['observations_grouped'] for t in turns if t['request']['tool']=='observe_soil_range')
    assert len(groups)==1 and groups[0]['ridge_ids']==list(range(64))
    assert groups[0]['root_vwc']==.31


@pytest.mark.parametrize('dose',[0,60,True,float('nan')])
def test_guard_bounds_before_physics(dose):
    r=planted();before=r.export_state()
    check=guard(context(r),OperationRequest('irrigate_range',dict(start_ridge=0,end_ridge=63,depth_mm=dose)))
    assert check.status=='BLOCKED' and r.export_state()==before


def test_missing_partial_stale_observation_is_unknown_not_false_or_zero():
    r=planted();check=guard(context(r),OperationRequest('irrigate_range',dict(start_ridge=0,end_ridge=63,depth_mm=5)))
    assert check.status=='NEED_OBSERVATION'
    call(r,'inspect_crop',start_ridge=0,end_ridge=63);call(r,'observe_soil_range',start_ridge=0,end_ridge=31)
    assert PublicView(context(r)).field('observe_soil_range') is None
    with pytest.raises(ValueError,match='NEED_OBSERVATION'):features(context(r))
    fresh(r);assert len(features(context(r)))==8
    call(r,'advance_time',hours=24)
    with pytest.raises(ValueError,match='NEED_OBSERVATION'):features(context(r))


def test_inventory_invalidates_after_real_resource_change():
    r=planted();call(r,'inspect_inventory');assert PublicView(context(r)).inventory() is not None
    call(r,'ground_inspect',start_ridge=0,end_ridge=0)
    assert PublicView(context(r)).inventory() is None


def test_r7_is_known_blocked_and_no_private_state_needed():
    r=planted()
    for hours in (720,720,384):call(r,'advance_time',hours=hours)
    c=fresh(r)
    assert PublicView(c).field('inspect_crop')[0]['stage']=='R7'
    check=guard(c,OperationRequest('harvest',dict(start_ridge=0,end_ridge=7)))
    assert check.status=='BLOCKED' and check.rule=='r8_maturity'
    assert 'audit' not in c and 'backend_state' not in c


def test_advice_is_nonphysical_bound_to_evidence_and_never_rewrites_dose():
    r=planted();c=fresh(r);e=extension(r);e.augment(c);before=r.export_state()
    assert e.intercept(OperationRequest('request_irrigation_advice'),c)
    assert r.export_state()==before and e.active_advice['dose_mm']==5
    wrong=OperationRequest('irrigate_range',dict(start_ridge=0,end_ridge=63,depth_mm=10))
    assert e.intercept(wrong,c) and r.export_state()==before
    call(r,'observe_soil_range',start_ridge=0,end_ridge=63)
    correct=OperationRequest('irrigate_range',dict(start_ridge=0,end_ridge=63,depth_mm=5))
    assert e.intercept(correct,context(r))  # observation version changed
    e.intercept(OperationRequest('request_irrigation_advice'),context(r))
    assert e.intercept(correct,context(r)) is False
    result=r.invoke(correct);assert result.ok
    e.observe(correct,result,public_history(r)[-1],context(r))
    assert e.trace[-1]['advice_id'] and r.pending_irrigation_mm==5
    assert e.intercept(OperationRequest('request_irrigation_advice'),context(r))
    assert e.trace[-1]['feedback']['status']=='BLOCKED'


def test_guards_retry_budget_does_not_create_fake_physical_events():
    r=Runtime(soybean_normal());e=extension(r)
    class Model:
        metadata={}
        def decide(self,c):return Decision(OperationRequest('irrigate_range',dict(start_ridge=0,end_ridge=63,depth_mm=60)))
    m=Model();run_agent(r,m,max_steps=10,context_extension=e,max_context_chars=60000,max_history_turns=4)
    assert r.tool_errors==0 and not any(x.kind=='operation_requested' for x in r.events)
    assert r.events[-1].payload['reason']=='precondition_retry_budget'
    assert len(m.metadata['autogen']['decision_trace'])==3
    assert replay(artifact(r,m.metadata))[1]['replay_verified']


@pytest.mark.parametrize('mutation',['dose','phase','tool','source','contract'])
def test_lifecycle_closed_schema_and_provenance(mutation):
    c=context(Runtime(soybean_normal()));k=seed_knowledge(c)
    if mutation=='dose':k['phases'][0]['goal']='Irrigate 20 mm every day'
    if mutation=='phase':k['phases'].pop()
    if mutation=='tool':k['phases'][0]['operations']=['fictional_tool']
    if mutation=='source':k['sources']['operations']['sha256']='changed'
    if mutation=='contract':k['contract_fingerprint']='changed'
    with pytest.raises(ValueError):validate_knowledge(k,c)


def test_memory_is_snapshot_local_evidence_deduplicated_and_not_oracle_answer(tmp_path):
    r=planted()
    # A real rejected dose and a later accepted request establish a local recovery.
    bad=r.invoke(OperationRequest('irrigate_range',dict(start_ridge=0,end_ridge=63,depth_mm=60)));assert not bad.ok
    call(r,'irrigate_range',start_ridge=0,end_ridge=63,depth_mm=5)
    record=artifact(r);rows=induce(record)
    assert any(row['kind']=='failure_lesson' and row['verified'] for row in rows)
    path=tmp_path/'memory.jsonl';empty=MemorySnapshot(path)
    assert append(path,rows)>0 and append(path,rows)==0
    assert not empty.rows and empty.fingerprint!=MemorySnapshot(path).fingerprint
    assert all(row['scope']=='local_workflow_not_full_season_success' for row in rows)
    assert not qualify(record)['reference_eligible']
    corrupted=deepcopy(record);corrupted['state']['scenario_fingerprint']='bad'
    with pytest.raises(ValueError):induce(corrupted)


def test_training_variants_are_real_separate_inputs_and_same_public_feature_path():
    pytest.importorskip('gymnasium')
    from evaluate.oracle.environment import IrrigationEnv,training_spec
    from fairy.events.records import fingerprint
    normal=soybean_normal().to_dict()
    assert fingerprint(training_spec(1).to_dict())!=fingerprint(training_spec(2).to_dict())
    assert training_spec(1).to_dict()==training_spec(1).to_dict()
    assert soybean_normal().to_dict()==normal
    e=IrrigationEnv();obs,_=e.reset(seed=7);before=e.runtime.export_state()
    assert list(obs)==pytest.approx(features(e.context))
    with pytest.raises(ValueError):e.step(99)
    assert e.runtime.export_state()==before
    e.context['status']['irrigation_remaining_mm']=0
    assert list(e.action_masks())==[True,False,False,False,False]
    e.context['status']['irrigation_remaining_mm']=60
    done=False
    while not done:_,_,done,_,info=e.step(0)
    assert info['metrics']['task_success'] and info['metrics']['water_used_mm']==0
    assert replay(artifact(e.runtime))[1]['replay_verified']


def test_actual_ppo_training_has_changed_weights_and_loads(tmp_path):
    pytest.importorskip('sb3_contrib')
    from torch import nn
    from fairy.agents.models.numeric.irrigation import DOSES, FEATURES
    from fairy.agents.models.numeric.modeling import IrrigationActorCriticPolicy
    from fairy.agents.models.numeric.training import train
    from fairy.agents.models.numeric.irrigation import PPOAdvisor
    from fairy.events.provenance import source_identity
    path=tmp_path/'policy.zip';m=train(path,timesteps=128)
    assert m['weight_l1_change']>0 and m['timesteps']>=128
    assert set(m['training_scenario_seeds']).isdisjoint(m['heldout_scenario_seeds'])
    advisor=PPOAdvisor(path,expected_source=source_identity()['fingerprint'])
    policy = advisor.policy.policy
    assert isinstance(policy, IrrigationActorCriticPolicy)
    actor, critic = policy.mlp_extractor.policy_net, policy.mlp_extractor.value_net
    for branch in (actor, critic):
        assert [(layer.in_features, layer.out_features) for layer in branch if isinstance(layer, nn.Linear)] == [
            (len(FEATURES), 32), (32, 32)
        ]
        assert sum(isinstance(layer, nn.Tanh) for layer in branch) == 2
    assert actor[0].weight is not critic[0].weight
    assert policy.action_net.out_features == len(DOSES)
    assert policy.value_net.out_features == 1
    r=planted();a=advisor.advise(fresh(r));assert 0<=a['dose_mm']<=30
    path.write_bytes(path.read_bytes()+b'x')
    with pytest.raises(ValueError,match='fingerprint'):PPOAdvisor(path)


def test_shared_loop_full_season_services_are_not_physical_and_stub_is_not_expert():
    r=Runtime(soybean_normal())
    class ZeroAdvisor(TestAdvisor):
        def advise(self,c):return {**super().advise(c),'dose_mm':0.,'action':0}
    e=AutogenExtension(seed_knowledge(context(r)),ZeroAdvisor())
    requests=[]
    def add(tool,**args):requests.append(OperationRequest(tool,args))
    def sensors():
        add('inspect_crop',start_ridge=0,end_ridge=63)
        add('observe_soil_range',start_ridge=0,end_ridge=63)
        add('get_current_weather')
    add('observe_soil_range',start_ridge=0,end_ridge=63);add('get_current_weather');add('inspect_inventory')
    add('prepare_seedbed',start_ridge=0,end_ridge=63);add('plant',start_ridge=0,end_ridge=63,spacing_m=.079,depth_mm=40)
    for _ in range(80):
        sensors();add('get_forecast',days=3);add('request_irrigation_advice');add('advance_time',hours=24)
    for first in range(0,64,8):
        sensors();add('harvest',start_ridge=first,end_ridge=first+7)
        add('inspect_inventory');add('unload')
        add('inspect_inventory');add('dry_grain',target_moisture=.13)
        add('inspect_inventory');add('store_grain')
    class Scripted:
        metadata={'kind':'scripted_integration_test_not_llm'}
        def decide(self,c):
            assert 'oracle_guidance' in c and 'backend_state' not in c
            return Decision(requests.pop(0))
    model=Scripted()
    run_agent(r,model,max_steps=800,max_errors=1,context_extension=e,max_context_chars=60000,max_history_turns=4)
    record=artifact(r,model.metadata);report=qualify(record)
    assert report['task_valid'] and report['replay_verified'] and r.tool_errors==0
    assert report['numeric_advice_calls']==80 and report['numeric_zero_advice_calls']==80
    assert not report['trained_numeric_policy_attested'] and not report['reference_eligible']
    assert select({'test':report})['selected'] is None
    assert not any(x.kind=='operation_requested' and x.payload['tool']=='request_irrigation_advice' for x in r.events)
    assert not e.missed_windows


def test_plain_finish_can_discuss_a_tool_marker_without_becoming_a_tool():
    response=NS(model='mock',usage=None,choices=[NS(finish_reason='stop',message=NS(content='I cannot continue; the service emitted <tool_call> text.',tool_calls=[]))])
    class Client:
        base_url='https://example.invalid'
        def __init__(self):self.chat=self;self.completions=self
        def create(self,**kwargs):return response
    a=LLMAdapter(model='mock',client=Client());decision=a.decide(context(Runtime(soybean_normal())))
    assert decision.request is None and not decision.protocol_error and decision.finish_reason=='model_finished'


def test_agent_service_restore_preserves_advice_and_context_without_new_prediction():
    r=planted();c=fresh(r);e=extension(r);e.augment(c)
    e.intercept(OperationRequest('request_irrigation_advice'),c)
    state=e.snapshot();saved=r.export_state()
    restored=Runtime.from_state(json.loads(json.dumps(saved)))
    copy=AutogenExtension(e.knowledge,TestAdvisor(),state=json.loads(json.dumps(state)))
    assert e.augment(c)==copy.augment(context(restored))
    assert copy.active_advice==e.active_advice and copy.snapshot()==e.snapshot()
    wrong=deepcopy(state);wrong['policy_fingerprint']='different'
    with pytest.raises(ValueError,match='different'):AutogenExtension(e.knowledge,TestAdvisor(),state=wrong)


def test_memory_profile_filter_and_real_extracted_package_knowledge(tmp_path):
    pytest.importorskip('yaml')
    from fairy.agents.loop.context.kg.lifecycle import load_knowledge
    r=planted();c=context(r)
    k=load_knowledge(None,c);assert k['phases']!=seed_knowledge(c)['phases']
    rows=induce(artifact(r));path=tmp_path/'memory.jsonl';append(path,rows)
    snapshot=MemorySnapshot(path);phase=rows[0]['phase'];profile=rows[0]['profile']
    assert snapshot.retrieve(phase,profile=profile)
    assert snapshot.retrieve(phase,profile={**profile,'contract_fingerprint':'incompatible'})==[]


def test_gymnasium_reset_and_step_contract():
    pytest.importorskip('gymnasium')
    from gymnasium.utils.env_checker import check_env
    from evaluate.oracle.environment import IrrigationEnv
    check_env(IrrigationEnv(),skip_render_check=True)


def test_llm_memory_condensation_does_not_generate_provenance_hashes(tmp_path):
    from fairy.agents.loop.controller.autogen.memory import condense
    r=planted();rows=induce(artifact(r))
    class Client:
        def __init__(self):self.chat=self;self.completions=self
        def create(self,**payload):
            candidates=json.loads(payload['messages'][1]['content'])
            assert all(set(row)=={'index','kind','phase','tool','lesson'} for row in candidates)
            content=json.dumps({'lessons':[{'index':row['index'],'lesson':row['lesson']} for row in candidates]})
            return NS(model='mock_induction',usage=NS(prompt_tokens=1,completion_tokens=1),
                      choices=[NS(finish_reason='stop',message=NS(content=content))])
    result=condense(rows,Client(),'mock_induction',tmp_path/'induction.json')
    assert [row['source_steps'] for row in result]==[row['source_steps'] for row in rows]
    assert all(row['induction']['method']=='llm_condensed_verified_public_candidates' for row in result)


def test_selection_does_not_hide_a_better_untrained_baseline():
    reports={'learned':{'reference_eligible':True,'trained_stack_eligible':True,
        'selection_objective':{'score':.72},'metrics':{'tool_errors':0}},
      'zero':{'reference_eligible':True,'trained_stack_eligible':False,
        'selection_objective':{'score':.92},'metrics':{'tool_errors':0}}}
    report=select(reports)
    assert report['selected']=='zero' and report['selected_trained_stack']=='learned'


def test_daily_adviser_revalidates_certificates_without_new_policy_decisions():
    class Counting(TestAdvisor):
        def __init__(self):self.calls=0
        def advise(self,c):self.calls+=1;return super().advise(c)
    r=planted();c=fresh(r);policy=Counting();e=AutogenExtension(seed_knowledge(c),policy)
    e.augment(c);e.intercept(OperationRequest('request_irrigation_advice'),c)
    first=deepcopy(e.active_advice)
    call(r,'observe_soil_range',start_ridge=0,end_ridge=63)
    e.intercept(OperationRequest('request_irrigation_advice'),context(r))
    assert policy.calls==1 and e.active_advice['decision_id']==first['decision_id']
    assert e.active_advice['advice_id']!=first['advice_id']
    assert e.trace[-1]['feedback']['status']=='REVALIDATED'
    assert not e.trace[-1]['feedback']['model_inference_performed']


def test_zero_decision_completes_window_and_new_day_is_explicit():
    class CountingZero(TestAdvisor):
        def __init__(self):self.calls=0
        def advise(self,c):self.calls+=1;return {**super().advise(c),'dose_mm':0.,'action':0}
    r=planted();c=fresh(r);policy=CountingZero();e=AutogenExtension(seed_knowledge(c),policy)
    e.augment(c);before=r.export_state();e.intercept(OperationRequest('request_irrigation_advice'),c)
    assert r.export_state()==before and e.active_advice['parameters'] is None
    assert e.trace[-1]['feedback']['clock_advanced'] is False
    e.intercept(OperationRequest('request_irrigation_advice'),c)
    assert policy.calls==1 and 'WINDOW_COMPLETE' in e.trace[-1]['feedback']['reason']
    restored=AutogenExtension(e.knowledge,policy,state=json.loads(json.dumps(e.snapshot())))
    restored.intercept(OperationRequest('request_irrigation_advice'),c)
    assert policy.calls==1 and 'WINDOW_COMPLETE' in restored.trace[-1]['feedback']['reason']
    call(r,'advance_time',hours=24);c=fresh(r);restored.augment(c)
    restored.intercept(OperationRequest('request_irrigation_advice'),c)
    assert policy.calls==2


def test_new_process_service_and_pending_water_restore_without_model_calls(tmp_path):
    import subprocess,sys
    r=planted();c=fresh(r);e=extension(r);e.augment(c)
    e.intercept(OperationRequest('request_irrigation_advice'),c)
    request=OperationRequest('irrigate_range',dict(start_ridge=0,end_ridge=63,depth_mm=5))
    assert e.intercept(request,c) is False
    result=r.invoke(request);e.observe(request,result,public_history(r)[-1],c)
    source=tmp_path/'checkpoint.json';target=tmp_path/'continued.json'
    source.write_text(json.dumps({'state':r.export_state(),'agent':e.snapshot()}))
    script='''import json,sys
from fairy.events.session import Runtime
from fairy.events.artifacts import artifact,replay
from fairy.acps.ontology.contracts import OperationRequest
from fairy.agents.loop.context.public import build_context
from fairy.agents.loop.context.history import public_history
from fairy.agents.loop.controller.autogen.extension import AutogenExtension
from fairy.agents.loop.controller.autogen.memory import MemorySnapshot
s=json.load(open(sys.argv[1]));r=Runtime.from_state(s['state'])
class NoInference:
 fingerprint=s['agent']['policy_fingerprint']
 manifest=s['agent']['numeric_policy_manifest']
 def advise(self,context):raise AssertionError('Restore must not rerun a numerical model')
e=AutogenExtension(s['agent']['knowledge'],NoInference(),memory=MemorySnapshot(rows=s['agent']['memory_snapshot']),state=s['agent'])
assert e.snapshot()==s['agent']
c=build_context(r,public_history(r),max_chars=60000,max_turns=4)
q=OperationRequest('advance_time',{'hours':24});result=r.invoke(q);assert result.ok
e.observe(q,result,public_history(r)[-1],c)
assert replay(artifact(r))[1]['replay_verified']
json.dump({'state':r.export_state(),'agent':e.snapshot()},open(sys.argv[2],'w'))
'''
    subprocess.run([sys.executable,'-I','-c',script,str(source),str(target)],check=True)
    old=context(r);q=OperationRequest('advance_time',{'hours':24});result=r.invoke(q)
    e.observe(q,result,public_history(r)[-1],old)
    assert json.loads(target.read_text())=={'state':r.export_state(),'agent':e.snapshot()}
    assert r.irrigation_used_mm==5 and r.pending_irrigation_mm==0
    assert sum(x.payload['flux']['irrigation_mm'] for x in r.events if x.kind=='environment_day_completed')==5


def test_agent_service_history_retains_all_current_public_observations():
    r=planted();c=fresh(r);e=extension(r);e.augment(c)
    e.intercept(OperationRequest('request_irrigation_advice'),c)
    model_context=e.augment(c)
    visible=model_context['history']+model_context['public_memory']['observations']
    tools={turn['request']['tool'] for turn in visible if turn['result'].get('ok')}
    assert {'inspect_crop','observe_soil_range','get_current_weather','get_forecast'}<=tools
    # The extra advice receipt is real agent-service history, never a fabricated sensor result.
    assert any(turn['request']['tool']=='request_irrigation_advice' for turn in model_context['history'])
    assert model_context['public_memory']['source'].startswith('public event receipts')


def test_preplant_crop_read_is_not_fresh_after_same_day_planting():
    r=Runtime(soybean_normal());call(r,'inspect_crop',start_ridge=0,end_ridge=63)
    call(r,'prepare_seedbed',start_ridge=0,end_ridge=63)
    call(r,'plant',start_ridge=0,end_ridge=63,spacing_m=.079,depth_mm=40)
    assert PublicView(context(r)).field('inspect_crop') is None
    assert PublicView(context(r)).phase()=='establishment'
    call(r,'observe_soil_range',start_ridge=0,end_ridge=63);call(r,'get_current_weather');call(r,'get_forecast',days=3)
    with pytest.raises(ValueError,match='inspect_crop'):features(context(r))
    call(r,'inspect_crop',start_ridge=0,end_ridge=63)
    assert features(context(r))[0]==pytest.approx(1/5)
