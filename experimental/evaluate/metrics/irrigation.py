"""Task, resource, tool and replay outcomes remain separate metrics."""

from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from evaluate.metrics import TaskMetrics
    from fairy.events.session import Runtime
from datetime import datetime


def evaluate(runtime: Runtime, replay_verified: bool | None = None) -> TaskMetrics:
    """评价先观测、补水、次日复查与水分目标，不使用产量或全季得分。"""
    spec = runtime.scenario
    first = runtime.first_irrigation_seq
    pre = [o for o in runtime.observations if first is not None and o["seq"] < first]
    last_day = (
        datetime.fromisoformat(runtime.last_irrigation_time).date()
        if runtime.last_irrigation_time
        else None
    )
    post = [
        o
        for o in runtime.observations
        if last_day is not None and datetime.fromisoformat(o["time"]).date() > last_day
    ]
    moisture = runtime.audit_state()["root_vwc"]
    in_target = spec.target_vwc_min <= moisture <= spec.target_vwc_max
    queried = {
        runtime.events[e.cause - 1].payload["tool"]
        for e in runtime.events
        if e.kind == "operation_result"
        and e.payload["ok"]
        and e.cause is not None
        and first is not None
        and e.cause < first
    }
    required_queries_completed = set(spec.required_queries) <= queried
    return dict(
        task_success=bool(
            pre
            and post
            and in_target
            and runtime.pending_irrigation_mm == 0
            and required_queries_completed
        ),
        required_queries_completed=required_queries_completed,
        target_reached=in_target,
        observed_before_irrigation=bool(pre),
        observed_after_settlement=bool(post),
        root_vwc=moisture,
        water_used_mm=runtime.irrigation_used_mm,
        water_used_m3=spec.farm.volume_m3(runtime.irrigation_used_mm),
        tool_errors=runtime.tool_errors,
        replay_verified=replay_verified,
    )
