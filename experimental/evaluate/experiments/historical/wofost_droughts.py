"""Public-tool qualification of historical droughts on the WOFOST proxy."""

from datetime import date, timedelta
from pathlib import Path

from evaluate.experiments.historical.operations import (
    TARGET,
    call,
    effective_response,
    next_day,
    ready_for_water,
    require,
    run_season,
    verify_season,
    water_stress_stage,
    window,
)
from evaluate.scenarios.historical.compiler import compile_season, make_season_document
from evaluate.scenarios.historical.weather import verify_archive
from evaluate.experiments.historical.slices import (
    capture,
    run_public_slice,
    run_reference_slice,
    verify_slice,
)
from evaluate.scenarios.historical.weather import rank_seasons
from fairy.acps.ontology.contracts import JsonObject
from fairy.events.artifacts import (
    artifact,
    assert_equivalent,
    load_json,
    replay,
    write_json,
)
from fairy.events.session import Runtime

BACKEND = "wofost"
HARBIN_YEARS = (2016, 2001, 2007)
REFERENCE_DATES = {2016: "2016-07-14", 2001: "2001-07-09", 2007: "2007-07-13"}


def _check_pending(
    checkpoint: JsonObject, depth: float
) -> tuple[JsonObject, JsonObject]:
    """Resume an unsettled application and compare it with the continuous path."""
    continuous = Runtime.from_state(checkpoint["state"])
    require(call(continuous, "irrigate_range", **window(TARGET), depth_mm=depth))
    pending = continuous.pending_irrigation_mm
    if pending != depth * 22 / 64:
        raise ValueError("Pending irrigation disagrees with the irrigated ridge area")
    start = artifact(continuous)
    restored = Runtime.from_state(start["state"])
    for runtime in (continuous, restored):
        next_day(runtime)
        if runtime.pending_irrigation_mm or runtime.irrigation_used_mm != pending:
            raise ValueError("Pending irrigation was not settled exactly once")
    assert_equivalent(continuous.export_state(), restored.export_state())
    result = artifact(restored)
    replay(result)
    return start, result


def qualify_drought(archive: JsonObject, year: int, out: Path) -> JsonObject:
    """Keep the declared exposure and record water and grain responses separately."""
    document = make_season_document(archive, year, "drought")
    spec = compile_season(document, archive, backend=BACKEND)
    control, trace = run_season(spec, backend=BACKEND)
    verify_season(document, archive, control)
    (first,) = document["environment"]["timeline"]
    daily = {row["date"]: row for row in trace["daily"]}
    exposure = [
        row
        for day, row in daily.items()
        if first["start_date"] <= day <= first["end_date"]
        and water_stress_stage(row["target_stages"], BACKEND)
    ]
    eligible = [
        row["date"]
        for row in exposure
        if ready_for_water(
            {"stages": row["target_stages"], "vwc": row["target_vwc"]}, BACKEND
        )
    ]
    original = REFERENCE_DATES.get(year)
    report: JsonObject = {
        "year": year,
        "scenario_id": spec.scenario_id,
        "native_scenario_id": document["identity"]["id"],
        "exposure_dates": [first["start_date"], first["end_date"]],
        "reproductive_days_in_window": len(exposure),
        "low_vwc_reproductive_days_in_window": len(eligible),
        "native_reference_day": original,
        "wofost_observation_on_native_reference_day": (
            daily.get(original) if original else None
        ),
        "zero_stored_dry_kg": control["metrics"]["stored_dry_grain_kg"],
        "zero_irrigation_task_success": control["metrics"]["task_success"],
    }
    out.mkdir(parents=True, exist_ok=False)
    write_json(out / "drought-l3.json", document)
    write_json(out / "drought-zero.json", control)
    write_json(out / "zero-exposure.json", trace)
    if not eligible:
        report.update(
            status="exposure_unmet",
            reason="No low-VWC reproductive day in the declared window",
        )
    else:
        day = original if original is not None and original in eligible else eligible[0]
        depth = 10.0
        treated, treatment_trace = run_season(spec, dose=(day, depth), backend=BACKEND)
        verify_season(document, archive, treated)
        write_json(out / "drought-reference.json", treated)
        write_json(out / "reference-exposure.json", treatment_trace)
        tomorrow = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
        after = next(row for row in treatment_trace["daily"] if row["date"] == tomorrow)
        response = after["target_vwc"] - daily[tomorrow]["target_vwc"]
        report.update(
            treatment_date=day,
            dose_depth_mm=depth,
            water_mm=treated["metrics"]["water_used_mm"],
            water_m3=treated["metrics"]["water_used_m3"],
            next_day_response_vwc=response,
            added_stored_dry_kg=(
                treated["metrics"]["stored_dry_grain_kg"]
                - control["metrics"]["stored_dry_grain_kg"]
            ),
            treated_stored_dry_kg=treated["metrics"]["stored_dry_grain_kg"],
            treated_task_success=treated["metrics"]["task_success"],
        )
        if not effective_response(response, 0):
            report.update(
                status="response_unmet",
                reason="Next-day contrast below public response minimum",
            )
        else:
            for level in ("L1", "L2"):
                stem = level.lower()
                cut, start = capture(treated, day, level, f"{stem}-start.json")
                public = run_public_slice(cut, start)
                reference = run_reference_slice(cut, start, (day, depth))
                for name, result in (("public", public), ("reference", reference)):
                    if not verify_slice(cut, start, result, treated)["task_success"]:
                        raise ValueError(
                            f"{year} {level} {name} irrigation slice did not qualify"
                        )
                    write_json(out / f"{stem}-{name}.json", result)
                write_json(out / f"{stem}.json", cut)
                write_json(out / f"{stem}-start.json", start)
                if level == "L1":
                    pending, settled = _check_pending(start, depth)
                    write_json(out / "pending-irrigation.json", pending)
                    write_json(out / "settled-irrigation.json", settled)
            report.update(status="qualified")
    write_json(out / "qualification.json", report)
    return report


def expand_wofost_droughts(archive: JsonObject, out: Path) -> bool:
    """Qualify the original years and seek a real replacement for any missed exposure."""
    verify_archive(archive)
    out.mkdir(parents=True, exist_ok=False)
    years = {
        str(year): qualify_drought(archive, year, out / str(year))
        for year in HARBIN_YEARS
    }
    needed = sum(row["status"] == "exposure_unmet" for row in years.values())
    replacements = []
    if needed:
        for year in rank_seasons(archive)["drought_order"]:
            if str(year) in years:
                continue
            report = qualify_drought(archive, year, out / str(year))
            years[str(year)] = report
            if report["status"] == "qualified":
                replacements.append(year)
                if len(replacements) == needed:
                    break
    write_json(
        out / "expansion.json",
        dict(
            backend=BACKEND,
            archive_fingerprint=archive["fingerprint"],
            years=years,
            supplemental_years=replacements,
        ),
    )
    return (
        not any(
            row["status"] == "response_unmet"
            for year, row in years.items()
            if int(year) in HARBIN_YEARS
        )
        and len(replacements) == needed
    )


def verify_wofost_droughts(archive: JsonObject, out: Path) -> None:
    """Recheck archive binding, strict replay and both outcomes of each saved cut."""
    report = load_json(out / "expansion.json")
    if report["archive_fingerprint"] != archive["fingerprint"]:
        raise ValueError("Drought suite archive mismatch")
    for year, qualification in report["years"].items():
        folder = out / year
        doc = load_json(folder / "drought-l3.json")
        verify_season(doc, archive, load_json(folder / "drought-zero.json"))
        if qualification["status"] != "exposure_unmet":
            treated = load_json(folder / "drought-reference.json")
            verify_season(doc, archive, treated)
        if qualification["status"] == "qualified":
            for level in ("l1", "l2"):
                cut, start = (
                    load_json(folder / f"{level}{suffix}.json")
                    for suffix in ("", "-start")
                )
                for strategy in ("public", "reference"):
                    result = load_json(folder / f"{level}-{strategy}.json")
                    if not verify_slice(cut, start, result, treated)["task_success"]:
                        raise ValueError(
                            f"{year} {level} {strategy} no longer qualifies"
                        )
            pending, settled = _check_pending(
                load_json(folder / "l1-start.json"), qualification["dose_depth_mm"]
            )
            assert_equivalent(pending, load_json(folder / "pending-irrigation.json"))
            assert_equivalent(settled, load_json(folder / "settled-irrigation.json"))
