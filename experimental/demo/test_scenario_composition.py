"""Normal + stage-anchored drought: causal checks and process-boundary recovery."""
from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import pytest

from evaluate.scenarios.compose import compile_scenario, REFERENCE_PATH
from evaluate.scenarios.disturbances import Drought
from evaluate.scenarios.normal import SOYBEAN_NORMAL_MANAGEMENT
from evaluate.scenarios.spec import ScenarioSpec
from evaluate.scenarios.lib.l3.soybean_normal import (
    soybean_normal, soybean_drought_r1, soybean_drought_r5, soybean_drought_r1_r5, R1_DROUGHT, R5_DROUGHT)
from evaluate.scenarios.lib.l3.normal_validation import validate_suite
from fairy.acps.ontology.contracts import OperationRequest
from fairy.agents.loop.context.public import build_context
from fairy.agents.loop.context.history import public_history
from fairy.agents.loop.controller.run import run_agent
from fairy.agents.loop.controller.strategies import oracle_for
from fairy.events.session import Runtime
from fairy.events.artifacts import artifact, replay, assert_equivalent
from fairy.events.records import canonical, fingerprint


def invoke(runtime, tool, **args):
    result = runtime.invoke(OperationRequest(tool, args))
    assert result.ok, result.error
    return result


def planted(spec):
    runtime = Runtime(spec)
    invoke(runtime, "prepare_seedbed", start_ridge=0, end_ridge=63)
    invoke(runtime, "plant", start_ridge=0, end_ridge=63, spacing_m=.079, depth_mm=40)
    return runtime


def through(runtime, day):
    while runtime.now.date().isoformat() < day:
        invoke(runtime, "advance_time", hours=24)


def test_declarative_factories_do_not_construct_engines(monkeypatch):
    def forbidden(*a, **kw):
        raise AssertionError("Factory constructed an engine")
    monkeypatch.setattr(Runtime, "__init__", forbidden)
    for factory in (soybean_normal, soybean_drought_r1, soybean_drought_r5, soybean_drought_r1_r5):
        spec = factory()
        assert spec.season.cultivar == "HEINONG58" and spec.construction_json
        assert ScenarioSpec.from_dict(json.loads(canonical(spec.to_dict()))) == spec


def test_normal_and_variants_share_task_constraints_and_only_change_rainfall():
    normal = soybean_normal()
    assert all(w.rain_mm == 3 and w.et0_mm == 3 for w in normal.weather)
    phases = SOYBEAN_NORMAL_MANAGEMENT.phases
    assert {p.name for p in phases} == {"preparation", "establishment", "vegetative", "reproductive", "pod_fill", "harvest", "storage"}
    assert all(set(p.operations+p.observations) <= set(normal.tools) for p in phases)
    for factory in (soybean_drought_r1, soybean_drought_r5, soybean_drought_r1_r5):
        spec = factory()
        assert spec.task == normal.task and spec.irrigation_quota_mm == normal.irrigation_quota_mm == 60
        assert spec.farm == normal.farm and spec.season == normal.season
        manifest = json.loads(spec.construction_json)
        changed = {w.day for w in spec.weather if w.rain_mm != 3}
        assert len(changed) == sum(e["duration_days"] for e in manifest["events"])
        for base, compiled in zip(normal.weather, spec.weather):
            assert replace(base, rain_mm=compiled.rain_mm) == compiled


def test_composition_order_is_deterministic_and_does_not_mutate_normal():
    normal = soybean_normal()
    before = normal.to_dict()
    left = compile_scenario(normal, (R1_DROUGHT, R5_DROUGHT), scenario_id="paired")
    right = compile_scenario(normal, (R5_DROUGHT, R1_DROUGHT), scenario_id="paired")
    assert left == right and normal.to_dict() == before


@pytest.mark.parametrize("kwargs", [
    {"kind": "flood"}, {"kind": "pest"}, {"trigger_mode": "actual_stage"}, {"stage": "R9"},
    {"duration_days": 0}, {"duration_days": True}, {"duration_days": 1.5},
    {"offset_days": -1}, {"rainfall_factor": 1}, {"rainfall_factor": float("nan")},
    {"rainfall_factor": True}, {"event_id": "not an identifier"},
])
def test_unsupported_or_illegal_disturbances_reject(kwargs):
    values = dict(event_id="event", stage="R5", duration_days=3)
    with pytest.raises(ValueError):
        Drought(**{**values, **kwargs})


@pytest.mark.parametrize("events", [
    (R1_DROUGHT, R1_DROUGHT),
    (R1_DROUGHT, Drought("overlap", "R1", 2, offset_days=2)),
    (Drought("too_long", "R5", 30),),
    (Drought("wrong_target", "R5", 3, target_id="east_half"),),
    [R1_DROUGHT],
])
def test_conflicts_stage_overrun_target_and_mutability_reject(events):
    with pytest.raises(ValueError):
        compile_scenario(soybean_normal(), events, scenario_id="invalid")


def test_cannot_chain_a_disturbed_scenario_as_normal():
    with pytest.raises(ValueError, match="already disturbed"):
        compile_scenario(soybean_drought_r1(), (R5_DROUGHT,), scenario_id="invalid")


@pytest.mark.parametrize("mutation", ["date", "factor", "weather", "template", "constraint", "unknown_field"])
def test_recipe_and_actual_inputs_are_checked_on_restore(mutation):
    spec = soybean_drought_r1_r5()
    value = json.loads(canonical(spec.to_dict()))
    manifest = json.loads(value["construction_json"])
    if mutation == "date": manifest["events"][0]["start_day"] = "2026-06-03"
    elif mutation == "factor": manifest["events"][0]["rainfall_factor"] = .5
    elif mutation == "weather": value["weather"][30]["rain_mm"] = 99
    elif mutation == "template": manifest["management_template"]["phases"][0]["goal"] = "Invented goal"
    elif mutation == "constraint": value["irrigation_quota_mm"] = 999
    else: manifest["unexpected"] = "ignored instructions"
    manifest["recipe_fingerprint"] = fingerprint(manifest["events"])
    value["construction_json"] = canonical(manifest)
    with pytest.raises(ValueError):
        ScenarioSpec.from_dict(value)


def test_reference_mismatch_requires_explicit_rebuild(monkeypatch):
    import evaluate.scenarios.compose as composition
    original = composition.process_files
    monkeypatch.setattr(composition, "process_files", lambda: {**original(), "changed": "new model"})
    with pytest.raises(ValueError, match="explicitly rebuild"):
        soybean_drought_r5()


@pytest.mark.parametrize("day,pending", [
    ("2026-06-01", False), ("2026-06-08", False), ("2026-06-29", False),
    ("2026-07-04", False), ("2026-07-04", True),
])
def test_new_process_recovery_before_during_and_between_droughts(tmp_path, day, pending):
    runtime = planted(soybean_drought_r1_r5())
    through(runtime, day)
    if pending:
        invoke(runtime, "irrigate_range", start_ridge=0, end_ridge=63, depth_mm=5)
    source, target = tmp_path/"state.json", tmp_path/"continued.json"
    source.write_text(json.dumps(runtime.export_state()))
    original = runtime.export_state()
    continuous = Runtime.from_state(original)
    through(continuous, "2026-07-25")
    script = '''import json,sys
from fairy.events.session import Runtime
from fairy.acps.ontology.contracts import OperationRequest
r=Runtime.from_state(json.load(open(sys.argv[1])))
while r.now.date().isoformat()<'2026-07-25':
 assert r.invoke(OperationRequest('advance_time',{'hours':24})).ok
json.dump(r.export_state(),open(sys.argv[2],'w'))
'''
    subprocess.run([sys.executable, "-I", "-c", script, str(source), str(target)], check=True)
    state = json.loads(target.read_text())
    assert_equivalent(continuous.export_state(), state)
    restored = Runtime.from_state(state)
    assert replay(artifact(restored))[1]["replay_verified"]
    assert restored.pending_irrigation_mm == 0
    assert restored.irrigation_used_mm == (5 if pending else 0)
    for event_id, count in (("flowering_drought", 12), ("podfill_drought", 10)):
        assert sum(event_id in e.payload["forcing"]["disturbance_ids"] for e in restored.events
                   if e.kind == "environment_day_completed") == count


def test_segmented_stepping_cannot_repeat_disturbances_or_water():
    runtime = planted(soybean_drought_r1_r5())
    through(runtime, "2026-06-29")
    invoke(runtime, "irrigate_range", start_ridge=0, end_ridge=63, depth_mm=5)
    before = runtime.export_state()
    a, b = Runtime.from_state(before), Runtime.from_state(before)
    invoke(a, "advance_time", hours=24*15)
    for _ in range(30): invoke(b, "advance_time", hours=12)
    assert a.now == b.now and a.pending_irrigation_mm == b.pending_irrigation_mm == 0
    assert_equivalent(a.audit_state(), b.audit_state())
    assert [e.payload for e in a.events if e.kind == "environment_day_completed"] == [e.payload for e in b.events if e.kind == "environment_day_completed"]


def test_late_planting_cannot_move_the_frozen_drought():
    spec = soybean_drought_r1()
    early = planted(spec)
    late = Runtime(spec)
    through(late, "2026-05-25")
    invoke(late, "prepare_seedbed", start_ridge=0, end_ridge=63)
    invoke(late, "plant", start_ridge=0, end_ridge=63, spacing_m=.079, depth_mm=40)
    through(early, "2026-06-03")
    through(late, "2026-06-03")
    for runtime in (early, late):
        applied = [e.payload["day"] for e in runtime.events if e.kind == "environment_day_completed"
                   and "flowering_drought" in e.payload["forcing"]["disturbance_ids"]]
        assert applied == ["2026-06-02"]
    assert early.backend.field_observation([0])[0]["stage"] != late.backend.field_observation([0])[0]["stage"]


def test_forcing_audit_tamper_rejects_and_public_context_does_not_leak_calendar():
    runtime = planted(soybean_drought_r1_r5())
    context = build_context(runtime, public_history(runtime), max_chars=60000)
    for hidden in ("construction_json", "flowering_drought", "podfill_drought", "reference_calendar", "2026-06-30"):
        assert hidden not in canonical(context)
    through(runtime, "2026-06-03")
    state = runtime.export_state()
    event = next(e for e in state["events"] if e["kind"] == "environment_day_completed" and e["payload"]["forcing"]["disturbance_ids"])
    event["payload"]["forcing"]["disturbance_ids"] = []
    with pytest.raises(ValueError, match="forcing provenance"):
        Runtime.from_state(state)


def test_complete_paired_validation_and_nonadditive_effects():
    report, records = validate_suite()
    assert report["valid"] and len(records) == 7
    assert all(r["metrics"]["task_success"] and r["metrics"]["replay_verified"] for r in report["runs"].values())
    for case, check in report["checks"].items():
        assert check["stored_dry_mass_difference_from_normal_kg"] > 0
        assert check["fixed_intervention_response_kg"] > 0
        assert records[case+"_control"]["state"]["scenario_fingerprint"] == records[case+"_managed"]["state"]["scenario_fingerprint"]
    loss = lambda name: report["checks"][name]["stored_dry_mass_difference_from_normal_kg"]
    assert abs(loss("r1_r5")-loss("r1")-loss("r5")) > 1  # process carryover, never additive score patches


@pytest.mark.parametrize("factory", [soybean_normal, soybean_drought_r1, soybean_drought_r5, soybean_drought_r1_r5])
def test_cases_share_cli_oracle_and_explicit_backend_support(factory):
    spec = factory()
    runtime = Runtime(spec)
    run_agent(runtime, oracle_for(spec), max_steps=400, max_context_chars=60000)
    assert artifact(runtime)["metrics"]["task_success"]
    assert replay(artifact(runtime))[1]["replay_verified"]
    with pytest.raises(ValueError, match="no fallback"):
        Runtime(spec, "wofost")
