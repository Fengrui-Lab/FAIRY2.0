"""Independent acceptance tests for ontology loading and registered operations."""
from dataclasses import FrozenInstanceError, asdict, replace
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from evaluate.metrics.irrigation import evaluate
from evaluate.scenarios.lib.l1.soybean_irrigation import soybean_irrigation
from evaluate.scenarios.lib.l1.soybean_weather_irrigation import soybean_weather_irrigation
from fairy.acps.backends.biology.contracts import BiologyBackendRegistry
from fairy.acps.backends.biology.native.backend import NativeBackend
from fairy.acps.ontology.contracts import ObjectSpec, OperationBinding, OperationRequest, Parameter
from fairy.acps.ontology.service.weather import FORECAST
from fairy.agents.models.llm.adapter import LLMAdapter
from fairy.agents.loop.context.public import build_context
from fairy.agents.loop.controller.oracle import IrrigationOracle
from fairy.agents.loop.controller.run import run_agent
from fairy.events.artifacts import artifact, assert_equivalent, replay
from fairy.events.records import canonical
from fairy.events.registry import BoundOperation, OperationRegistry
from fairy.events.session import Runtime


@pytest.fixture(params=["native", "wofost"])
def backend(request):
    if request.param == "wofost":
        pytest.importorskip("pcse")
    return request.param


def call(runtime, name, **arguments):
    return runtime.invoke(OperationRequest(name, arguments))


def test_weather_scene_oracle_and_replay(backend):
    runtime = run_agent(Runtime(soybean_weather_irrigation(), backend), IrrigationOracle())
    assert evaluate(runtime)["task_success"]
    requested = [e.payload["tool"] for e in runtime.events if e.kind == "operation_requested"]
    assert requested[:2] == ["get_current_weather", "get_forecast"]
    assert len(runtime.operations.tool_definitions()) == 5
    _, metrics = replay(artifact(runtime))
    assert metrics["replay_verified"] and metrics["task_success"]


def test_three_operation_scene_remains_scoped_even_with_weather_object():
    spec = soybean_weather_irrigation()
    original = soybean_irrigation()
    runtime = Runtime(replace(spec, tools=original.tools, operation_bindings=original.operation_bindings, required_queries=()))
    assert len(runtime.operations.tool_definitions()) == 3
    before = runtime.audit_state()
    for name in ("get_current_weather", "get_forecast", "plot-1", "soil-1"):
        result = call(runtime, name)
        assert not result.ok and result.error == "UNKNOWN_TOOL" and not result.data
    assert runtime.audit_state() == before
    assert not hasattr(runtime.objects[spec.farm.plot_id], "operations")
    replay(artifact(runtime))


def test_public_weather_separate_from_forcing_and_context(backend):
    spec = soybean_weather_irrigation()
    reports = tuple(replace(w, rain_mm=12.345, tmin_c=11.25) for w in spec.public_weather)
    runtime = Runtime(replace(spec, public_weather=reports), backend)
    physical, now = runtime.backend.export_state(), runtime.now
    context = build_context(runtime, [])
    assert "weather" not in context
    for hidden in ("12.345", "storage_mm", "backend_state", "pcse", "gdd", "et0_mm"):
        assert hidden not in canonical(context)
    assert all(set(o) == {"object_id", "type_id", "target_id"} for o in context["objects"])
    assert {d["function"]["name"] for d in context["tools"]} == set(spec.tools)
    current = call(runtime, "get_current_weather")
    forecast = call(runtime, "get_forecast", days=7)
    assert current.data["weather"]["rain_mm"] == 12.345
    assert forecast.data["forecast"][0]["rain_mm"] == 12.345
    assert runtime.weather[runtime.now.date().isoformat()].rain_mm == 0
    assert forecast.data["available_days"] == 1 and forecast.data["requested_days"] == 7
    assert forecast.data["forecast"][0]["day"] == "2026-05-10"
    assert forecast.data["issued_at"] == "2026-05-09T00:00:00+08:00"
    assert call(runtime, "get_current_weather") == current
    soil = call(runtime, "observe_soil")
    assert call(runtime, "observe_soil") == soil
    assert runtime.now == now
    assert_equivalent(runtime.backend.export_state(), physical)
    replay(artifact(runtime))


def test_weather_uses_local_date_across_midnight(backend):
    runtime = Runtime(soybean_weather_irrigation(), backend)
    call(runtime, "advance_time", hours=17)
    assert call(runtime, "get_current_weather").data["weather"]["day"] == "2026-05-10"
    forecast = call(runtime, "get_forecast", days=1).data
    assert forecast["forecast"] == [] and forecast["available_days"] == 0
    assert forecast["issued_at"] == "2026-05-10T00:00:00+08:00"
    replay(artifact(runtime))


@pytest.mark.parametrize("arguments", [{}, {"days": 0}, {"days": 8}, {"days": True},
                                       {"days": 1.5}, {"days": "1"}, {"days": 1, "audit": True}])
def test_forecast_invalid_arguments_match_schema_without_effects(arguments):
    runtime = Runtime(soybean_weather_irrigation())
    state, now = runtime.backend.export_state(), runtime.now
    schema = FORECAST.tool_schema()["function"]["parameters"]
    assert schema["properties"]["days"] == {"type": "integer", "minimum": 1, "maximum": 7}
    assert schema["required"] == ["days"] and schema["additionalProperties"] is False
    result = runtime.invoke(OperationRequest("get_forecast", arguments))
    assert not result.ok and result.error == "INVALID_ARGUMENTS" and result.data == {}
    assert runtime.now == now
    assert runtime.backend.export_state() == state
    replay(artifact(runtime))


def invalid_specs():
    spec = soybean_irrigation()
    yield replace(spec, objects=spec.objects + (spec.objects[1],))
    yield replace(spec, objects=spec.objects + (ObjectSpec("runtime", "plot"),))
    yield replace(spec, objects=spec.objects + (ObjectSpec("unknown", "magic", "plot-1"),))
    yield replace(spec, objects=(spec.objects[0], replace(spec.objects[1], target_id="missing"), spec.objects[2]))
    yield replace(spec, objects=spec.objects[1:])
    yield replace(spec, operation_bindings=(OperationBinding("observe_soil", "missing"), *spec.operation_bindings[1:]))
    yield replace(spec, operation_bindings=(OperationBinding("observe_soil", "irrigation-1"), *spec.operation_bindings[1:]))
    yield replace(spec, operation_bindings=(OperationBinding("observe_soil", "plot-1"), *spec.operation_bindings[1:]))
    yield replace(spec, operation_bindings=(OperationBinding("observe_soil", "runtime"), *spec.operation_bindings[1:]))


@pytest.mark.parametrize("spec", list(invalid_specs()))
def test_invalid_object_and_binding_fail_before_backend_construction(spec, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Invalid definitions must fail before engine construction")
    monkeypatch.setattr("fairy.events.session.make_backend", forbidden)
    with pytest.raises(ValueError):
        Runtime(spec)


@pytest.mark.parametrize("changes", [{"actions": ()}, {"crops": ("rice",)}, {"timestep": "hour"}])
def test_unsupported_backend_capability_fails_before_warmup(changes, monkeypatch):
    monkeypatch.setattr(NativeBackend, "capabilities", replace(NativeBackend.capabilities, **changes))
    def forbidden(*args, **kwargs):
        raise AssertionError("No warmup on incompatible backend")
    monkeypatch.setattr(NativeBackend, "advance_day", forbidden)
    with pytest.raises(ValueError, match="Backend"):
        Runtime(soybean_irrigation())


def test_registry_rejects_duplicate_names_and_detaches_public_schema():
    binding = BoundOperation(FORECAST, "weather-1", lambda args, cause: {})
    with pytest.raises(ValueError, match="Duplicate operation"):
        OperationRegistry((binding, binding))
    registry = OperationRegistry((binding,))
    schemas = registry.tool_definitions()
    schemas[0]["function"]["parameters"]["properties"]["days"]["maximum"] = 999
    assert registry.tool_definitions()[0]["function"]["parameters"]["properties"]["days"]["maximum"] == 7
    with pytest.raises(ValueError, match="Duplicate backend"):
        BiologyBackendRegistry((("native", NativeBackend), ("native", NativeBackend)))
    spec = soybean_irrigation()
    with pytest.raises(ValueError, match="identity"):
        BiologyBackendRegistry((("other", NativeBackend),)).create("other", spec.farm, spec.crop, spec.sowing_date)


def test_object_specs_are_immutable_and_public_view_detached():
    runtime = Runtime(soybean_irrigation())
    with pytest.raises(FrozenInstanceError):
        runtime.scenario.objects[1].target_id = "bad"
    with pytest.raises(TypeError):
        runtime.objects["extra"] = object()
    view = runtime.public_objects()
    view[0]["object_id"] = "changed"
    assert runtime.public_objects()[0]["object_id"] == "plot-1"


def test_weather_binding_restores_in_fresh_process(backend, tmp_path):
    runtime = Runtime(soybean_weather_irrigation(), backend)
    call(runtime, "get_current_weather")
    call(runtime, "get_forecast", days=1)
    call(runtime, "observe_soil")
    call(runtime, "irrigate", depth_mm=15)
    call(runtime, "advance_time", hours=5)
    path = tmp_path / "pending.json"
    path.write_text(canonical(runtime.export_state()))
    script = """
import json,sys
from fairy.events.session import Runtime
from fairy.acps.ontology.contracts import OperationRequest
r=Runtime.from_state(json.load(open(sys.argv[1])))
for tool,args in [('advance_time', {'hours':19}), ('get_current_weather', {}), ('observe_soil', {})]:
    assert r.invoke(OperationRequest(tool,args)).ok
print(json.dumps(r.export_state()))
"""
    completed = subprocess.run([sys.executable, "-c", script, str(path)], capture_output=True, text=True, check=True)
    call(runtime, "advance_time", hours=19)
    call(runtime, "get_current_weather")
    call(runtime, "observe_soil")
    assert_equivalent(runtime.export_state(), json.loads(completed.stdout))
    assert runtime.pending_irrigation_mm == 0
    assert runtime.audit_state()["irrigation_mm"] == 15


def test_restore_and_replay_reject_binding_fingerprint_drift():
    runtime = Runtime(soybean_weather_irrigation())
    state = runtime.export_state()
    state["binding_fingerprint"] = "changed"
    with pytest.raises(ValueError, match="binding fingerprint"):
        Runtime.from_state(state)
    record = artifact(runtime)
    record["state"]["binding_fingerprint"] = "changed"
    with pytest.raises(ValueError):
        replay(record)


def test_operation_bindings_follow_replacement_backend(backend, monkeypatch):
    runtime = Runtime(soybean_weather_irrigation(), backend)
    original = runtime.backend
    call(runtime, "irrigate", depth_mm=15)
    old_advance = original.advance_day
    def failing(*args):
        old_advance(*args)
        raise RuntimeError("synthetic failure after integration")
    monkeypatch.setattr(original, "advance_day", failing)
    with pytest.raises(RuntimeError, match="synthetic failure"):
        call(runtime, "advance_time", hours=24)
    assert runtime.backend is not original
    def stale_observe():
        raise AssertionError("Provider retained replaced engine")
    monkeypatch.setattr(original, "observe", stale_observe)
    assert call(runtime, "observe_soil").ok
    assert call(runtime, "get_forecast", days=1).ok
    assert call(runtime, "advance_time", hours=24).ok
    assert call(runtime, "observe_soil").ok
    assert runtime.audit_state()["irrigation_mm"] == 15


def test_cli_weather_scene_run_and_replay(backend, tmp_path, capsys):
    from interface.cli.__main__ import main
    path = tmp_path / "weather.json"
    assert main(["run", "--scenario", "soybean_l1_weather_irrigation_v1", "--backend", backend, "--out", str(path)]) == 0
    assert main(["replay", str(path)]) == 0
    stored = json.loads(path.read_text())
    assert stored["state"]["scenario"]["weather_in_context"] is False


@pytest.mark.parametrize("query_mode", ["none", "invalid", "late", "before"])
def test_weather_task_requires_successful_queries_before_irrigation(query_mode):
    runtime = Runtime(soybean_weather_irrigation())
    if query_mode in ("invalid", "before"):
        call(runtime, "get_current_weather")
        call(runtime, "get_forecast", days=0 if query_mode == "invalid" else 1)
    call(runtime, "observe_soil")
    call(runtime, "irrigate", depth_mm=40)
    if query_mode == "late":
        call(runtime, "get_current_weather")
        call(runtime, "get_forecast", days=1)
    call(runtime, "advance_time", hours=24)
    call(runtime, "observe_soil")
    metrics = evaluate(runtime)
    assert metrics["target_reached"] and metrics["observed_after_settlement"]
    assert metrics["required_queries_completed"] is (query_mode == "before")
    assert metrics["task_success"] is (query_mode == "before")
    replay(artifact(runtime))


def test_replay_binding_drift_rejected_before_warmup(monkeypatch):
    record = artifact(Runtime(soybean_weather_irrigation()))
    record["state"]["binding_fingerprint"] = "changed"
    def forbidden(*args, **kwargs):
        raise AssertionError("Incompatible bindings must fail before pre-run")
    monkeypatch.setattr(NativeBackend, "advance_day", forbidden)
    with pytest.raises(ValueError, match="binding fingerprint"):
        replay(record)


def test_old_runtime_schema_is_explicitly_rejected():
    runtime = Runtime(soybean_irrigation())
    state = runtime.export_state()
    state["schema"] = 1
    with pytest.raises(ValueError, match="Incompatible"):
        Runtime.from_state(state)
    record = artifact(runtime)
    record["state"]["schema"] = 1
    with pytest.raises(ValueError, match="Incompatible"):
        replay(record)


@pytest.mark.parametrize("changes", [
    {"name": ""}, {"name": "not-a-field"}, {"name": 1}, {"kind": "unsupported"},
    {"kind": "string", "minimum": 0}, {"minimum": float("nan")},
    {"maximum": float("inf")}, {"minimum": True},
    {"minimum": 2, "maximum": 1}, {"exclusive_minimum": 2, "maximum": 2},
    {"kind": "integer", "minimum": 0.1, "maximum": 0.9},
    {"kind": "integer", "exclusive_minimum": 1, "maximum": 1.9},
    {"unit": ""},
])
def test_invalid_parameter_contracts_fail_at_declaration(changes):
    declaration = {"name": "amount", "kind": "number", **changes}
    with pytest.raises(ValueError):
        Parameter(**declaration)


@pytest.mark.parametrize("changes", [
    {"name": "bad-name"}, {"description": " "}, {"timing": "implicit_clock"},
    {"version": 0}, {"version": True}, {"version": 1.5},
    {"required_backend_actions": ["irrigate"]},
    {"required_backend_actions": ("irrigate", "irrigate")},
    {"required_backend_actions": (1,)},
    {"parameters": [Parameter("days", "integer")]},
    {"parameters": (Parameter("days", "integer"), Parameter("days", "number"))},
    {"results": ({"name": "data"},)},
])
def test_invalid_operation_contracts_fail_at_declaration(changes):
    with pytest.raises(ValueError):
        replace(FORECAST, **changes)


@pytest.mark.parametrize("name,provider", [("", "weather-1"), ("get_forecast", " "),
                                          (None, "weather-1"), ("get_forecast", [])])
def test_invalid_binding_names_rejected(name, provider):
    with pytest.raises(ValueError):
        OperationBinding(name, provider)


def test_integer_bound_contract_accepts_single_feasible_value():
    parameter = Parameter("count", "integer", minimum=0.1, maximum=1.1, exclusive_minimum=0.5)
    parameter.validate(1)
    for invalid in (0, 2, True):
        with pytest.raises(ValueError):
            parameter.validate(invalid)


def test_mock_llm_weather_scene_uses_only_exposed_contracts(backend):
    runtime = Runtime(soybean_weather_irrigation(), backend)
    depth = 40 if backend == "native" else 16
    steps = [("get_current_weather", {}), ("get_forecast", {"days": 1}),
             ("observe_soil", {}), ("irrigate", {"depth_mm": depth}),
             ("advance_time", {"hours": 24}), ("observe_soil", {}), None]
    requests = []

    def create(**kwargs):
        requests.append(kwargs)
        step = steps[len(requests) - 1]
        calls = [] if step is None else [SimpleNamespace(function=SimpleNamespace(
            name=step[0], arguments=json.dumps(step[1])))]
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=calls,
                                      content="Completed" if step is None else None),
                                     finish_reason="stop" if step is None else "tool_calls")],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5))

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    agent = LLMAdapter(model="offline-weather-test", client=client)
    run_agent(runtime, agent)
    assert evaluate(runtime)["task_success"] and evaluate(runtime)["required_queries_completed"]
    assert agent.metadata["calls"] == 6 and agent.metadata["privileged"] is False
    for request in requests:
        assert request["parallel_tool_calls"] is False
        assert {tool["function"]["name"] for tool in request["tools"]} == set(runtime.scenario.tools)
        context = json.loads(request["messages"][1]["content"])
        assert "weather" not in context
        assert all(set(obj) == {"object_id", "type_id", "target_id"} for obj in context["objects"])
        for hidden in ("storage_mm", "backend_state", "pcse", "gdd", "TSUM", "audit"):
            assert hidden not in canonical(context)
    first = json.loads(requests[0]["messages"][1]["content"])
    after_current = json.loads(requests[1]["messages"][1]["content"])
    after_forecast = json.loads(requests[2]["messages"][1]["content"])
    assert "history" not in first and len(requests[0]["messages"]) == 2
    current_results = [json.loads(m["content"]) for m in requests[1]["messages"] if m["role"] == "tool"]
    forecast_results = [json.loads(m["content"]) for m in requests[2]["messages"] if m["role"] == "tool"]
    assert current_results[0]["data"]["weather"]["day"] == "2026-05-09"
    assert forecast_results[1]["data"]["forecast"][0]["day"] == "2026-05-10"
    replay(artifact(runtime, agent.metadata))
