"""Historical interfaces exercised with engineering fixtures, never published as observations."""

import csv
from dataclasses import asdict, replace
from datetime import date, datetime, timedelta, timezone

import pytest

from evaluate.scenarios.historical_pilot import build, verify
from evaluate.scenarios.historical_scenarios import (
    call,
    compile_season,
    make_season_document,
    next_day,
    require,
    run_season,
)
from evaluate.scenarios.historical_slices import (
    SliceRunner,
    advance_public,
    capture,
    run_public_slice,
    verify_slice,
)
from evaluate.scenarios.historical_weather import (
    DATASET,
    POINT,
    TIMEZONE,
    import_archive,
    rank_seasons,
)
from evaluate.scenarios.lib.l3.soybean_full_season import soybean_full_season
from evaluate.scenarios.lib.l3.soybean_normal import soybean_normal
from fairy.acps.ontology.contracts import OperationRequest
from fairy.events.artifacts import assert_equivalent, load_json
from fairy.events.records import fingerprint
from fairy.events.session import Runtime


def test_cds_local_midnight_and_ending_interval(tmp_path):
    source = tmp_path / "cds-fixture.csv"
    start = datetime(2000, 7, 1, 16, tzinfo=timezone.utc)
    with source.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "time",
                "latitude",
                "longitude",
                "t2m",
                "d2m",
                "sp",
                "u10",
                "v10",
                "tp",
                "ssrd",
            ]
        )
        for offset in range(25):
            writer.writerow(
                [
                    (start + timedelta(hours=offset)).isoformat(),
                    *POINT,
                    293.15,
                    283.15,
                    99_000,
                    3,
                    4,
                    {0: 0.02, 24: 0.003}.get(offset, 0),
                    int(offset == 24) * 1e6,
                ]
            )
    archive = import_archive([source])
    row = archive["daily"]["2000-07-02"]
    assert row["rain_mm"] == pytest.approx(3)
    assert row["radiation_mj_m2"] == pytest.approx(1)
    assert row["wind_ms"] == pytest.approx(5)
    assert row["et0_mm"] > 0
    assert (
        "2000-07-01" in archive["missing_dates"]
        and "2000-07-03" in archive["missing_dates"]
    )
    assert archive["files"][0]["sha256"]
    assert 2000 in rank_seasons(archive)["excluded_years"]


@pytest.fixture(scope="module")
def archive():
    base = {
        "schema": 1,
        "dataset": DATASET,
        "point": list(POINT),
        "timezone": TIMEZONE,
        "daily": {},
        "files": [{"name": "synthetic engineering fixture, not ERA5-Land"}],
    }
    for year in range(1991, 2001):
        template = soybean_full_season() if year == 2000 else soybean_normal()
        for day in template.weather:
            key = date.fromisoformat(day.day).replace(year=year).isoformat()
            base["daily"][key] = asdict(replace(day, day=key))
    return {**base, "fingerprint": fingerprint(base)}


def test_l3_and_slices_end_to_end(archive, tmp_path):
    out = tmp_path / "pilot"
    assert build(archive, out), load_json(out / "selection.json")
    verify(archive, out)
    report = load_json(out / "selection.json")
    assert report["drought_year"] == 2000
    for short in ("l1", "l2"):
        assert load_json(out / f"{short}-public.json")["metrics"]["task_success"]
    zero = load_json(out / "drought-zero.json")
    reference = load_json(out / "drought-reference.json")
    assert (
        reference["metrics"]["stored_dry_grain_kg"]
        > zero["metrics"]["stored_dry_grain_kg"]
    )


def test_slice_scope_lineage_and_pending_recovery(archive):
    spec = compile_season(make_season_document(archive, 2000, "drought"), archive)
    ref, _ = run_season(spec, dose=("2000-07-20", 30))
    doc, checkpoint = capture(ref, "2000-07-20", "L1", "start.json")
    run = SliceRunner(doc, checkpoint)
    with pytest.raises(ValueError, match="scope"):
        run.invoke("irrigate_range", start_ridge=0, end_ridge=21, depth_mm=20)
    with pytest.raises(ValueError, match="deadline"):
        run.invoke("advance_time", hours=48)
    for tool in ("observe_soil_range", "inspect_crop", "irrigate_range"):
        extra = {"depth_mm": 30} if tool == "irrigate_range" else {}
        assert run.invoke(tool, start_ridge=22, end_ridge=43, **extra).ok
    assert run.status()["pending_irrigation_mm"] > 0
    restored = SliceRunner(doc, checkpoint, resume_state=run.runtime.export_state())
    unfinished = SliceRunner(
        doc, checkpoint, resume_state=run.runtime.export_state()
    ).finish("pending")
    assert not unfinished["metrics"]["task_success"]
    for current in (run, restored):
        advance_public(current.invoke, current.status())
        current.invoke("observe_soil_range", start_ridge=22, end_ridge=43)
    first, second = run.finish("continuous"), restored.finish("resumed")
    assert first["metrics"]["task_success"]
    assert_equivalent(first["run"]["state"], second["run"]["state"])
    assert verify_slice(doc, checkpoint, second, ref)["task_success"]
    wrong = {**ref, "agent": {}}
    with pytest.raises(ValueError, match="fingerprint"):
        verify_slice(doc, checkpoint, run_public_slice(doc, checkpoint), wrong)


def test_historical_provider_does_not_publish_future_weather(archive):
    doc = make_season_document(archive, 1992, "normal")
    runtime = Runtime(compile_season(doc, archive))
    assert "get_forecast" not in runtime.scenario.tools
    assert runtime.invoke(OperationRequest("advance_time", {"hours": 24})).ok
    report = runtime.invoke(OperationRequest("get_current_weather", {}))
    assert report.ok and report.data["source"] == "historical_reanalysis"
    assert report.data["weather"]["day"] == "1992-05-05"
    doc["environment"]["timeline"] = [{"stage": "R5"}]
    with pytest.raises(ValueError, match="stage-triggered"):
        compile_season(doc, archive)


def test_late_planting_changes_stage_not_weather(archive):
    spec = compile_season(make_season_document(archive, 1992, "normal"), archive)
    runtime = Runtime(spec)
    require(call(runtime, "prepare_seedbed", start_ridge=0, end_ridge=63))
    require(
        call(
            runtime, "plant", start_ridge=0, end_ridge=31, spacing_m=0.079, depth_mm=40
        )
    )
    for _ in range(10):
        next_day(runtime)
    require(
        call(
            runtime, "plant", start_ridge=32, end_ridge=63, spacing_m=0.079, depth_mm=40
        )
    )
    crop = require(call(runtime, "inspect_crop", start_ridge=0, end_ridge=63))[
        "receipt"
    ]["observations"]
    assert crop[0]["stage"] != crop[32]["stage"]
    assert (
        spec.weather
        == compile_season(
            make_season_document(archive, 1992, "normal"), archive
        ).weather
    )
    assert runtime.weather["1992-05-15"].day == "1992-05-15"


def test_regional_declaration_binds_forcing_sources_on_restore(archive):
    from zoneinfo import ZoneInfo

    from evaluate.scenarios.historical_scenarios import (
        make_regional_document,
        verify_season,
    )
    from fairy.acps.ontology.farm.spec import FarmSpec
    from fairy.events.artifacts import artifact, replay

    region = {
        **archive,
        "point": [43.75, 125.25],
        "requested_point": [43.82, 125.32],
        "timezone": "America/New_York",
    }

    def seal(weather):
        return {
            **weather,
            "fingerprint": fingerprint(
                {key: value for key, value in weather.items() if key != "fingerprint"}
            ),
        }

    def declaration(weather):
        return make_regional_document(
            weather,
            region="region_fixture",
            start=date(1992, 5, 5),
            end=date(1992, 10, 22),
            kind="baseline",
            farm=FarmSpec(),
            season=soybean_normal().season,
            quota_mm=9,
        )

    region = seal(region)
    doc = declaration(region)
    runtime = Runtime(compile_season(doc, region))
    assert runtime.zone == ZoneInfo(region["timezone"])
    assert runtime.now.isoformat() == "1992-05-05T07:00:00-04:00"
    next_day(runtime)
    complete_day = next(
        e for e in runtime.events if e.kind == "environment_day_completed"
    )
    assert (
        complete_day.payload["forcing"]["archive_fingerprint"] == region["fingerprint"]
    )
    record = artifact(runtime)
    verify_season(doc, region, record)
    restored = Runtime.from_state(record["state"])
    assert_equivalent(runtime.export_state(), restored.export_state())
    assert replay(record)[1]["replay_verified"]

    altered = seal({**region, "files": [{"name": "different source"}]})
    with pytest.raises(ValueError, match="different inputs"):
        verify_season(declaration(altered), altered, record)
    with pytest.raises(ValueError, match="archive contents"):
        compile_season(doc, {**region, "files": []})


def test_cold_planting_gate_uses_the_public_seedbed_and_is_transactional(archive):
    from evaluate.scenarios.historical_scenarios import (
        FULL,
        make_regional_document,
        verify_season,
    )
    from evaluate.scenarios.season import SeasonSpec
    from fairy.acps.ontology.farm.spec import FarmSpec
    from fairy.events.artifacts import artifact

    weather = {k: v for k, v in archive.items() if k != "fingerprint"}
    weather["daily"] = dict(weather["daily"])
    for key, temperature in (("1992-05-05", 5), ("1992-05-06", 20)):
        weather["daily"][key] = dict(
            weather["daily"][key], tmin_c=temperature, tmax_c=temperature, rain_mm=0
        )
    weather["fingerprint"] = fingerprint(weather)
    doc = make_regional_document(
        weather,
        region="cold_fixture",
        start=date(1992, 5, 5),
        end=date(1992, 10, 22),
        kind="cold_planting",
        farm=FarmSpec(),
        season=SeasonSpec(),
        quota_mm=0,
    )
    runtime = Runtime(compile_season(doc, weather))
    prepare = call(runtime, "prepare_seedbed", **FULL)
    assert prepare.ok
    args = dict(**FULL, spacing_m=0.079, depth_mm=40)
    assert call(runtime, "plant", **args).error == "SEEDBED_NOT_SETTLED"
    next_day(runtime)
    before_read = runtime.now
    cold = require(call(runtime, "observe_seedbed_range", **FULL))["receipt"]
    assert cold["settled"] and not cold["observations"][0]["temperature_ready"]
    assert runtime.now == before_read
    before = runtime.export_state()
    assert call(runtime, "plant", **args).error == "SEEDBED_TOO_COLD"
    after = runtime.export_state()
    for key in ("now", "backend_state", "physical_state"):
        assert before[key] == after[key]
    next_day(runtime)
    warm = require(call(runtime, "observe_seedbed_range", **FULL))["receipt"]
    assert warm["observations"][0]["temperature_ready"]
    assert call(runtime, "plant", **args).ok
    verify_season(doc, weather, artifact(runtime))
    assert "observe_seedbed_range" not in soybean_normal().tools


def test_flood_storage_conservation_and_drainage_receipts_survive_restore(archive):
    from copy import deepcopy
    from evaluate.scenarios.historical_scenarios import make_regional_document, window
    from evaluate.scenarios.season import SeasonSpec
    from fairy.acps.ontology.farm.spec import FarmSpec
    from fairy.events.artifacts import artifact, replay

    weather = {k: v for k, v in archive.items() if k != "fingerprint"}
    weather["daily"] = dict(weather["daily"])
    weather["daily"]["1992-05-05"] = dict(weather["daily"]["1992-05-05"], rain_mm=145)
    weather["fingerprint"] = fingerprint(weather)
    doc = make_regional_document(
        weather,
        region="flood_fixture",
        start=date(1992, 5, 5),
        end=date(1992, 10, 22),
        kind="flooding",
        farm=FarmSpec(),
        season=SeasonSpec(ponding_zones=((22, 43, 30, 4),)),
        quota_mm=0,
    )
    runtime = Runtime(compile_season(doc, weather))
    next_day(runtime)
    flux = next(
        e.payload["flux"]
        for e in runtime.events
        if e.kind == "environment_day_completed"
    )
    assert abs(flux["balance_error_mm"]) < 1e-8
    scope = window((22, 43))
    before = require(call(runtime, "observe_surface_water", **scope))["receipt"]
    assert all(row["standing_water_mm"] == 30 for row in before["observations"])
    stamp = runtime.now
    assert (
        call(runtime, "drain_surface", **window((21, 43)), max_depth_mm=20).error
        == "RIDGE_DRAINAGE_UNAVAILABLE"
    )
    result = require(call(runtime, "drain_surface", **scope, max_depth_mm=20))[
        "receipt"
    ]
    assert result["field_average_mm"] == pytest.approx(20 * 22 / 64)
    assert runtime.now == stamp
    checkpoint = runtime.export_state()
    resumed = Runtime.from_state(checkpoint)
    corrupted = deepcopy(checkpoint)
    event = next(
        e for e in reversed(corrupted["events"]) if e["kind"] == "operation_result"
    )
    event["payload"]["data"]["receipt"]["drained"][0]["depth_mm"] = 21
    with pytest.raises(ValueError, match="managed surface outflow"):
        Runtime.from_state(corrupted)
    for run in (runtime, resumed):
        next_day(run)
        observed = require(call(run, "observe_surface_water", **scope))["receipt"]
        assert all(row["standing_water_mm"] == 6 for row in observed["observations"])
    assert_equivalent(runtime.export_state(), resumed.export_state())
    assert replay(artifact(resumed))[1]["replay_verified"]
    assert "drain_surface" not in soybean_normal().tools


def test_biotic_treatment_is_private_until_settled_and_never_spends_twice(archive):
    from copy import deepcopy
    from evaluate.scenarios.historical_scenarios import (
        make_regional_document,
        plant_prefix,
        window,
    )
    from evaluate.scenarios.season import SeasonSpec
    from fairy.acps.ontology.farm.spec import FarmSpec
    from fairy.events.artifacts import artifact, replay

    day = "1992-06-12"
    zone = window((10, 11))
    doc = make_regional_document(
        archive,
        region="biotic_fixture",
        start=date(1992, 5, 5),
        end=date(1992, 10, 22),
        kind="insect_pressure",
        farm=FarmSpec(),
        season=SeasonSpec(
            biotic_outbreaks=((day, 10, 11, "insect", 0.75),), plant_protection_units=2
        ),
        quota_mm=0,
    )
    runtime = plant_prefix(compile_season(doc, archive))
    before = require(call(runtime, "scout_biotic", **zone))["receipt"]
    assert before["observations"][0]["insect_pressure"] is None
    while runtime.now.date().isoformat() < day:
        next_day(runtime)
    assert (
        require(call(runtime, "scout_biotic", **zone))["receipt"]["observations"][0][
            "insect_pressure"
        ]
        < 0.2
    )
    next_day(runtime)
    pulse = require(call(runtime, "scout_biotic", **zone))["receipt"]
    assert pulse["observations"][0]["insect_pressure"] > 0.7
    require(call(runtime, "treat_biotic", **zone, kind="insect"))
    queued = require(call(runtime, "scout_biotic", **zone))["receipt"]
    assert queued["observations"] == pulse["observations"]
    assert queued["pending_ridges"] == [10, 11]
    assert queued["protection_remaining_units"] == 0
    assert (
        call(runtime, "treat_biotic", **zone, kind="insect").error
        == "BIOTIC_TREATMENT_ALREADY_QUEUED"
    )
    snapshot = runtime.export_state()
    restored = Runtime.from_state(snapshot)
    corrupt = deepcopy(snapshot)
    corrupt["backend_state"]["state"]["biotic"][10]["pending"] = None
    with pytest.raises(ValueError, match="protection state disagrees"):
        Runtime.from_state(corrupt)
    for run in (runtime, restored):
        next_day(run)
        observed = require(call(run, "scout_biotic", **zone))["receipt"]
        assert not observed["pending_ridges"]
        assert observed["observations"][0]["insect_pressure"] < 0.6
        assert observed["observations"][0]["insect_protection_days"] > 0
        assert (
            call(run, "treat_biotic", **zone, kind="insect").error
            == "INSUFFICIENT_PROTECTION_STOCK"
        )
    assert_equivalent(runtime.export_state(), restored.export_state())
    assert replay(artifact(restored))[1]["replay_verified"]
    assert "scout_biotic" not in soybean_normal().tools


def test_partial_planter_gap_is_observed_after_emergence_and_repairs_only_missing_sites(
    archive,
):
    from copy import deepcopy
    from math import ceil
    from evaluate.scenarios.historical_scenarios import (
        make_regional_document,
        plant_prefix,
        window,
    )
    from evaluate.scenarios.season import SeasonSpec
    from fairy.acps.ontology.farm.spec import FarmSpec
    from fairy.events.artifacts import artifact, replay

    doc = make_regional_document(
        archive,
        region="stand_fixture",
        start=date(1992, 5, 5),
        end=date(1992, 10, 22),
        kind="planter_skip",
        farm=FarmSpec(),
        season=SeasonSpec(planter_skip_zones=((11, 13, 0.4),)),
        quota_mm=0,
    )
    runtime = plant_prefix(compile_season(doc, archive))
    gap = window((11, 13))
    row = lambda: require(call(runtime, "observe_stand_range", **gap))["receipt"][
        "observations"
    ][0]
    assert row()["established_fraction"] is None
    assert call(runtime, "replant_gaps", **gap).error == "STAND_REPAIR_WINDOW_CLOSED"
    for _ in range(24):
        if row()["established_fraction"] is not None:
            break
        next_day(runtime)
    assert row()["established_fraction"] == pytest.approx(0.6, abs=0.001)
    before = runtime.export_state()
    assert (
        call(runtime, "replant_gaps", **window((10, 13))).error
        == "NO_UNFILLED_SEED_SITES"
    )
    assert runtime.export_state()["physical_state"] == before["physical_state"]
    original = before["backend_state"]["state"]["ridges"][11]
    receipt = require(call(runtime, "replant_gaps", **gap))["receipt"]
    assert receipt["seeds_used"] == 3 * round(
        0.4 * ceil(2 * runtime.scenario.farm.length_m / 0.079)
    )
    after = runtime.export_state()
    assert after["backend_state"]["state"]["ridges"][11] == original
    assert row()["replant_stage"] == "pre_emergence"
    assert call(runtime, "replant_gaps", **gap).error == "NO_UNFILLED_SEED_SITES"
    corrupt = deepcopy(runtime.export_state())
    corrupt["backend_state"]["state"]["stands"][11]["replant"] = None
    with pytest.raises(ValueError, match="Replanted cohorts disagree"):
        Runtime.from_state(corrupt)
    restored = Runtime.from_state(runtime.export_state())
    assert_equivalent(restored.export_state(), runtime.export_state())
    assert replay(artifact(restored))[1]["replay_verified"]


def test_dryer_outage_after_a_stored_dried_batch_requires_service(archive):
    from copy import deepcopy

    from evaluate.scenarios.historical_scenarios import (
        grow_to_harvest,
        make_regional_document,
        move_to,
        window,
    )
    from evaluate.scenarios.season import SeasonSpec
    from fairy.acps.ontology.farm.spec import FarmSpec
    from fairy.events.artifacts import artifact, replay

    doc = make_regional_document(
        archive,
        region="dryer_fixture",
        start=date(1992, 5, 5),
        end=date(1992, 10, 22),
        kind="dryer_failure",
        farm=FarmSpec(),
        season=SeasonSpec(dryer_failure_after_dried_batches=1, dryer_repair_hours=6),
        quota_mm=0,
    )
    runtime, _ = grow_to_harvest(compile_season(doc, archive))

    def status():
        return require(call(runtime, "inspect_dryer"))["receipt"]["availability"]

    assert status() == "ready"
    assert call(runtime, "repair_dryer").error == "DRYER_SERVICE_NOT_REQUIRED"
    require(call(runtime, "harvest", **window((0, 1))))
    require(call(runtime, "unload"))
    require(call(runtime, "dry_grain", target_moisture=0.13))
    assert status() == "ready"
    require(call(runtime, "store_grain"))
    assert status() == "out_of_service"

    require(call(runtime, "harvest", **window((2, 3))))
    require(call(runtime, "unload"))
    broken = runtime.export_state()
    assert (
        call(runtime, "dry_grain", target_moisture=0.13).error == "DRYER_OUT_OF_SERVICE"
    )
    assert call(runtime, "store_grain").error == "UNSAFE_STORAGE_MOISTURE"
    assert runtime.export_state()["physical_state"] == broken["physical_state"]
    corrupt = deepcopy(runtime.export_state())
    corrupt["physical_state"]["state"]["dryer"].update(
        fault_active=False, repaired=True
    )
    with pytest.raises(ValueError, match="Dryer state disagrees"):
        Runtime.from_state(corrupt)

    runtime = Runtime.from_state(runtime.export_state())
    move_to(runtime, runtime.now.replace(hour=22, minute=0, second=0, microsecond=0))
    start = runtime.now
    service = require(call(runtime, "repair_dryer"))["receipt"]
    assert service["duration_hours"] == 6
    assert runtime.now.date() == start.date() + timedelta(days=1)
    assert (
        runtime.season_physical.state["trailer"]
        == broken["physical_state"]["state"]["trailer"]
    )
    assert status() == "ready"
    require(call(runtime, "dry_grain", target_moisture=0.13))
    require(call(runtime, "store_grain"))
    assert status() == "ready"
    _, replayed = replay(artifact(runtime))
    assert replayed["replay_verified"]


def test_historical_wofost_binds_proxy_and_preserves_historical_forcing(archive):
    pytest.importorskip("pcse")
    from evaluate.scenarios.historical_scenarios import make_regional_document
    from evaluate.scenarios.season import SeasonSpec
    from fairy.acps.ontology.farm.spec import FarmSpec

    document = make_season_document(archive, 2000, "drought")
    native = compile_season(document, archive)
    proxy = compile_season(document, archive, backend="wofost")
    assert proxy.weather == native.weather
    assert proxy.public_weather == native.public_weather
    assert proxy.timezone == native.timezone
    assert proxy.scenario_id != native.scenario_id
    assert proxy.season.cultivar == "Soybean_901_proxy"
    with pytest.raises(ValueError, match="requires HEINONG58"):
        Runtime(proxy)
    with pytest.raises(ValueError, match="explicitly declared Soybean_901_proxy"):
        Runtime(native, "wofost")

    for role, season in (
        ("flooding", SeasonSpec(ponding_zones=((0, 4, 30, 4),))),
        (
            "insect_pressure",
            SeasonSpec(biotic_outbreaks=(("2000-06-01", 0, 4, "insect", 0.8),)),
        ),
        (
            "disease_pressure",
            SeasonSpec(biotic_outbreaks=(("2000-06-01", 0, 4, "disease", 0.8),)),
        ),
        ("planter_skip", SeasonSpec(planter_skip_zones=((0, 4, 0.5),))),
    ):
        doc = make_regional_document(
            archive,
            region="harbin",
            start=date(2000, 5, 5),
            end=date(2000, 10, 22),
            kind=role,
            farm=FarmSpec(),
            season=season,
            quota_mm=0,
        )
        with pytest.raises(ValueError, match=f"capability unsupported: {role}"):
            compile_season(doc, archive, backend="wofost")


def test_wofost_seedbed_uses_settled_weather_and_restores(archive):
    pytest.importorskip("pcse")
    from evaluate.scenarios.historical_scenarios import (
        FULL,
        make_regional_document,
    )
    from evaluate.scenarios.season import SeasonSpec
    from fairy.acps.ontology.farm.spec import FarmSpec

    doc = make_regional_document(
        archive,
        region="harbin",
        start=date(2000, 5, 5),
        end=date(2000, 10, 22),
        kind="cold_planting",
        farm=FarmSpec(),
        season=SeasonSpec(),
        quota_mm=0,
    )
    spec = compile_season(doc, archive, backend="wofost")
    run = Runtime(spec, "wofost")
    before = require(call(run, "observe_seedbed_range", **FULL))["receipt"]
    assert not before["settled"]
    assert before["observations"][0]["seedbed_vwc"] is None
    assert (
        call(run, "plant", **FULL, spacing_m=0.079, depth_mm=40).error
        == "SEEDBED_NOT_SETTLED"
    )
    next_day(run)
    state = run.export_state()
    restored = Runtime.from_state(state)
    wet_day = spec.weather[0]
    expected = 10.0 + 0.4 * (
        (wet_day.tmin_c + wet_day.tmax_c) / 2 - 0.025 * wet_day.rain_mm - 10.0
    )
    for runtime in (run, restored):
        after = require(call(runtime, "observe_seedbed_range", **FULL))["receipt"]
        assert after["settled"]
        assert after["observations"][0]["seedbed_temperature_c"] == round(expected, 2)
    assert_equivalent(run.export_state(), restored.export_state())
