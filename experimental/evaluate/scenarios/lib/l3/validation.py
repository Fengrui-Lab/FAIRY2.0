"""Offline causal checks, never an Agent prompt or an Oracle answer source."""

from __future__ import annotations
from typing import Any, TYPE_CHECKING, cast

if TYPE_CHECKING:
    from fairy.acps.ontology.contracts import JsonObject, OperationResult
from fairy.acps.backends.biology.native.season import NativeSeasonBackend
from evaluate.scenarios.lib.l3.soybean_full_season import soybean_full_season
from evaluate.metrics import evaluate
from fairy.acps.ontology.contracts import OperationRequest
from fairy.events.session import Runtime
from fairy.events.artifacts import artifact, replay


def validate_drought_challenge() -> tuple[JsonObject, dict[str, JsonObject]]:
    """检查固定干旱是否覆盖目标阶段，并用同输入对照验证实际补水响应。"""
    outputs: JsonObject = {}
    artifacts: dict[str, JsonObject] = {}
    for treatment in (False, True):
        runtime = Runtime(soybean_full_season())

        def call(name: str, **args: Any) -> OperationResult:
            result = runtime.invoke(OperationRequest(name, args), actor="scenario_validation")
            if not result.ok:
                raise ValueError(f"Validation operation rejected: {result.error}")
            return result

        call("prepare_seedbed", start_ridge=0, end_ridge=63)
        call("plant", start_ridge=0, end_ridge=63, spacing_m=0.079, depth_mm=40)
        stages: dict[str, str] = {}
        stress_days, target_days = [], []
        while runtime.now.date().isoformat() < "2026-08-06":
            today = runtime.now.date().isoformat()
            row = cast(NativeSeasonBackend, runtime.backend).state["ridges"][22]
            stage = cast(NativeSeasonBackend, runtime.backend).stage(row)
            stages.setdefault(stage, today)
            if "2026-07-15" <= today <= "2026-07-31" and stage in ("R5", "R6"):
                target_days.append(today)
                if row["storage_mm"] / (runtime.scenario.farm.root_depth_m * 1000) < 0.20:
                    stress_days.append(today)
            # Fixed engineering intervention, not an automatically optimal policy.
            if treatment and today == "2026-07-20":
                call("irrigate_range", start_ridge=22, end_ridge=43, depth_mm=4 * 64 / 22)
            call("advance_time", hours=24)
        for first in range(0, 64, 8):
            call("harvest", start_ridge=first, end_ridge=first + 7)
            call("unload")
            if runtime.season_physical.moisture(runtime.season_physical.state["trailer"]) > 0.13:
                call("dry_grain", target_moisture=0.13)
            call("store_grain")
        runtime.finish("scenario_validation")
        name = "irrigated" if treatment else "control"
        record = artifact(runtime)
        replay(record)
        outputs[name] = {
            "stage_dates": stages,
            "target_stage_days": target_days,
            "stress_days": stress_days,
            "metrics": evaluate(runtime),
        }
        artifacts[name] = record
    delta = (
        outputs["irrigated"]["metrics"]["stored_dry_grain_kg"]
        - outputs["control"]["metrics"]["stored_dry_grain_kg"]
    )
    valid = (
        len(outputs["control"]["target_stage_days"]) == 17
        and bool(outputs["control"]["stress_days"])
        and delta > 0
        and all(row["metrics"]["task_success"] for row in outputs.values())
    )
    if not valid:
        raise ValueError("Drought challenge failed stage/stress/intervention validation")
    return {
        "valid": valid,
        "forcing_policy": "identical frozen weather for both runs",
        "status": "synthetic causal validation; not field calibration or optimality",
        "dry_grain_response_kg": delta,
        "runs": outputs,
    }, artifacts


if __name__ == "__main__":
    import json

    print(json.dumps(validate_drought_challenge()[0], ensure_ascii=False, indent=2))
