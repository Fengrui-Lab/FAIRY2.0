"""Trusted local JSON artifacts, deterministic re-execution and strict comparison."""
from __future__ import annotations
import json
from importlib.metadata import PackageNotFoundError, version
import platform
from math import isclose
from pathlib import Path
from fairy.acps.ontology.contracts import OperationRequest
from fairy.events.records import detached, fingerprint
from fairy.events.session import Runtime
from evaluate.metrics import evaluate
from evaluate.scenarios.spec import ScenarioSpec


def artifact(runtime, agent_metadata=None):
    installed = {}
    for name in ("pcse", "openai", "numpy", "pandas", "PyYAML"):
        try:
            installed[name] = version(name)
        except PackageNotFoundError:
            pass
    return {"schema": 1, "state": runtime.export_state(), "metrics": evaluate(runtime),
            "agent": detached(agent_metadata or {}), "audit": runtime.audit_state(),
            "environment": {"python": platform.python_version(), "packages": installed}}


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def assert_equivalent(expected, actual, path="root"):
    # Stable engine/parameter versions are checked independently of numeric tolerance.
    if isinstance(expected, bool) or isinstance(actual, bool):
        if type(expected) is not type(actual) or expected != actual:
            raise ValueError(f"Replay divergence at {path}")
    elif isinstance(expected, int) and isinstance(actual, int):
        if expected != actual:
            raise ValueError(f"Replay integer differs at {path}")
    elif isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        try:
            matches = isclose(expected, actual, rel_tol=1e-10, abs_tol=1e-9)
        except OverflowError:
            matches = False
        if not matches:
            raise ValueError(f"Replay divergence at {path}: {expected} != {actual}")
    elif isinstance(expected, dict) and isinstance(actual, dict):
        if expected.keys() != actual.keys():
            raise ValueError(f"Replay keys differ at {path}")
        for key in expected:
            assert_equivalent(expected[key], actual[key], f"{path}.{key}")
    elif isinstance(expected, list) and isinstance(actual, list):
        if len(expected) != len(actual):
            raise ValueError(f"Replay lengths differ at {path}")
        for index, (left, right) in enumerate(zip(expected, actual)):
            assert_equivalent(left, right, f"{path}[{index}]")
    elif expected != actual:
        raise ValueError(f"Replay divergence at {path}")


def replay(record):
    state = record["state"]
    from fairy import __version__
    if record.get("schema") != 1 or state.get("schema") != 4 or state.get("fairy_version") != __version__:
        raise ValueError("Incompatible artifact version")
    if fingerprint(state["scenario"]) != state["scenario_fingerprint"]:
        raise ValueError("Scenario fingerprint mismatch")
    if not isinstance(state.get("binding_fingerprint"), str):
        raise ValueError("Missing operation binding fingerprint")
    runtime = Runtime(ScenarioSpec.from_dict(state["scenario"]), state["backend"],
                      expected_binding_fingerprint=state["binding_fingerprint"])
    for event in state["events"]:
        if event["kind"] == "operation_requested":
            payload = event["payload"]
            runtime.invoke(OperationRequest(payload["tool"], payload["arguments"]), actor=payload["actor"])
        elif event["kind"] == "run_terminated":
            runtime.finish(event["payload"]["reason"])
    assert_equivalent(state, runtime.export_state())
    assert_equivalent(record["audit"], runtime.audit_state())
    assert_equivalent(record["metrics"], evaluate(runtime))
    return runtime, evaluate(runtime, replay_verified=True)
