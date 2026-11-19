"""Trusted local JSON artifacts, deterministic re-execution and strict comparison."""

from __future__ import annotations
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from evaluate.metrics import TaskMetrics
    from fairy.acps.ontology.contracts import JsonObject
import json
from importlib.metadata import PackageNotFoundError, version
import platform
import os
import tempfile
from math import isclose
from pathlib import Path
from fairy.acps.ontology.contracts import OperationRequest
from fairy.events.records import detached, fingerprint
from fairy.events.session import Runtime
from evaluate.metrics import evaluate
from evaluate.scenarios.spec import ScenarioSpec


def artifact(runtime: Runtime, agent_metadata: JsonObject | None = None) -> JsonObject:
    """保存输入、状态、审计、指标和Agent元数据，供可信本地恢复/回放。"""
    from fairy.agents.loop.context.history import public_history, progress_diagnostic

    installed = {}
    for name in ("pcse", "openai", "numpy", "pandas", "PyYAML"):
        try:
            installed[name] = version(name)
        except PackageNotFoundError:
            pass
    return {
        "schema": 1,
        "state": runtime.export_state(),
        "metrics": evaluate(runtime),
        "agent": detached(agent_metadata or {}),
        "audit": runtime.audit_state(),
        "diagnostics": progress_diagnostic(public_history(runtime), runtime.now),
        "environment": {
            "python": platform.python_version(),
            "packages": installed,
            "source": detached(runtime.source_identity),
        },
    }


def write_json(path: str | Path, value: Any, *, overwrite: bool = False) -> None:
    """规范JSON原子发布，默认拒绝覆盖，避免并发或重跑破坏旧证据。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Unique temporary + atomic no-clobber publication, including concurrent writers.
    encoded = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        if overwrite:
            temporary.replace(path)
        else:
            os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def load_json(path: str | Path) -> JsonObject:
    """读取可信本地JSON产物；本函数不构建引擎或调用模型。"""
    return json.loads(Path(path).read_text(encoding="utf-8"))


def assert_equivalent(expected: Any, actual: Any, path: str | Path = "root") -> None:
    # Stable engine/parameter versions are checked independently of numeric tolerance.
    """递归比较状态/事件/指标，整数与身份严格一致，浮点使用固定容差。"""
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


def replay(record: JsonObject) -> tuple[Runtime, TaskMetrics]:
    """在新Runtime重新执行原请求并比较状态、审计和指标；不调用LLM。"""
    state = record["state"]
    from fairy import __version__

    if (
        record.get("schema") != 1
        or state.get("schema") != 7
        or state.get("fairy_version") != __version__
    ):
        raise ValueError("Incompatible artifact version")
    if fingerprint(state["scenario"]) != state["scenario_fingerprint"]:
        raise ValueError("Scenario fingerprint mismatch")
    if not isinstance(state.get("binding_fingerprint"), str):
        raise ValueError("Missing operation binding fingerprint")
    runtime = Runtime(
        ScenarioSpec.from_dict(state["scenario"]),
        state["backend"],
        expected_binding_fingerprint=state["binding_fingerprint"],
    )
    if runtime.source_identity["fingerprint"] != state.get("source_fingerprint"):
        raise ValueError("Executable source fingerprint mismatch")
    cancelled = {e["cause"] for e in state["events"] if e["kind"] == "operation_cancelled"}
    for event in state["events"]:
        if event["kind"] == "operation_requested":
            payload = event["payload"]
            request = OperationRequest(payload["tool"], payload["arguments"])
            if event["seq"] in cancelled:
                runtime.record_cancelled(request, payload["actor"])
            else:
                runtime.invoke(request, actor=payload["actor"])
        elif event["kind"] == "run_suspended":
            runtime.suspend(event["payload"]["reason"])
        elif event["kind"] == "run_terminated":
            runtime.finish(event["payload"]["reason"])
    assert_equivalent(state, runtime.export_state())
    assert_equivalent(record["audit"], runtime.audit_state())
    assert_equivalent(record["metrics"], evaluate(runtime))
    return runtime, evaluate(runtime, replay_verified=True)
