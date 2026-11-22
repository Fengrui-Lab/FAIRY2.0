"""Qualified historical cases that exercise specific farm decisions."""

from datetime import date, timedelta
from pathlib import Path

from evaluate.scenarios.historical_scenarios import (
    call,
    require,
    window,
    compile_season,
    effective_response,
    make_regional_document,
    ready_for_water,
    run_season,
    verify_season,
)
from evaluate.scenarios.historical_slices import (
    capture,
    run_public_slice,
    run_reference_slice,
    verify_slice,
)
from evaluate.scenarios.historical_weather import POINT, TIMEZONE
from evaluate.scenarios.season import SeasonSpec
from fairy.acps.ontology.contracts import JsonObject
from fairy.acps.ontology.farm.spec import FarmSpec
from fairy.events.artifacts import artifact, assert_equivalent, replay, write_json
from fairy.events.session import Runtime


def build_dual_dry_patches(
    archive: JsonObject, out: Path, *, backend: str = "native"
) -> None:
    """Find a verified two-patch case allowing either empirically equivalent target."""
    if archive.get("requested_point") != list(POINT) or archive["timezone"] != TIMEZONE:
        raise ValueError("The dual-patch fixture requires the Harbin weather archive")
    west, east, background = (8, 19), (42, 53), (24, 35)
    depth_mm = 10.0
    quota_mm = depth_mm * 12 / 64
    document = make_regional_document(
        archive,
        region="harbin",
        start=date(2016, 5, 5),
        end=date(2016, 10, 22),
        kind="dual_dry_patch",
        farm=FarmSpec(length_m=268, width_m=64 * 1.1, initial_vwc=0.30),
        season=SeasonSpec(
            hydraulic_zones=((*west, 0.285, 0.82, 0.90), (*east, 0.285, 0.82, 0.90))
        ),
        quota_mm=quota_mm,
        exposure=(date(2016, 7, 1), date(2016, 8, 15)),
    )
    spec = compile_season(document, archive, backend=backend)
    control, control_trace = run_season(spec, target=west, backend=backend)

    def by_date(trace):
        return {row["date"]: row for row in trace["daily"]}

    west_days = by_date(control_trace)
    _, east_trace = run_season(spec, target=east, backend=backend)
    _, background_trace = run_season(spec, target=background, backend=backend)
    east_days, background_days = by_date(east_trace), by_date(background_trace)
    day = next(
        (
            key
            for key, row in west_days.items()
            if (backend == "native" or "2016-07-01" <= key <= "2016-08-15")
            and all(
                ready_for_water(
                    {"vwc": value["target_vwc"], "stages": value["target_stages"]},
                    backend,
                )
                for value in (row, east_days[key])
            )
            and background_days[key]["target_vwc"] >= 0.18
        ),
        None,
    )
    if day is None:
        raise ValueError(
            "No documented reproductive-stage deficit in both target patches"
        )
    following = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
    dose = (day, depth_mm)
    verify_season(document, archive, control)
    files = {"drought-l3": document, "control": control}
    metrics = {}
    deltas = []
    for side, region, zero_days in (
        ("west", west, west_days),
        ("east", east, east_days),
    ):
        reference, trace = run_season(spec, dose=dose, target=region, backend=backend)
        verify_season(document, archive, reference)
        delta = (
            reference["metrics"]["stored_dry_grain_kg"]
            - control["metrics"]["stored_dry_grain_kg"]
        )
        response = (
            by_date(trace)[following]["target_vwc"] - zero_days[following]["target_vwc"]
        )
        if (backend == "native" and delta <= 0) or not effective_response(response, 0):
            raise ValueError(
                f"{side} treatment did not validate against the zero-water control"
            )
        if abs(reference["metrics"]["water_used_mm"] - quota_mm) > 1e-9:
            raise ValueError(
                "The intervention must consume the single shared allocation"
            )
        deltas.append(delta)
        files[side] = reference
        metrics[side] = {
            "added_stored_dry_kg": delta,
            "next_day_response_vwc": response,
        }
        for level in ("L1", "L2"):
            stem = f"{side}-{level.lower()}"
            doc, checkpoint = capture(reference, day, level, f"{stem}-start.json")
            public = run_public_slice(doc, checkpoint)
            selected = run_reference_slice(doc, checkpoint, dose, target=region)
            for result in (public, selected):
                if not verify_slice(doc, checkpoint, result, reference)["task_success"]:
                    raise ValueError(f"Public or reference slice failed: {stem}")
            files.update(
                {
                    stem: doc,
                    f"{stem}-start": checkpoint,
                    f"{stem}-public": public,
                    f"{stem}-reference": selected,
                }
            )
    if backend == "wofost":
        quota = Runtime.from_state(files["west-l1-start"]["state"])
        require(call(quota, "irrigate_range", **window(west), depth_mm=depth_mm))
        before = quota.export_state()
        refusal = call(quota, "irrigate_range", **window(east), depth_mm=depth_mm)
        if refusal.error != "WATER_QUOTA_EXCEEDED":
            raise ValueError(
                "Second patch did not reach the shared water allocation gate"
            )
        assert_equivalent(
            {k: v for k, v in before.items() if k not in ("events", "tool_errors")},
            {
                k: v
                for k, v in quota.export_state().items()
                if k not in ("events", "tool_errors")
            },
        )
        files["quota-control"] = artifact(quota)
        replay(files["quota-control"])
    equivalent = abs(deltas[0] - deltas[1]) <= 1e-6
    if backend == "native" and not equivalent:
        raise ValueError(
            "Both patches may only be considered equivalent after a matched comparison"
        )
    files["qualification"] = {
        "decision_date": day,
        "shared_quota_mm": quota_mm,
        "dose_depth_mm": depth_mm,
        "candidate_vwc": {
            "west": west_days[day]["target_vwc"],
            "east": east_days[day]["target_vwc"],
            "background": background_days[day]["target_vwc"],
        },
        **(
            {"alternatives": metrics, "equivalent_dry_grain": equivalent}
            if backend == "wofost"
            else {"equivalent_alternatives": metrics}
        ),
    }
    out.mkdir(parents=True, exist_ok=False)
    for name, value in files.items():
        write_json(out / f"{name}.json", value)
