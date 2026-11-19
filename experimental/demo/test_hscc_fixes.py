"""HSCC counterexamples: cancellation, continuation, protocol and causal checks."""
from dataclasses import asdict, replace
from types import SimpleNamespace as NS
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import pytest

from evaluate.metrics import evaluate
from evaluate.scenarios.lib.l1.soybean_irrigation import soybean_irrigation
from evaluate.scenarios.lib.l3.soybean_full_season import soybean_full_season
from fairy.acps.ontology.contracts import OperationRequest
from fairy.agents.loop.context.history import public_history
from fairy.agents.loop.context.public import build_context
from fairy.agents.loop.controller.run import run_agent
from fairy.agents.loop.controller.season_oracle import SeasonOracle
from fairy.agents.models.decision import Decision
from fairy.agents.models.llm.adapter import LLMAdapter
from fairy.events.artifacts import artifact, replay, write_json, load_json
from fairy.events.session import Runtime


def invoke(runtime, name, **args):
    return runtime.invoke(OperationRequest(name, args))


@pytest.mark.parametrize("backend", ["native", "wofost"])
def test_interrupt_after_day_update_rolls_back_and_replays(backend):
    if backend == "wofost":
        pytest.importorskip("pcse")
    runtime = Runtime(soybean_irrigation(), backend)
    invoke(runtime, "observe_soil")
    invoke(runtime, "irrigate", depth_mm=10)
    before = runtime.audit_state()
    now = runtime.now
    original = runtime.backend.advance_day
    def interrupted(*args):
        original(*args)
        raise KeyboardInterrupt()
    runtime.backend.advance_day = interrupted
    with pytest.raises(KeyboardInterrupt):
        invoke(runtime, "advance_time", hours=24)
    assert runtime.now == now and runtime.audit_state() == before
    assert runtime.pending_irrigation_mm == 10 and runtime.tool_errors == 0
    assert runtime.events[-1].kind == "operation_cancelled"
    restored = Runtime.from_state(runtime.export_state())
    assert restored.audit_state() == before
    assert replay(artifact(restored))[1]["replay_verified"]
    assert invoke(restored, "advance_time", hours=24).ok
    assert restored.pending_irrigation_mm == 0


def test_interrupt_after_physical_effect_rolls_back(monkeypatch):
    from fairy.acps.backends.physical.native.backend import NativePhysicalBackend
    runtime = Runtime(soybean_full_season())
    before = runtime.audit_state()
    original = NativePhysicalBackend.execute
    def interrupted(self, *args):
        original(self, *args)
        raise KeyboardInterrupt()
    monkeypatch.setattr(NativePhysicalBackend, "execute", interrupted)
    with pytest.raises(KeyboardInterrupt):
        invoke(runtime, "prepare_seedbed", start_ridge=0, end_ridge=7)
    assert runtime.audit_state() == before
    assert replay(artifact(Runtime.from_state(runtime.export_state())))[1]["replay_verified"]


@pytest.mark.parametrize("kind", ["success", "rejection", "termination"])
def test_interrupt_during_event_publication_keeps_consistent_state(monkeypatch, kind):
    runtime = Runtime(soybean_irrigation())
    original = runtime._emit
    def interrupted(name, *args, **kwargs):
        value = original(name, *args, **kwargs)
        if name == ("run_terminated" if kind == "termination" else "operation_result"):
            raise KeyboardInterrupt()
        return value
    monkeypatch.setattr(runtime, "_emit", interrupted)
    with pytest.raises(KeyboardInterrupt):
        if kind == "termination":
            runtime.finish("model_finished")
        else:
            invoke(runtime, "observe_soil" if kind == "success" else "unknown")
    assert runtime.tool_errors == 0 and not runtime.done
    assert replay(artifact(Runtime.from_state(runtime.export_state())))[1]["replay_verified"]


def test_cli_interrupt_preserves_resumable_trace_and_source(tmp_path, monkeypatch, capsys):
    from interface.cli.__main__ import main
    monkeypatch.setattr(LLMAdapter, "__init__", lambda self, **kw: setattr(self, "metadata", {"kind": "mock"}))
    def interrupted(self, context):
        raise KeyboardInterrupt()
    monkeypatch.setattr(LLMAdapter, "decide", interrupted)
    source, target = tmp_path / "interrupted.json", tmp_path / "continued.json"
    assert main(["run", "--agent", "llm", "--out", str(source)]) == 130
    record = load_json(source)
    assert not record["state"]["done"]
    assert record["state"]["events"][-1]["kind"] == "run_suspended"
    assert replay(record)[1]["replay_verified"]
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    assert main(["resume", str(source), "--out", str(target)]) == 0
    assert hashlib.sha256(source.read_bytes()).hexdigest() == digest
    assert replay(load_json(target))[1]["replay_verified"]


def test_cancelled_history_tamper_is_rejected():
    runtime = Runtime(soybean_irrigation())
    runtime.record_cancelled(OperationRequest("advance_time", {"hours": 24}), "agent")
    state = runtime.export_state()
    state["events"][-1]["payload"]["code"] = "pretend_success"
    with pytest.raises(ValueError):
        Runtime.from_state(state)
    with pytest.raises(ValueError):
        replay({**artifact(runtime), "state": state})


def test_season_oracle_respects_twelve_hour_limit():
    runtime = Runtime(replace(soybean_full_season(), max_advance_hours=12))
    run_agent(runtime, SeasonOracle(), max_steps=400, max_context_chars=60000)
    assert evaluate(runtime)["task_success"] and runtime.tool_errors == 0
    assert all(e.payload["arguments"]["hours"] <= 12 for e in runtime.events
               if e.kind == "operation_requested" and e.payload["tool"] == "advance_time")
    assert replay(artifact(runtime))[1]["replay_verified"]


def test_resume_context_equals_continuous_context_and_retains_old_evidence():
    runtime = Runtime(soybean_full_season())
    invoke(runtime, "inspect_inventory")
    invoke(runtime, "inspect_crop", start_ridge=0, end_ridge=1)
    invoke(runtime, "harvest", start_ridge=0, end_ridge=1)  # unresolved rejection
    history = public_history(runtime)
    expected = build_context(runtime, history, max_chars=60000, max_turns=1)
    restored = Runtime.from_state(runtime.export_state())
    restored.audit_state = lambda: (_ for _ in ()).throw(AssertionError("private state read"))
    # Controller evaluation is trusted; only forbid private reads inside context projection.
    assert build_context(restored, public_history(restored), max_chars=60000, max_turns=1) == expected
    restored = Runtime.from_state(runtime.export_state())
    captured = []
    class Recorder:
        def decide(self, context):
            captured.append(context)
            return Decision(finish_reason="probe")
    run_agent(restored, Recorder(), max_context_chars=60000, max_history_turns=1)
    assert captured == [expected]
    assert expected["public_memory"]["observations"]
    assert expected["public_memory"]["unresolved_rejections"][0]["result"]["error"] == "CROP_NOT_HARVESTABLE"
    assert not any(key in json.dumps(expected) for key in ("grain_dry_g_m2", "backend_state", "rue_g_mj_par"))


def test_public_memory_only_resolves_same_successful_request():
    runtime = Runtime(soybean_full_season())
    invoke(runtime, "plant", start_ridge=0, end_ridge=0, spacing_m=.079, depth_mm=40)
    invoke(runtime, "inspect_inventory")
    assert build_context(runtime, public_history(runtime), max_chars=60000)["public_memory"]["unresolved_rejections"]
    invoke(runtime, "prepare_seedbed", start_ridge=0, end_ridge=0)
    invoke(runtime, "plant", start_ridge=0, end_ridge=0, spacing_m=.079, depth_mm=40)
    assert not build_context(runtime, public_history(runtime), max_chars=60000)["public_memory"]["unresolved_rejections"]


def test_context_budget_counts_omissions_without_slicing_receipts():
    from fairy.events.records import canonical
    runtime = Runtime(soybean_irrigation())
    for _ in range(30):
        invoke(runtime, "observe_soil")
    history = public_history(runtime)
    for limit in (4000, 5000, 6000, 10000):
        context = build_context(runtime, history, max_chars=limit)
        assert len(canonical(context)) <= limit
        assert context["context_policy"]["history_omitted"] == 30-len(context["history"])
        assert all(turn in history for turn in context["history"])


@pytest.mark.parametrize("reason, calls, content, expected", [
    ("stop", [], "", "EMPTY_RESPONSE"),
    ("length", [], None, "INCOMPLETE_RESPONSE"),
    ("length", [("observe_soil", "{}")], None, "INCOMPLETE_TOOL_RESPONSE"),
    ("tool_calls", [("observe_soil", "{}"), ("observe_soil", "{}")], None, "MULTIPLE_TOOL_CALLS"),
    ("tool_calls", [("irrigate", "{")], None, "INVALID_ARGUMENT_JSON"),
    ("tool_calls", [("irrigate", "[]")], None, "ARGUMENTS_NOT_OBJECT"),
])
def test_protocol_failures_are_classified_without_farm_errors(reason, calls, content, expected):
    captured = []
    def create(**kwargs):
        captured.append(kwargs)
        return NS(usage=None, choices=[NS(finish_reason=reason, message=NS(content=content,
            tool_calls=[NS(function=NS(name=n, arguments=a)) for n, a in calls]))])
    model = LLMAdapter(model="mock", client=NS(chat=NS(completions=NS(create=create))))
    runtime = Runtime(soybean_irrigation())
    run_agent(runtime, model, max_protocol_errors=2)
    assert runtime.tool_errors == 0 and runtime.irrigation_used_mm == 0
    assert runtime.events[-1].payload["reason"] == "protocol_error_budget"
    assert all(e.kind != "operation_requested" for e in runtime.events)
    assert model.metadata["protocol_errors"] == 2
    audit = model.metadata["model_calls"][0]
    assert audit["protocol_error"] == expected and audit["effective_call_count"] == 0
    from fairy.events.records import canonical
    assert audit["request_json_utf8_bytes"] == len(canonical(captured[0]).encode())
    assert json.loads(captured[1]["messages"][1]["content"])["protocol_feedback"]["error"] == expected
    assert replay(artifact(runtime, model.metadata))[1]["replay_verified"]


def test_empty_choices_is_not_model_finish():
    client = NS(chat=NS(completions=NS(create=lambda **kw: NS(usage=None, choices=[]))))
    model = LLMAdapter(model="mock", client=client)
    decision = model.decide(build_context(Runtime(soybean_irrigation()), []))
    assert decision.protocol_error == "EMPTY_CHOICES" and decision.request is None


def test_read_loop_diagnosed_without_automatic_actions():
    class Reader:
        def decide(self, context):
            return Decision(OperationRequest("inspect_inventory"))
    runtime = Runtime(soybean_full_season())
    start = runtime.now
    run_agent(runtime, Reader(), max_steps=30, max_context_chars=60000)
    diagnostic = artifact(runtime)["diagnostics"]
    assert diagnostic["low_progress"] and diagnostic["management_actions"] == 0
    assert runtime.now == start and runtime.tool_errors == 0
    assert runtime.events[-1].payload["reason"] == "step_budget"


def test_inspection_charging_loop_and_long_wait_remain_legal():
    runtime = Runtime(soybean_full_season())
    for _ in range(8):
        assert invoke(runtime, "ground_inspect", start_ridge=0, end_ridge=0).ok
        assert invoke(runtime, "charge_equipment", equipment="robot").ok
    assert artifact(runtime)["diagnostics"]["low_progress"]
    assert invoke(runtime, "advance_time", hours=48).ok
    assert not artifact(runtime)["diagnostics"]["low_progress"]


def test_source_identity_and_no_clobber(tmp_path):
    runtime = Runtime(soybean_irrigation())
    record = artifact(runtime)
    source = record["environment"]["source"]
    assert source["fingerprint"] == record["state"]["source_fingerprint"]
    assert "fairy/events/session.py" in source["files"]
    state = runtime.export_state()
    state["source_fingerprint"] = "0" * 64
    with pytest.raises(ValueError, match="source fingerprint"):
        Runtime.from_state(state)
    path = tmp_path / "record.json"
    write_json(path, record)
    before = path.read_bytes()
    with pytest.raises(FileExistsError):
        write_json(path, {"replacement": True})
    assert path.read_bytes() == before


def test_prior_protocol_budget_is_enforced_without_another_request():
    class Forbidden:
        metadata = {"prior_usage": {"protocol_errors": 2}}
        def decide(self, context):
            raise AssertionError("Protocol budget exhausted")
    runtime = Runtime(soybean_irrigation())
    run_agent(runtime, Forbidden(), max_protocol_errors=2)
    assert runtime.events[-1].payload["reason"] == "protocol_error_budget"


def test_concurrent_artifact_publication_has_one_winner(tmp_path):
    path = tmp_path / "shared.json"
    def publish(number):
        try:
            write_json(path, {"number": number})
            return True
        except FileExistsError:
            return False
    with ThreadPoolExecutor(2) as pool:
        assert sum(pool.map(publish, [1, 2])) == 1
    assert load_json(path)["number"] in (1, 2)
    assert not list(tmp_path.glob("*.tmp"))


def test_cli_refuses_existing_artifact_before_engine_creation(tmp_path, monkeypatch):
    import interface.cli.__main__ as cli
    path = tmp_path / "existing.json"
    path.write_text("original evidence")
    monkeypatch.setattr(cli, "Runtime", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("engine constructed")))
    assert cli.main(["run", "--out", str(path)]) == 2
    assert path.read_text() == "original evidence"


def test_fixed_drought_stage_and_intervention_response():
    from evaluate.scenarios.lib.l3.validation import validate_drought_challenge
    from fairy.events.records import fingerprint
    report, records = validate_drought_challenge()
    assert report["valid"] and report["dry_grain_response_kg"] > 0
    assert fingerprint(records["control"]["state"]["scenario"]) == fingerprint(records["irrigated"]["state"]["scenario"])
    assert len(report["runs"]["control"]["target_stage_days"]) == 17
    assert report["runs"]["irrigated"]["metrics"]["water_used_mm"] == 4
