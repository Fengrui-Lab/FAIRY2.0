"""Public-tool lifecycle, fixed-input paired probes, reference calendar and replay."""

from __future__ import annotations
from typing import Mapping, TYPE_CHECKING, cast

if TYPE_CHECKING:
    from evaluate.scenarios.spec import ScenarioSpec
    from fairy.acps.ontology.contracts import JsonObject
import argparse
from datetime import date, timedelta
from pathlib import Path
from fairy.events.session import Runtime
from fairy.events.artifacts import artifact, replay, write_json
from fairy.events.records import fingerprint
from evaluate.oracle.environment import call, finish_driver
from .soybean_wofost import (
    soybean_wofost_normal,
    soybean_wofost_calendar_drought,
    soybean_wofost_reproductive_drought,
    REFERENCE_PATH,
    process_identity,
)


def run_probe(
    spec: ScenarioSpec, doses: Mapping[str, float] | None = None
) -> tuple[Runtime, JsonObject]:
    """用公共工具执行WOFOST固定补水日程并完成收储，返回运行与日过程。"""
    runtime = Runtime(spec, "wofost")
    doses = doses or {}
    call(runtime, "prepare_seedbed", start_ridge=0, end_ridge=63)
    call(runtime, "plant", start_ridge=0, end_ridge=63, spacing_m=0.079, depth_mm=40)
    phases: dict[str, str] = {}
    daily = []
    while True:
        crop = call(runtime, "inspect_crop", start_ridge=0, end_ridge=63)["receipt"]["observations"]
        soil = call(runtime, "observe_soil_range", start_ridge=0, end_ridge=63)["receipt"][
            "observations"
        ]
        day = runtime.now.date().isoformat()
        phase = crop[0]["phase"]
        phases.setdefault(phase, day)
        daily.append(dict(day=day, phase=phase, root_vwc=soil[0]["root_vwc"]))
        if all(row["harvestable"] for row in crop):
            break
        if (runtime.deadline - runtime.now).total_seconds() < 86400:
            raise ValueError("Probe did not mature before deadline")
        if day in doses:
            call(runtime, "irrigate_range", start_ridge=0, end_ridge=63, depth_mm=doses[day])
        call(runtime, "advance_time", hours=24)
    finish_driver(runtime)
    return runtime, dict(phase_starts=phases, daily=daily)


def build_reference() -> JsonObject:
    """离线运行公开管理前缀，生成可审查的固定阶段日历；不属于Agent任务。"""
    spec = soybean_wofost_normal()
    runtime, obs = run_probe(spec)
    return dict(
        schema=1,
        reference_backend="wofost",
        cultivar="Soybean_901_proxy",
        baseline_fingerprint=fingerprint(spec.to_dict()),
        process_identity=process_identity(),
        phase_starts=obs["phase_starts"],
        scope="public coarse phases; no R1/R5 calibration",
    )


def validate_suite(destination: str | Path) -> JsonObject:
    """执行声明的成组场景对照和严格回放，保留单项结果与失败证据。"""
    reference = build_reference()
    import json

    if reference != json.loads(REFERENCE_PATH.read_text()):
        raise ValueError("Frozen WOFOST calendar changed")
    out = Path(destination)
    out.mkdir(parents=True, exist_ok=False)
    reports = {}
    phase = soybean_wofost_reproductive_drought()
    event = json.loads(cast(str, phase.construction_json))["events"][0]
    start = date.fromisoformat(event["start_day"])
    phase_doses = {(start + timedelta(days=i)).isoformat(): 20.0 for i in (13, 26)}
    cases = [
        ("normal", soybean_wofost_normal(), {}),
        ("calendar_control", soybean_wofost_calendar_drought(), {}),
        (
            "calendar_managed",
            soybean_wofost_calendar_drought(),
            {"2026-06-08": 20.0, "2026-07-05": 20.0},
        ),
        ("phase_control", phase, {}),
        ("phase_managed", phase, phase_doses),
    ]
    for name, spec, doses in cases:
        runtime, obs = run_probe(spec, doses)
        record = artifact(
            runtime, {"kind": "fixed_public_probe", "doses": doses, "optimality": "not_proven"}
        )
        _, metrics = replay(record)
        if not metrics["task_success"]:
            raise ValueError("Probe engineering failure: " + name)
        write_json(out / (name + ".json"), record)
        reports[name] = dict(metrics=metrics, **obs)
    normal = reports["normal"]["metrics"]["stored_dry_grain_kg"]
    checks = {}
    for case in ("calendar", "phase"):
        control = reports[case + "_control"]
        managed = reports[case + "_managed"]
        loss = normal - control["metrics"]["stored_dry_grain_kg"]
        response = (
            managed["metrics"]["stored_dry_grain_kg"] - control["metrics"]["stored_dry_grain_kg"]
        )
        checks[case] = dict(
            dry_mass_difference_from_normal_kg=loss,
            intervention_response_kg=response,
            positive_response=loss > 0 and response > 0,
        )
    exposure = [
        d
        for d in reports["phase_control"]["daily"]
        if event["start_day"] <= d["day"] <= event["end_day"]
    ]
    covered = len(exposure) == 40 and all(d["phase"] == "reproductive" for d in exposure)
    valid = covered and checks["phase"]["positive_response"]
    report = dict(
        valid=valid,
        phase_coverage=covered,
        checks=checks,
        runs=reports,
        reference=reference,
        scope="uncalibrated proxy; fixed paired probes, not optimized doses or field evidence",
    )
    write_json(out / "report.json", report)
    if not valid:
        raise ValueError("WOFOST causal acceptance failed; see report")
    return report


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--build-reference", action="store_true")
    p.add_argument("--out")
    a = p.parse_args()
    if a.build_reference:
        write_json(REFERENCE_PATH, build_reference(), overwrite=True)
    elif a.out:
        validate_suite(a.out)
    else:
        p.error("Specify --build-reference or --out")
