"""Offline reference construction and paired causal acceptance via public tools."""

from __future__ import annotations
from typing import Any, TYPE_CHECKING, cast

if TYPE_CHECKING:
    from evaluate.scenarios.spec import ScenarioSpec
    from fairy.acps.ontology.contracts import JsonObject
from dataclasses import asdict
from datetime import date, timedelta
import json
from pathlib import Path
from fairy.acps.ontology.contracts import OperationRequest
from fairy.events.session import Runtime
from fairy.events.artifacts import artifact, replay, write_json
from fairy.events.records import fingerprint
from evaluate.metrics import evaluate
from evaluate.scenarios.normal import SOYBEAN_NORMAL_MANAGEMENT
from evaluate.scenarios.compose import REFERENCE_PATH, process_files
from evaluate.scenarios.lib.l3.soybean_normal import (
    soybean_normal,
    soybean_drought_r1,
    soybean_drought_r5,
    soybean_drought_r1_r5,
)

CASES = {
    "normal": soybean_normal,
    "r1": soybean_drought_r1,
    "r5": soybean_drought_r5,
    "r1_r5": soybean_drought_r1_r5,
}


def run_reference(
    spec: ScenarioSpec, *, irrigate: bool = False, finish: bool = True
) -> tuple[Runtime, JsonObject]:
    """Fixed paired probe, not a learned policy or an optimality claim."""
    runtime = Runtime(spec)

    def call(name: str, **args: Any) -> JsonObject:
        result = runtime.invoke(OperationRequest(name, args), actor="scenario_validation")
        if not result.ok:
            raise ValueError(f"Reference operation rejected: {result.error}")
        return result.data

    call("inspect_inventory")
    call("get_current_weather")
    call("observe_soil_range", start_ridge=0, end_ridge=63)
    call("prepare_seedbed", start_ridge=0, end_ridge=63)
    call("plant", start_ridge=0, end_ridge=63, spacing_m=0.079, depth_mm=40)
    manifest = json.loads(cast(str, spec.construction_json))
    doses = {}
    if irrigate:
        for event in manifest["events"]:
            midpoint = date.fromisoformat(event["start_day"]) + timedelta(
                days=event["duration_days"] // 2
            )
            doses[midpoint.isoformat()] = 20.0
    stages: dict[str, str] = {}
    daily: list[JsonObject] = []
    while True:
        observed = call("inspect_crop", start_ridge=0, end_ridge=63)["receipt"]["observations"]
        soil = call("observe_soil_range", start_ridge=0, end_ridge=63)["receipt"]["observations"]
        day, stage = runtime.now.date().isoformat(), observed[0]["stage"]
        if len({r["stage"] for r in observed}) != 1:
            raise ValueError("The reference assumes simultaneous homogeneous planting")
        stages.setdefault(stage, day)
        daily.append({"day": day, "stage": stage, "root_vwc": soil[0]["root_vwc"]})
        if stage == "R8":
            break
        if runtime.now >= runtime.deadline:
            raise ValueError("Normal reference did not mature before deadline")
        if day in doses:
            call("irrigate_range", start_ridge=0, end_ridge=63, depth_mm=doses[day])
        call("advance_time", hours=24)
    if finish:
        for first in range(0, 64, 8):
            call("harvest", start_ridge=first, end_ridge=first + 7)
            call("unload")
            call("inspect_inventory")
            if runtime.season_physical.moisture(runtime.season_physical.state["trailer"]) > 0.13:
                call("dry_grain", target_moisture=0.13)
            call("store_grain")
        runtime.finish("scenario_validation")
    return runtime, {"stage_starts": stages, "daily": daily}


def build_reference() -> JsonObject:
    """离线运行公开管理前缀，生成可审查的固定阶段日历；不属于Agent任务。"""
    spec = soybean_normal()
    runtime, observation = run_reference(spec, finish=False)
    return {
        "schema": 1,
        "calendar_id": "hn58_synthetic_normal_calendar_v1",
        "baseline_fingerprint": json.loads(cast(str, spec.construction_json))[
            "baseline_fingerprint"
        ],
        "template_fingerprint": fingerprint(SOYBEAN_NORMAL_MANAGEMENT.to_dict()),
        "reference_backend": "native",
        "capabilities": asdict(runtime.season_backend.capabilities),
        "reference_prefix": "public inventory/weather/soil, prepare all ridges, plant all on start date; no irrigation",
        "stage_starts": observation["stage_starts"],
        "process_files_sha256": process_files(),
    }


def validate_suite() -> tuple[JsonObject, dict[str, JsonObject]]:
    """执行声明的成组场景对照和严格回放，保留单项结果与失败证据。"""
    actual_reference = build_reference()
    expected_reference = json.loads(REFERENCE_PATH.read_text())
    if fingerprint(actual_reference) != fingerprint(expected_reference):
        raise ValueError(
            "Frozen calendar differs from normal execution; explicitly rebuild and review"
        )
    runs, records = {}, {}
    for case, factory in CASES.items():
        for treatment in ((False,) if case == "normal" else (False, True)):
            name = case + ("_managed" if treatment else "_control")
            runtime, observation = run_reference(factory(), irrigate=treatment)
            record = artifact(
                runtime,
                {
                    "kind": "fixed_validation_probe",
                    "privileged": False,
                    "management_template": SOYBEAN_NORMAL_MANAGEMENT.template_id,
                    "intervention": (
                        "fixed whole-plot 20 mm at each event midpoint"
                        if treatment
                        else "no irrigation"
                    ),
                },
            )
            verified = replay(record)[1]
            if not verified["task_success"]:
                raise ValueError("Reference failed harvest/storage engineering gates")
            runs[name] = {"metrics": verified, **observation}
            records[name] = record
    normal = runs["normal_control"]
    normal_days = {r["day"]: r for r in normal["daily"]}
    checks = {}
    for case in ("r1", "r5", "r1_r5"):
        control, managed = runs[case + "_control"], runs[case + "_managed"]
        spec = CASES[case]()
        events = json.loads(cast(str, spec.construction_json))["events"]
        exposures = []
        for event in events:
            days = [
                r for r in control["daily"] if event["start_day"] <= r["day"] <= event["end_day"]
            ]
            if len(days) != event["duration_days"] or any(
                r["stage"] != event["stage"] for r in days
            ):
                raise ValueError("Drought missed its reference stage")
            if not any(r["root_vwc"] < normal_days[r["day"]]["root_vwc"] - 0.01 for r in days):
                raise ValueError("Rainfall deficit did not cause an observed root-water difference")
            applied = [
                e
                for e in records[case + "_control"]["state"]["events"]
                if e["kind"] == "environment_day_completed"
                and event["event_id"] in e["payload"]["forcing"]["disturbance_ids"]
            ]
            if len(applied) != event["duration_days"]:
                raise ValueError("Disturbance was missed or applied more than once")
            exposures.append(
                {
                    "event_id": event["event_id"],
                    "stage": event["stage"],
                    "days": len(days),
                    "start_day": event["start_day"],
                    "end_day": event["end_day"],
                }
            )
        loss = normal["metrics"]["stored_dry_grain_kg"] - control["metrics"]["stored_dry_grain_kg"]
        response = (
            managed["metrics"]["stored_dry_grain_kg"] - control["metrics"]["stored_dry_grain_kg"]
        )
        managed_days = {row["day"]: row for row in managed["daily"]}
        water_response = max(
            managed_days[row["day"]]["root_vwc"] - row["root_vwc"]
            for row in control["daily"]
        )
        if water_response <= 0.01:
            raise ValueError("Paired irrigation did not measurably replenish root-zone water")
        if (
            records[case + "_control"]["state"]["scenario_fingerprint"]
            != records[case + "_managed"]["state"]["scenario_fingerprint"]
        ):
            raise ValueError("Management comparison changed the exogenous scenario")
        checks[case] = {
            "exposures": exposures,
            "stored_dry_mass_difference_from_normal_kg": loss,
            "fixed_intervention_response_kg": response,
            "peak_root_vwc_response": water_response,
        }
    return {
        "valid": True,
        "scope": "synthetic Native water/crop engineering validation, not field calibration or optimality",
        "reference": actual_reference,
        "checks": checks,
        "runs": runs,
    }, records


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--build-reference",
        action="store_true",
        help="Explicitly regenerate the packaged reference calendar",
    )
    parser.add_argument(
        "--out", help="New output directory for report and seven replayed artifacts"
    )
    args = parser.parse_args()
    if args.build_reference:
        write_json(REFERENCE_PATH, build_reference(), overwrite=True)
    else:
        report, records = validate_suite()
        if args.out:
            destination = Path(args.out)
            destination.mkdir(parents=True, exist_ok=False)
            write_json(destination / "validation.json", report)
            for name, record in records.items():
                write_json(destination / (name + ".json"), record)
        print(json.dumps(report, ensure_ascii=False, indent=2))
