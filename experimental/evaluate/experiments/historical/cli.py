"""Build and verify the historical L3 pair and its L1/L2 descendants."""

from __future__ import annotations

import argparse
from datetime import date, timedelta
from pathlib import Path
from typing import Sequence

from evaluate.scenarios.historical.compiler import (
    PHASES,
    compile_season,
    make_season_document,
    make_regional_document,
)
from evaluate.experiments.historical.operations import (
    STRESS_VWC,
    effective_response,
    run_season,
    verify_season,
)
from evaluate.scenarios.historical.weather import verify_archive
from evaluate.experiments.historical.slices import (
    capture,
    run_public_slice,
    run_reference_slice,
    verify_slice,
)
from evaluate.scenarios.season import SeasonSpec
from fairy.acps.ontology.farm.spec import FarmSpec
from evaluate.scenarios.historical.weather import import_archive, rank_seasons
from fairy.acps.ontology.contracts import JsonObject
from fairy.events.artifacts import assert_equivalent, load_json, replay, write_json


def _at(trace: JsonObject, day: str) -> JsonObject:
    return next((row for row in trace["daily"] if row["date"] == day), {})


def _try_drought(
    normal_stress: int, doc: JsonObject, archive: JsonObject
) -> dict[str, JsonObject]:
    spec = compile_season(doc, archive)
    control, trace = run_season(spec)
    if trace["stress_days"] <= normal_stress:
        raise ValueError("No stronger R5/R6 soil stress than the typical year")
    qualifying = [
        row["date"]
        for row in trace["daily"]
        if set(row["target_stages"]) <= set(PHASES) and row["target_vwc"] < STRESS_VWC
    ]
    reasons: list[str] = []
    for day in qualifying[:5]:
        for depth in (10.0, 20.0, 30.0):
            try:
                dose = (day, depth)
                treated, treatment_trace = run_season(spec, dose=dose)
                tomorrow = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
                same_day = _at(trace, tomorrow)
                with_water = _at(treatment_trace, tomorrow)
                if not (
                    same_day
                    and with_water
                    and effective_response(
                        with_water["target_vwc"], same_day["target_vwc"]
                    )
                ):
                    raise ValueError("Reference failed the next-day moisture contrast")
                if (
                    treated["metrics"]["stored_dry_grain_kg"]
                    <= control["metrics"]["stored_dry_grain_kg"]
                ):
                    raise ValueError("Reference did not improve stored dry grain")
                files = {
                    "drought-l3.json": doc,
                    "drought-zero.json": control,
                    "drought-reference.json": treated,
                    "drought-exposure.json": {
                        "zero": trace,
                        "reference": treatment_trace,
                        "dose": dose,
                        "rejected_candidates": reasons,
                    },
                }
                for level, at in (("L1", day), ("L2", qualifying[0])):
                    stem = level.lower()
                    child, start = capture(treated, at, level, f"{stem}-start.json")
                    public = run_public_slice(child, start)
                    reference = run_reference_slice(child, start, dose)
                    if not (
                        public["metrics"]["task_success"]
                        and reference["metrics"]["task_success"]
                    ):
                        raise ValueError(
                            f"{level} public or reference trajectory did not qualify"
                        )
                    for run in (public, reference):
                        verify_slice(child, start, run, treated)
                    files.update(
                        {
                            f"{stem}.json": child,
                            f"{stem}-start.json": start,
                            f"{stem}-public.json": public,
                            f"{stem}-reference.json": reference,
                        }
                    )
                replay(control)
                replay(treated)
                return files
            except ValueError as exc:
                reasons.append(f"{day}/{depth:g}: {exc}")
    raise ValueError(
        "No validated dry-season path in the bounded search: " + "; ".join(reasons)
    )


def build(archive: JsonObject, out: Path) -> bool:
    """Select the first ranked pair that passes the declared historical and simulation gates."""
    verify_archive(archive)
    out.mkdir(parents=True, exist_ok=False)
    report = rank_seasons(archive)
    report["archive_fingerprint"] = archive["fingerprint"]
    report["failed_pairs"] = []
    normal_trials = {}
    for normal_year in report["normal_order"]:
        if not report["drought_order"]:
            break
        normal_doc = make_season_document(archive, normal_year, "normal")
        try:
            normal_run, normal_trace = run_season(compile_season(normal_doc, archive))
        except ValueError as exc:
            report["failed_pairs"].append({"normal": normal_year, "reason": str(exc)})
            continue
        if normal_trace["stress_days"] > 2:
            report["failed_pairs"].append(
                {
                    "normal": normal_year,
                    "reason": "Typical-year R5/R6 stress exceeds two days",
                }
            )
            continue
        normal_trials[normal_year] = normal_trace["stress_days"]
        for dry_year in report["drought_order"]:
            dry_doc = make_season_document(archive, dry_year, "drought")
            try:
                bundle = _try_drought(normal_trace["stress_days"], dry_doc, archive)
            except ValueError as exc:
                report["failed_pairs"].append(
                    {"normal": normal_year, "drought": dry_year, "reason": str(exc)}
                )
                continue
            replay(normal_run)
            bundle.update(
                {
                    "normal-l3.json": normal_doc,
                    "normal-run.json": normal_run,
                    "normal-exposure.json": normal_trace,
                }
            )
            for name, payload in bundle.items():
                write_json(out / name, payload)
            report.update(
                status="qualified", normal_year=normal_year, drought_year=dry_year
            )
            write_json(out / "selection.json", report)
            return True
    report.update(
        status="blocked",
        reason="No complete, qualifying historical pair",
        typical_trials=normal_trials,
    )
    write_json(out / "selection.json", report)
    return False


def verify(archive: JsonObject, folder: Path) -> None:
    verify_archive(archive)
    report = load_json(folder / "selection.json")
    if (
        report["status"] != "qualified"
        or report["archive_fingerprint"] != archive["fingerprint"]
    ):
        raise ValueError("Bundle is not a qualified run of this weather archive")
    ranking = rank_seasons(archive)
    assert_equivalent(ranking, {key: report[key] for key in ranking})
    normal = load_json(folder / "normal-l3.json")
    dry = load_json(folder / "drought-l3.json")
    reference = load_json(folder / "drought-reference.json")
    exposure = load_json(folder / "drought-exposure.json")
    dose = (exposure["dose"][0], exposure["dose"][1])
    normal_year, dry_year = (
        doc["environment"]["weather"]["year"] for doc in (normal, dry)
    )
    if (
        normal["identity"]["role"] != "normal"
        or dry["identity"]["role"] != "drought"
        or (normal_year, dry_year) != (report["normal_year"], report["drought_year"])
        or normal_year not in ranking["normal_order"]
        or dry_year not in ranking["drought_order"]
    ):
        raise ValueError("Selection does not match the archived weather ranking")
    for doc, record, expected_trace, treatment in (
        (
            normal,
            load_json(folder / "normal-run.json"),
            load_json(folder / "normal-exposure.json"),
            None,
        ),
        (dry, load_json(folder / "drought-zero.json"), exposure["zero"], None),
        (dry, reference, exposure["reference"], dose),
    ):
        verify_season(doc, archive, record)
        repeated, trace = run_season(compile_season(doc, archive), dose=treatment)
        assert_equivalent(expected_trace, trace)
        assert_equivalent(record["state"], repeated["state"])
    next_day = (date.fromisoformat(dose[0]) + timedelta(days=1)).isoformat()
    if (
        exposure["zero"]["stress_days"]
        <= load_json(folder / "normal-exposure.json")["stress_days"]
        or not effective_response(
            _at(exposure["reference"], next_day)["target_vwc"],
            _at(exposure["zero"], next_day)["target_vwc"],
        )
        or reference["metrics"]["stored_dry_grain_kg"]
        <= load_json(folder / "drought-zero.json")["metrics"]["stored_dry_grain_kg"]
    ):
        raise ValueError("Recorded pair failed historical contrast")
    for level in ("l1", "l2"):
        doc = load_json(folder / f"{level}.json")
        start = load_json(folder / f"{level}-start.json")
        for kind in ("public", "reference"):
            metrics = verify_slice(
                doc, start, load_json(folder / f"{level}-{kind}.json"), reference
            )
            if not metrics["task_success"]:
                raise ValueError(f"{level} {kind} failed replayed task metrics")


def expand_droughts(archive: JsonObject, out: Path) -> bool:
    """Publish independently qualified Harbin drought bundles for every ranked candidate."""
    verify_archive(archive)
    ranking = rank_seasons(archive)
    if not ranking["drought_order"]:
        raise ValueError("No complete drought candidates in the ranking")
    out.mkdir(parents=True, exist_ok=False)
    reports = {}
    for year in ranking["drought_order"]:
        document = make_season_document(archive, year, "drought")
        try:
            bundle = _try_drought(0, document, archive)
        except ValueError as exc:
            reports[str(year)] = {"status": "rejected", "reason": str(exc)}
            continue
        for name, data in bundle.items():
            write_json(out / str(year) / name, data)
        exposure = bundle["drought-exposure.json"]
        reference = bundle["drought-reference.json"]["metrics"]
        zero = bundle["drought-zero.json"]["metrics"]
        reports[str(year)] = {
            "status": "qualified",
            "rain_minus_et0_mm": ranking["climatic_balance_mm"][str(year)],
            "zero_stress_days": exposure["zero"]["stress_days"],
            "reference_stress_days": exposure["reference"]["stress_days"],
            "dose": exposure["dose"],
            "water_mm": reference["water_used_mm"],
            "added_stored_dry_kg": reference["stored_dry_grain_kg"]
            - zero["stored_dry_grain_kg"],
        }
    write_json(
        out / "expansion.json",
        {
            "archive_fingerprint": archive["fingerprint"],
            "years": reports,
        },
    )
    return any(row["status"] == "qualified" for row in reports.values())


def run_regional(
    archive: JsonObject, profile: JsonObject, out: Path, *, backend: str = "native"
) -> None:
    """Compile a regional profile, complete it with a reference driver and check replay."""
    if (
        archive["timezone"] != profile["timezone"]
        or archive.get("requested_point", archive["point"])
        != profile["requested_point"]
    ):
        raise ValueError(
            "The archive does not match the regional profile point and timezone"
        )
    exposure = None
    if "exposure" in profile:
        first, last = profile["exposure"]
        exposure = (date.fromisoformat(first), date.fromisoformat(last))
    document = make_regional_document(
        archive,
        region=profile["region"],
        start=date.fromisoformat(profile["start_date"]),
        end=date.fromisoformat(profile["end_date"]),
        kind=profile["role"],
        farm=FarmSpec(**profile["farm"]),
        season=SeasonSpec.from_dict(profile["season"]),
        quota_mm=profile["quota_mm"],
        exposure=exposure,
    )
    record, trace = run_season(
        compile_season(document, archive, backend=backend), backend=backend
    )
    verify_season(document, archive, record)
    out.mkdir(parents=True, exist_ok=False)
    for name, data in (("scenario", document), ("run", record), ("exposure", trace)):
        write_json(out / f"{name}.json", data)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    ingest = commands.add_parser(
        "import-cds", help="Import locally downloaded original point CSVs"
    )
    ingest.add_argument("csv", nargs="+", type=Path)
    ingest.add_argument("--out", required=True, type=Path)
    acquire = commands.add_parser(
        "fetch-era5", help="Fetch explicit ERA5 via PCSE and archive UTC-hourly inputs"
    )
    acquire.add_argument("folder", type=Path)
    acquire.add_argument("--out", required=True, type=Path)
    for name in (
        "build",
        "verify",
        "expand-droughts",
        "build-dual-dry-patches",
        "build-wet-harvest",
        "build-cold-planting",
        "build-integrated-season",
        "verify-integrated-season",
        "build-flood",
        "build-biotic",
        "build-replant",
        "build-dryer-failure",
    ):
        sub = commands.add_parser(name)
        sub.add_argument("archive", type=Path)
        sub.add_argument("folder", type=Path)
        if name in (
            "verify",
            "expand-droughts",
            "build-dual-dry-patches",
            "build-wet-harvest",
            "build-cold-planting",
            "build-integrated-season",
            "build-dryer-failure",
        ):
            sub.add_argument(
                "--backend", choices=("native", "wofost"), default="native"
            )
    regional = commands.add_parser(
        "run-regional", help="Run a separately declared regional baseline"
    )
    regional.add_argument("archive", type=Path)
    regional.add_argument("profile", type=Path)
    regional.add_argument("folder", type=Path)
    regional.add_argument("--backend", choices=("native", "wofost"), default="native")
    args = parser.parse_args(argv)
    if args.command == "import-cds":
        write_json(args.out, import_archive(args.csv))
        return 0
    if args.command == "fetch-era5":
        from evaluate.experiments.historical.weather_provider import (
            fetch_openmeteo,
            import_openmeteo,
        )

        write_json(args.out, import_openmeteo(fetch_openmeteo(args.folder)))
        return 0
    archive = load_json(args.archive)
    if args.command == "build":
        success = build(archive, args.folder)
        print(args.folder / "selection.json")
        return 0 if success else 2
    if args.command == "expand-droughts":
        if args.backend == "wofost":
            from evaluate.experiments.historical.wofost_droughts import expand_wofost_droughts

            return 0 if expand_wofost_droughts(archive, args.folder) else 2
        return 0 if expand_droughts(archive, args.folder) else 2
    if args.command == "build-dual-dry-patches":
        from evaluate.experiments.historical.cases.dual_dry_patches import build_dual_dry_patches

        build_dual_dry_patches(archive, args.folder, backend=args.backend)
        return 0
    if args.command == "build-wet-harvest":
        from evaluate.experiments.historical.cases.wet_harvest import build_wet_harvest

        build_wet_harvest(archive, args.folder, backend=args.backend)
        return 0
    if args.command == "build-integrated-season":
        from evaluate.experiments.historical.cases.integrated import build_integrated_season

        build_integrated_season(archive, args.folder, backend=args.backend)
        return 0
    if args.command == "verify-integrated-season":
        from evaluate.experiments.historical.cases.integrated import verify_integrated_season

        verify_integrated_season(archive, args.folder)
        return 0
    if args.command == "build-cold-planting":
        from evaluate.experiments.historical.cases.cold_planting import build_cold_planting

        build_cold_planting(archive, args.folder, backend=args.backend)
        return 0
    if args.command == "build-flood":
        from evaluate.experiments.historical.cases.flood import build_flood

        build_flood(archive, args.folder)
        return 0
    if args.command == "build-biotic":
        from evaluate.experiments.historical.cases.biotic import build_biotic

        build_biotic(archive, args.folder)
        return 0
    if args.command == "build-replant":
        from evaluate.experiments.historical.cases.replant import build_replant

        build_replant(archive, args.folder)
        return 0
    if args.command == "build-dryer-failure":
        from evaluate.experiments.historical.cases.dryer_failure import build_dryer_failure

        build_dryer_failure(archive, args.folder, backend=args.backend)
        return 0
    if args.command == "run-regional":
        run_regional(
            archive, load_json(args.profile), args.folder, backend=args.backend
        )
        return 0
    if args.backend == "wofost":
        from evaluate.experiments.historical.wofost_droughts import verify_wofost_droughts

        verify_wofost_droughts(archive, args.folder)
    else:
        verify(archive, args.folder)
    print("Verified historical L3 pair and L1/L2 evidence")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
