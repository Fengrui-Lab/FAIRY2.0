"""Privileged reference policy for engineering checks, not an optimum claim."""

from __future__ import annotations
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from fairy.events.session import Runtime
from fairy.acps.ontology.contracts import OperationRequest
from fairy.agents.models.decision import Decision


class SeasonOracle:
    """具有完整管理状态权限的全季脚本参考；通过公共工具执行，不是数值最优解。"""

    metadata = {
        "kind": "oracle",
        "privileged": True,
        "policy": "season_reference_v1",
        "optimality": "not_proven",
    }

    def decide(self, runtime: Runtime) -> Decision:
        """根据该策略允许的输入产生一次Decision；不直接调用农事工具。"""
        b, p = runtime.season_backend, runtime.season_physical
        s = p.state

        def request(name: str, **args: Any) -> Decision:
            return Decision(OperationRequest(name, args))

        def wait() -> Decision:
            hours = min(
                48,
                runtime.scenario.max_advance_hours,
                (runtime.deadline - runtime.now).total_seconds() / 3600,
            )
            return (
                request("advance_time", hours=hours)
                if hours > 0
                else Decision(finish_reason="deadline")
            )

        def span(name: str, ids: list[int], **args: Any) -> Decision:
            first = ids[0]
            end = first
            while end + 1 in ids and end - first < 7:
                end += 1
            return request(name, start_ridge=first, end_ridge=end, **args)

        if not any(e.kind == "operation_result" and e.payload["ok"] for e in runtime.events):
            return request("inspect_inventory")
        unprepared = [i for i, v in enumerate(s["prepared"]) if not v]
        unplanted = [i for i, r in enumerate(b.management_state()) if not r.planted]
        weather = runtime.weather[runtime.now.date().isoformat()]
        if unprepared or unplanted:
            if weather.rain_mm > 10:
                return wait()
            ids = unprepared or unplanted
            if any(b.management_state()[i].root_vwc > 0.36 for i in ids):
                return wait()
            return (
                span("prepare_seedbed", ids)
                if unprepared
                else span(
                    "plant",
                    ids,
                    spacing_m=runtime.season_config.seed_spacing_cm / 100,
                    depth_mm=runtime.season_config.sowing_depth_cm * 10,
                )
            )
        if p.weight(s["combine"]) > 0:
            return request("unload")
        if p.weight(s["trailer"]) > 0:
            if p.moisture(s["trailer"]) > runtime.season_config.safe_moisture:
                return request(
                    "dry_grain", target_moisture=min(0.13, runtime.season_config.safe_moisture)
                )
            return request("store_grain")
        unharvested = [i for i, r in enumerate(b.management_state()) if not r.harvested]
        if not unharvested:
            if runtime.pending_irrigation_mm:
                return wait()
            return Decision(finish_reason="oracle_finished")
        mature = [i for i in unharvested if b.management_state()[i].harvestable]
        if mature and weather.rain_mm <= 10:
            return span("harvest", mature)
        remaining = runtime.scenario.irrigation_quota_mm - runtime.irrigation_used_mm
        dry = [
            i
            for i in unharvested
            if b.field_observation((i,))[0]["stage"] in ("R5", "R6", "reproductive")
            and b.management_state()[i].root_vwc < 0.20
        ]
        if dry and remaining > 1e-6 and not runtime.pending_irrigation_mm:
            i = dry[0]
            return request(
                "irrigate_range", start_ridge=i, end_ridge=i, depth_mm=min(15, remaining * 64)
            )
        return wait()
