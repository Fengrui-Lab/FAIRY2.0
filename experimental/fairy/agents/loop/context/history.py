"""Public event projections for continuation and non-intervening diagnostics."""

from __future__ import annotations
from typing import Sequence, TYPE_CHECKING, cast

if TYPE_CHECKING:
    from fairy.acps.ontology.contracts import JsonObject
    from fairy.events.records import HistoryTurn
    from fairy.events.session import Runtime
from datetime import datetime
from fairy.events.records import detached, fingerprint

OBSERVATIONS = {
    "observe_soil",
    "observe_soil_range",
    "observe_seedbed_range",
    "observe_surface_water",
    "scout_biotic",
    "observe_stand_range",
    "inspect_crop",
    "inspect_inventory",
    "inspect_dryer",
    "get_current_weather",
    "get_forecast",
    "survey_multispectral",
    "survey_thermal",
    "ground_inspect",
}
WORK = {
    "prepare_seedbed",
    "plant",
    "harvest",
    "unload",
    "dry_grain",
    "repair_dryer",
    "store_grain",
    "irrigate",
    "irrigate_range",
    "drain_surface",
    "treat_biotic",
    "replant_gaps",
}


def public_history(runtime: Runtime) -> list[HistoryTurn]:
    """从实际请求/回执构建公开历史；保留拒绝、取消与原始事件时刻。"""
    requests: dict[int, JsonObject] = {}
    history: list[HistoryTurn] = []
    for event in runtime.events:
        body = event.payload
        if event.kind == "operation_requested":
            requests[event.seq] = {"tool": body["tool"], "arguments": body["arguments"]}
        elif event.kind in ("operation_result", "operation_cancelled"):
            result = (
                body
                if event.kind == "operation_result"
                else {"ok": False, "data": {}, "error": "INTERRUPTED"}
            )
            history.append(
                {
                    "request": requests[cast(int, event.cause)],
                    "result": result,
                    "time": event.time,
                    "event_seq": event.seq,
                }
            )
    return detached(history)


def public_memory(history: Sequence[HistoryTurn], kept: Sequence[HistoryTurn]) -> JsonObject:
    """保留被近期历史截断的最新公开观测与未解决拒绝，不能从后端补全事实。"""
    latest: dict[str, HistoryTurn] = {}
    failures: dict[str, HistoryTurn] = {}
    for turn in history:
        req, res = turn["request"], turn["result"]
        key = fingerprint(req)
        if res["ok"]:
            failures.pop(key, None)  # only the same successful request resolves this failure
            if req["tool"] in OBSERVATIONS:
                latest.pop(key, None)
                latest[key] = turn
        else:
            failures.pop(key, None)
            failures[key] = turn
    kept_keys = {fingerprint(t) for t in kept}
    recent = list(latest.values())[-12:]
    return {
        "source": "public event receipts; observations retain their recorded time",
        "observations": [t for t in recent if fingerprint(t) not in kept_keys],
        "unresolved_rejections": list(failures.values())[-8:],
        "omitted_observation_targets": max(0, len(latest) - 12),
        "omitted_rejections": max(0, len(failures) - 8),
    }


def progress_diagnostic(history: Sequence[HistoryTurn], now: datetime) -> JsonObject:
    """仅依据公开历史诊断重复读取、等待或充电循环；不自动执行修复动作。"""
    window = history[-32:]
    timed = [t for t in window if t.get("time")]
    hours = (
        (now - datetime.fromisoformat(timed[0]["time"])).total_seconds() / 3600 if timed else 0.0
    )
    work = sum(t["result"]["ok"] and t["request"]["tool"] in WORK for t in window)
    failures = sum(not t["result"]["ok"] for t in window)
    signatures = [fingerprint({"request": t["request"], "result": t["result"]}) for t in window]
    return {
        "source": "last 32 public receipts; diagnostic only, no automatic actions",
        "window_calls": len(window),
        "simulated_hours": hours,
        "management_actions": work,
        "rejected_calls": failures,
        "repeated_request_results": len(signatures) - len(set(signatures)),
        "charging_calls": sum(t["request"]["tool"] == "charge_equipment" for t in window),
        "low_progress": len(window) >= 8 and work == 0 and hours < 24,
    }
