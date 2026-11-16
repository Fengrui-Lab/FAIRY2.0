"""Acceptance tests live in the existing demo directory; no new top-level tree."""
from __future__ import annotations
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace as NS
from dataclasses import asdict, replace
import pytest

from evaluate.scenarios.lib.l1.soybean_irrigation import soybean_irrigation
from evaluate.metrics.irrigation import evaluate
from fairy.acps.ontology.contracts import OperationRequest
from fairy.agents.loop.context.public import build_context
from fairy.agents.loop.controller.oracle import IrrigationOracle
from fairy.agents.loop.controller.run import run_agent
from fairy.agents.models.decision import Decision
from fairy.agents.models.llm.adapter import LLMAdapter
from fairy.events.artifacts import artifact, replay, assert_equivalent
from fairy.events.records import canonical
from fairy.events.session import Runtime


@pytest.fixture(params=["native", "wofost"])
def backend(request):
    if request.param == "wofost":
        pytest.importorskip("pcse")
    return request.param


def call(runtime, tool, **arguments):
    return runtime.invoke(OperationRequest(tool, arguments))


def test_oracle_success_control_and_strict_replay(backend):
    runtime = run_agent(Runtime(soybean_irrigation(), backend), IrrigationOracle())
    metrics = evaluate(runtime)
    assert metrics["task_success"] and metrics["tool_errors"] == 0
    assert 0 < metrics["water_used_mm"] <= 45
    assert metrics["water_used_m3"] == metrics["water_used_mm"]
    reproduced, report = replay(artifact(runtime, IrrigationOracle.metadata))
    assert report["replay_verified"] is True
    assert_equivalent(runtime.audit_state(), reproduced.audit_state())
    control = Runtime(soybean_irrigation(), backend)
    call(control, "observe_soil")
    call(control, "advance_time", hours=24)
    call(control, "observe_soil")
    assert not evaluate(control)["task_success"]
    assert control.audit_state()["root_vwc"] < runtime.audit_state()["root_vwc"]


def test_observation_is_pure_and_event_payload_is_immutable(backend):
    runtime = Runtime(soybean_irrigation(), backend)
    physical, clock = runtime.backend.export_state(), runtime.now
    first = call(runtime, "observe_soil")
    second = call(runtime, "observe_soil")
    assert first == second
    assert_equivalent(physical, runtime.backend.export_state())
    assert runtime.now == clock
    payload = runtime.events[-1].payload
    payload["data"]["root_vwc"] = -1
    assert runtime.events[-1].payload["data"]["root_vwc"] >= 0


@pytest.mark.parametrize("tool,args,error", [
    ("irrigate", {"depth_mm": -1}, "INVALID_ARGUMENTS"),
    ("irrigate", {"depth_mm": 0}, "INVALID_ARGUMENTS"),
    ("irrigate", {"depth_mm": True}, "INVALID_ARGUMENTS"),
    ("irrigate", {"depth_mm": "20"}, "INVALID_ARGUMENTS"),
    ("irrigate", {"depth_mm": 10 ** 1000}, "INVALID_ARGUMENTS"),
    ("irrigate", {"depth_mm": 46}, "WATER_QUOTA_EXCEEDED"),
    ("irrigate", {"depth_mm": 10, "plot_id": "secret"}, "INVALID_ARGUMENTS"),
    ("advance_time", {"hours": 25}, "TIME_BUDGET_EXCEEDED"),
    ("advance_time", {"hours": 0}, "INVALID_ARGUMENTS"),
    ("advance_time", {"hours": True}, "INVALID_ARGUMENTS"),
    ("observe_soil", {"audit": True}, "INVALID_ARGUMENTS"),
    ("audit_state", {}, "UNKNOWN_TOOL"),
])
def test_rejections_have_no_physical_effect(backend, tool, args, error):
    runtime = Runtime(soybean_irrigation(), backend)
    physical, clock = runtime.backend.export_state(), runtime.now
    result = runtime.invoke(OperationRequest(tool, args))
    assert result.ok is False and result.error == error and result.data == {}
    assert runtime.now == clock and runtime.irrigation_used_mm == runtime.pending_irrigation_mm == 0
    assert_equivalent(physical, runtime.backend.export_state())
    # Rejected calls are part of the reproducible trajectory too.
    replay(artifact(runtime))


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_json_request_is_rejected_at_boundary(value):
    runtime = Runtime(soybean_irrigation())
    before = runtime.export_state()
    with pytest.raises(ValueError):
        call(runtime, "irrigate", depth_mm=value)
    assert runtime.export_state() == before


def test_cumulative_quota_and_pending_settle_once(backend):
    runtime = Runtime(soybean_irrigation(), backend)
    call(runtime, "irrigate", depth_mm=10)
    call(runtime, "irrigate", depth_mm=20)
    before = runtime.audit_state()
    assert not call(runtime, "irrigate", depth_mm=16).ok
    assert runtime.audit_state() == before
    assert runtime.pending_irrigation_mm == runtime.irrigation_used_mm == 30
    call(runtime, "advance_time", hours=17)
    assert runtime.pending_irrigation_mm == 0
    assert runtime.audit_state()["irrigation_mm"] == 30
    call(runtime, "advance_time", hours=7)
    assert runtime.audit_state()["irrigation_mm"] == 30


def test_midnight_and_split_advancement(backend):
    whole = Runtime(soybean_irrigation(), backend)
    split = Runtime(soybean_irrigation(), backend)
    for runtime in (whole, split):
        call(runtime, "irrigate", depth_mm=15)
    before = split.audit_state()
    call(split, "advance_time", hours=16 + 59 / 60)
    assert split.now.hour == 23 and split.now.minute == 59
    assert split.audit_state() == before
    call(split, "observe_soil")
    call(split, "advance_time", hours=1 / 60)
    assert split.now.hour == split.now.minute == 0
    assert split.audit_state()["next_day"] == "2026-05-10"
    after = split.audit_state()
    call(split, "advance_time", hours=7)
    assert split.audit_state() == after
    call(whole, "advance_time", hours=24)
    assert_equivalent(split.audit_state(), whole.audit_state())
    assert len([e for e in split.events if e.kind == "environment_day_completed"]) == 1


def test_restore_pending_and_continue_in_new_process(backend, tmp_path):
    runtime = Runtime(soybean_irrigation(), backend)
    call(runtime, "observe_soil")
    call(runtime, "irrigate", depth_mm=15)
    call(runtime, "advance_time", hours=5)
    path = tmp_path / "state.json"
    path.write_text(canonical(runtime.export_state()))
    script = """
import json,sys
from fairy.events.session import Runtime
from fairy.acps.ontology.contracts import OperationRequest
r=Runtime.from_state(json.load(open(sys.argv[1])))
r.invoke(OperationRequest('advance_time', {'hours':19}))
r.invoke(OperationRequest('observe_soil'))
print(json.dumps(r.export_state()))
"""
    completed = subprocess.run([sys.executable, "-c", script, str(path)], capture_output=True, text=True, check=True)
    call(runtime, "advance_time", hours=19)
    call(runtime, "observe_soil")
    assert_equivalent(runtime.export_state(), json.loads(completed.stdout))
    restored = Runtime.from_state(json.loads(completed.stdout))
    assert restored.pending_irrigation_mm == 0
    assert restored.audit_state()["irrigation_mm"] == 15


def test_restore_after_settlement(backend):
    runtime = Runtime(soybean_irrigation(), backend)
    call(runtime, "irrigate", depth_mm=15)
    call(runtime, "advance_time", hours=17)
    restored = Runtime.from_state(runtime.export_state())
    call(restored, "advance_time", hours=7)
    assert restored.audit_state()["irrigation_mm"] == 15
    assert_equivalent(restored.audit_state(), runtime.audit_state())


def test_water_balance_daily_and_area_conversion(backend):
    runtime = Runtime(soybean_irrigation(), backend)
    call(runtime, "irrigate", depth_mm=20)
    call(runtime, "advance_time", hours=24)
    flux = next(e.payload["flux"] for e in runtime.events if e.kind == "environment_day_completed")
    assert abs(flux["balance_error_mm"]) < 1e-8
    assert flux["application_loss_mm"] == pytest.approx(1.6)
    farm = replace(runtime.scenario.farm, length_m=20, width_m=5)
    assert farm.volume_m3(20) == 2


def test_native_rain_overflow_and_storage_balance():
    from fairy.acps.backends.biology.contracts import WeatherDay, make_backend
    spec = soybean_irrigation()
    native = make_backend("native", spec.farm, spec.crop, spec.sowing_date)
    flux = native.advance_day(WeatherDay(spec.sowing_date, rain_mm=200), 45)
    assert flux["runoff_mm"] > 0 and flux["drainage_mm"] > 0
    assert abs(flux["balance_error_mm"]) < 1e-8
    assert spec.farm.wilting_point_vwc <= native.audit()["root_vwc"] <= spec.farm.field_capacity_vwc
    assert native.audit()["gdd"] == 14


def test_engine_failure_rolls_back_physics_and_pending_input(backend, monkeypatch):
    runtime = Runtime(soybean_irrigation(), backend)
    call(runtime, "irrigate", depth_mm=15)
    before, now = runtime.audit_state(), runtime.now
    original = runtime.backend.advance_day
    def fail_after_integration(*args):
        original(*args)
        raise RuntimeError("injected backend fault")
    monkeypatch.setattr(runtime.backend, "advance_day", fail_after_integration)
    with pytest.raises(RuntimeError, match="injected"):
        call(runtime, "advance_time", hours=24)
    assert runtime.now == now and runtime.pending_irrigation_mm == 15
    assert_equivalent(before, runtime.audit_state())
    assert runtime.events[-1].kind == "engine_failed"
    assert not any(e.kind == "environment_day_completed" for e in runtime.events)
    call(runtime, "advance_time", hours=24)
    assert runtime.audit_state()["irrigation_mm"] == 15


def test_native_needs_no_optional_packages():
    script = """
import sys,importlib.abc
class Block(importlib.abc.MetaPathFinder):
 def find_spec(self, fullname, path=None, target=None):
  if fullname.split('.')[0] in {'pcse','openai','yaml','numpy','pandas'}:
   raise ImportError('blocked optional dependency')
sys.meta_path.insert(0, Block())
from fairy.events.session import Runtime
from evaluate.scenarios.lib.l1.soybean_irrigation import soybean_irrigation
from fairy.agents.loop.controller.run import run_agent
from fairy.agents.loop.controller.oracle import IrrigationOracle
from evaluate.metrics.irrigation import evaluate
r=run_agent(Runtime(soybean_irrigation()), IrrigationOracle())
assert evaluate(r)['task_success']
try:
 Runtime(soybean_irrigation(), 'wofost')
except RuntimeError as exc:
 assert 'optional dependency' in str(exc)
else:
 raise AssertionError('WOFOST must not fall back')
"""
    subprocess.run([sys.executable, "-c", script], check=True, capture_output=True, text=True)


def test_multi_day_segmented_advancement(backend):
    from fairy.acps.backends.biology.contracts import WeatherDay
    spec = soybean_irrigation()
    spec = replace(spec, deadline="2026-05-11T07:00:00+08:00",
                   weather=spec.weather + (WeatherDay("2026-05-11"),))
    whole, split = Runtime(spec, backend), Runtime(spec, backend)
    for runtime in (whole, split):
        call(runtime, "irrigate", depth_mm=15)
    call(whole, "advance_time", hours=48)
    call(split, "advance_time", hours=24)
    call(split, "advance_time", hours=24)
    assert_equivalent(whole.audit_state(), split.audit_state())
    assert whole.audit_state()["irrigation_mm"] == 15
    fluxes = [e.payload["flux"] for e in whole.events if e.kind == "environment_day_completed"]
    assert len(fluxes) == 2 and fluxes[1]["irrigation_mm"] == 0
    assert all(abs(f["balance_error_mm"]) < 1e-8 for f in fluxes)


def test_fingerprint_version_and_clock_guards(backend):
    runtime = Runtime(soybean_irrigation(), backend)
    for field, replacement in (("scenario_fingerprint", "wrong"), ("fairy_version", "0"),
                               ("now", "2026-05-10T00:00:00+08:00")):
        state = runtime.export_state()
        state[field] = replacement
        with pytest.raises(ValueError):
            Runtime.from_state(state)
    state = runtime.export_state()
    state["backend_state"]["capabilities"]["version"] = "bad"
    with pytest.raises(ValueError):
        Runtime.from_state(state)
    if backend == "wofost":
        state = runtime.export_state()
        state["backend_state"]["crop_data_sha256"] = "bad"
        with pytest.raises(ValueError):
            Runtime.from_state(state)


def test_replay_detects_tampered_receipt_and_audit(backend):
    runtime = Runtime(soybean_irrigation(), backend)
    call(runtime, "observe_soil")
    record = artifact(runtime)
    record["state"]["events"][-1]["payload"]["data"]["root_vwc"] = 0.9
    with pytest.raises(ValueError, match="divergence"):
        replay(record)
    record = artifact(runtime)
    record["audit"]["root_vwc"] = 0.9
    with pytest.raises(ValueError, match="divergence"):
        replay(record)


def test_unknown_backend_fails_without_fallback():
    with pytest.raises(ValueError, match="Unsupported backend"):
        Runtime(soybean_irrigation(), "not-a-backend")


def test_public_context_is_bounded_detached_and_allowlisted(backend):
    runtime = Runtime(soybean_irrigation(), backend)
    history = []
    for _ in range(30):
        result = call(runtime, "observe_soil")
        history.append({"request": {"tool": "observe_soil", "arguments": {}}, "result": asdict(result)})
    before = runtime.export_state()
    context = build_context(runtime, history, max_chars=5000)
    assert len(canonical(context)) <= 5000 and len(context["history"]) <= 8
    text = canonical(context)
    for hidden in ("storage_mm", "pcse", "backend_state", "journal", "gdd", "audit", "TSUM"):
        assert hidden not in text
    context["status"]["pending_irrigation_mm"] = 500
    assert runtime.export_state() == before
    with pytest.raises(ValueError, match="character budget"):
        build_context(runtime, history, max_chars=10)


class FakeClient:
    def __init__(self, depth=40, responses=None):
        self.inputs = []
        self.depth = depth
        self.responses = responses
        self.chat = NS(completions=NS(create=self.create))

    def create(self, **kwargs):
        self.inputs.append(kwargs)
        index = len(self.inputs) - 1
        steps = self.responses or [("observe_soil", "{}"), ("irrigate", json.dumps({"depth_mm": self.depth})),
                                   ("advance_time", '{"hours":24}'), ("observe_soil", "{}"), None]
        step = steps[min(index, len(steps) - 1)]
        calls = [] if step is None else [NS(function=NS(name=step[0], arguments=step[1]))]
        return NS(choices=[NS(message=NS(tool_calls=calls, content="完成" if step is None else None),
                              finish_reason="stop" if step is None else "tool_calls")],
                  usage=NS(prompt_tokens=10, completion_tokens=5))


def test_llm_mock_end_to_end_without_credentials(backend):
    runtime = Runtime(soybean_irrigation(), backend)
    client = FakeClient(depth=40 if backend == "native" else 16)
    adapter = LLMAdapter(model="test-model", client=client)
    run_agent(runtime, adapter)
    assert evaluate(runtime)["task_success"]
    assert adapter.metadata["calls"] == 4 and adapter.metadata["input_tokens"] == 40
    assert adapter.metadata["privileged"] is False
    for request in client.inputs:
        assert len(request["tools"]) == 3 and request["parallel_tool_calls"] is False
        assert "backend_state" not in str(request) and "pcse" not in str(request)
    replay(artifact(runtime, adapter.metadata))


@pytest.mark.parametrize("raw", ["{", "[]", '{"depth_mm":NaN}', '{"depth_mm":Infinity}', '{"depth_mm":1e999}'])
def test_malformed_llm_arguments_are_bounded_and_replayable(raw):
    runtime = Runtime(soybean_irrigation())
    agent = LLMAdapter(model="mock", client=FakeClient(responses=[("irrigate", raw)]))
    run_agent(runtime, agent, max_errors=2)
    assert runtime.tool_errors == 2 and runtime.irrigation_used_mm == 0
    assert runtime.events[-1].payload["reason"] == "tool_error_budget"
    replay(artifact(runtime, agent.metadata))


def test_step_budget():
    class ObserveForever:
        def decide(self, context):
            return Decision(OperationRequest("observe_soil"))
    runtime = Runtime(soybean_irrigation())
    run_agent(runtime, ObserveForever(), max_steps=2)
    assert runtime.events[-1].payload["reason"] == "step_budget"
    assert not evaluate(runtime)["task_success"]


def test_real_sdk_serializes_public_tool_request_without_network():
    httpx = pytest.importorskip("httpx")
    openai = pytest.importorskip("openai")
    received = []
    def handler(request):
        received.append(json.loads(request.content))
        return httpx.Response(200, json={
            "id": "mock-response", "object": "chat.completion", "created": 0,
            "model": "test-model", "choices": [{"index": 0, "finish_reason": "tool_calls",
            "message": {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": "observe_soil", "arguments": "{}"}}]}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
        })
    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        client = openai.OpenAI(api_key="test-only-key", base_url="https://mock.invalid/v1", http_client=http_client)
        adapter = LLMAdapter(model="test-model", client=client)
        decision = adapter.decide(build_context(Runtime(soybean_irrigation()), []))
    assert decision.request == OperationRequest("observe_soil")
    assert received[0]["max_completion_tokens"] == 512
    assert "test-only-key" not in canonical(received)
    assert adapter.metadata["calls"] == 1


def test_cli_saves_trace_on_model_service_failure(tmp_path, monkeypatch, capsys):
    from interface.cli.__main__ import main
    def fail(self, context):
        raise RuntimeError("synthetic service fault")
    monkeypatch.setattr(LLMAdapter, "__init__", lambda self, **kwargs: setattr(self, "metadata", {"kind": "mock_failure"}))
    monkeypatch.setattr(LLMAdapter, "decide", fail)
    path = tmp_path / "failure.json"
    assert main(["run", "--agent", "llm", "--model", "mock", "--out", str(path)]) == 2
    record = json.loads(path.read_text())
    assert record["state"]["done"] is True
    assert record["state"]["events"][-1]["payload"]["reason"] == "execution_error"
    assert record["metrics"]["task_success"] is False


def test_inspect_never_constructs_backend(tmp_path, monkeypatch, capsys):
    from interface.cli.__main__ import main
    path = tmp_path / "artifact.json"
    path.write_text('{"schema":1,"stored":"only"}')
    def forbidden(*args, **kwargs):
        raise AssertionError("must not build engine")
    monkeypatch.setattr(Runtime, "__init__", forbidden)
    assert main(["inspect", str(path)]) == 0
    assert "only" in capsys.readouterr().out


def test_cli_run_replay_evaluate(backend, tmp_path):
    from interface.cli.__main__ import main
    path = tmp_path / "run.json"
    assert main(["run", "--backend", backend, "--out", str(path)]) == 0
    assert main(["replay", str(path)]) == 0
    assert main(["evaluate", str(path)]) == 0


def test_source_has_no_legacy_runtime_imports():
    for root in ("fairy", "evaluate", "interface"):
        for path in Path(root).rglob("*.py"):
            assert "reference/" not in path.read_text(), str(path)
            assert "from reference" not in path.read_text(), str(path)
