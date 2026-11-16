"""Privileged bounded one-day search reference, not a global-optimum claim."""
from datetime import datetime
from math import floor
from fairy.acps.ontology.contracts import OperationRequest
from fairy.agents.models.decision import Decision
from fairy.events.session import Runtime


def depth_candidates(quota):
    """At most limit trial depths, retaining the original 1-mm grid for small quotas."""
    from fairy.acps.ontology.farm.spec import finite
    finite(quota, "quota", 0)
    limit = 128
    upper = floor(quota)
    if upper <= limit - 1:
        depths = list(range(1, upper + 1))
    else:
        depths = [1 + (upper - 1) * i // (limit - 2) for i in range(limit - 1)]
    if quota > 0 and quota not in depths:
        depths.append(quota)
    return depths


class IrrigationOracle:
    metadata = {"kind": "oracle", "privileged": True,
                "policy": "bounded_depth_grid_v2", "max_candidates": 128, "optimality": "not_proven"}

    def decide(self, runtime):
        if runtime.done:
            return Decision(finish_reason="already_finished")
        for name, args in (("get_current_weather", {}), ("get_forecast", {"days": 1})):
            if name in runtime.scenario.tools and not any(
                e.kind == "operation_result" and e.payload["ok"] and
                runtime.events[e.cause - 1].payload["tool"] == name
                for e in runtime.events if e.cause is not None
            ):
                return Decision(OperationRequest(name, args))
        if not runtime.observations:
            return Decision(OperationRequest("observe_soil"))
        if not runtime.irrigation_used_mm:
            # Only the oracle receives the complete restore package and engine state.
            saved = runtime.export_state()
            target = (runtime.scenario.target_vwc_min + runtime.scenario.target_vwc_max) / 2
            hours = (datetime.fromisoformat(runtime.scenario.deadline) - runtime.now).total_seconds() / 3600
            if hours <= 0:
                return Decision(finish_reason="oracle_no_time_remaining")
            candidates = []
            quota = runtime.scenario.irrigation_quota_mm
            depths = depth_candidates(quota)
            for depth in depths:
                trial = Runtime.from_state(saved)
                accepted = trial.invoke(OperationRequest("irrigate", {"depth_mm": depth}), actor="oracle_search").ok
                remaining = hours
                while accepted and remaining > 1e-9:
                    chunk = min(remaining, runtime.scenario.max_advance_hours)
                    accepted = trial.invoke(OperationRequest("advance_time", {"hours": chunk}), actor="oracle_search").ok
                    remaining -= chunk
                if not accepted:
                    continue
                vwc = trial.audit_state()["root_vwc"]
                candidates.append((abs(vwc - target), depth))
            if not candidates:
                return Decision(finish_reason="oracle_no_feasible_action")
            depth = min(candidates)[1]
            return Decision(OperationRequest("irrigate", {"depth_mm": depth}))
        if runtime.pending_irrigation_mm:
            hours = (datetime.fromisoformat(runtime.scenario.deadline) - runtime.now).total_seconds() / 3600
            if hours <= 0:
                return Decision(finish_reason="oracle_no_time_remaining")
            return Decision(OperationRequest("advance_time", {"hours": min(hours, runtime.scenario.max_advance_hours)}))
        if runtime.now < runtime.deadline:
            hours = min(runtime.scenario.max_advance_hours, (runtime.deadline - runtime.now).total_seconds() / 3600)
            return Decision(OperationRequest("advance_time", {"hours": hours}))
        if datetime.fromisoformat(runtime.observations[-1]["time"]).date() < runtime.now.date():
            return Decision(OperationRequest("observe_soil"))
        return Decision(finish_reason="oracle_finished")
