"""Compile historical declarations and validate frozen scenario inputs."""

from __future__ import annotations

from dataclasses import asdict, replace
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from evaluate.scenarios.historical.weather import (
    DATASET,
    OPEN_METEO_DATASET,
    POINT,
    TIMEZONE,
    YEARS,
    season_dates,
    verify_archive,
)
from evaluate.scenarios.lib.l3.common import full_season_spec
from evaluate.scenarios.season import SeasonSpec
from evaluate.scenarios.spec import ScenarioSpec
from fairy.acps.backends.biology.contracts import WeatherDay
from fairy.acps.ontology.contracts import JsonObject, ObjectSpec, OperationBinding
from fairy.acps.ontology.farm.spec import FarmSpec
from fairy.events.records import canonical, fingerprint

ZONE = ZoneInfo(TIMEZONE)
QUOTA_MM = 12.0
SOURCE_TASK = "scenario_full_season_hb_dryr5r6_hn58_std_waterlimit"
PHASES = ("R5", "R6")


def boundary(day: date, hour: int = 7, *, zone: ZoneInfo = ZONE) -> datetime:
    return datetime.combine(day, time(hour), zone)


def make_season_document(archive: JsonObject, year: int, kind: str) -> JsonObject:
    """Declare a historical season and the explicit initial-state assumptions."""
    if kind not in ("normal", "drought") or year not in YEARS:
        raise ValueError("Unknown historical season identity")
    farm = FarmSpec(length_m=268, width_m=64 * 1.1, initial_vwc=0.30)
    season = SeasonSpec(hydraulic_zones=((22, 43, 0.285, 0.82, 0.90),))
    start, stop = season_dates(year)
    return {
        "schema": 1,
        "identity": {
            "id": f"soybean_l3_hn58_historical_{kind}_{year}_v1",
            "level": "L3",
            "role": kind,
            "source_task": SOURCE_TASK,
        },
        "space": {
            "farm": asdict(farm),
            "ridge_count": 64,
            "layout": "parallel_equal_rectangles",
            "coordinates": "local_metres",
            "ridge_id_range": [0, 63],
        },
        "initial_state": {
            "time": boundary(start).isoformat(),
            "planted": False,
            "basis": "matched_simulation_assumptions",
            "season": asdict(season),
        },
        "environment": {
            "weather": {
                "dataset": archive["dataset"],
                "point": archive["point"],
                "year": year,
                "archive_fingerprint": archive["fingerprint"],
            },
            "timeline": (
                [
                    {
                        "type": "historical_exposure_window",
                        "start_date": date(year, 7, 1).isoformat(),
                        "end_date": date(year, 8, 15).isoformat(),
                        "expected_stages": ["R5", "R6"],
                        "effect": "record_only",
                        "evidence": "season_selection_precipitation_minus_et0",
                    }
                ]
                if kind == "drought"
                else []
            ),
        },
        "objects": {"layout": "native_season_standard_v1"},
        "task": {
            "deadline": boundary(stop).isoformat(),
            "quota_mm": QUOTA_MM,
            "success": "safe_full_season_storage",
        },
    }


def make_regional_document(
    archive: JsonObject,
    *,
    region: str,
    start: date,
    end: date,
    kind: str,
    farm: FarmSpec,
    season: SeasonSpec,
    quota_mm: float,
    exposure: tuple[date, date] | None = None,
    grain_batches: bool = False,
    equipment_supply: bool = False,
) -> JsonObject:
    """Declare a regional season using observed calendar weather and explicit resources."""
    if (
        start >= end
        or start.year != end.year
        or not region.isidentifier()
        or kind
        not in (
            "normal",
            "drought",
            "baseline",
            "cold_planting",
            "flooding",
            "insect_pressure",
            "disease_pressure",
            "planter_skip",
            "dual_dry_patch",
            "wet_harvest",
            "dryer_failure",
        )
    ):
        raise ValueError("Invalid regional season identity")
    if equipment_supply and kind not in ("cold_planting", "planter_skip"):
        raise ValueError(
            "Equipment supply is qualified for cold planting and planter skip"
        )
    if (
        grain_batches
        and kind not in ("wet_harvest", "dryer_failure")
        and not (equipment_supply and kind == "cold_planting")
    ):
        raise ValueError(
            "Batch logistics is currently qualified for harvest and dryer scenarios"
        )
    suffix, layout = {
        (False, False): ("", "native_season_standard_v1"),
        (True, False): ("_batches", "grain_batches_v1"),
        (False, True): ("_supply", "equipment_supply_v1"),
        (True, True): ("_integrated", "grain_and_supply_v1"),
    }[(grain_batches, equipment_supply)]
    zone = ZoneInfo(archive["timezone"])
    timeline = []
    if exposure is not None:
        first, last = exposure
        if not start <= first <= last < end:
            raise ValueError("Exposure window outside the regional season")
        timeline.append(
            dict(
                type="historical_exposure_window",
                start_date=first.isoformat(),
                end_date=last.isoformat(),
                effect="record_only",
                expected_stages=(
                    ["unplanted"]
                    if kind == "cold_planting"
                    else (
                        ["R8"]
                        if kind == "wet_harvest"
                        else (
                            ["R1", "R3", "R5"]
                            if kind == "disease_pressure"
                            else list(PHASES)
                        )
                    )
                ),
                evidence="regional_scenario_declaration",
            )
        )
    return {
        "schema": 2,
        "identity": {
            "id": f"soybean_l3_{region}_{kind}_{start.year}_v2{suffix}",
            "level": "L3",
            "role": kind,
            "region": region,
        },
        "space": {
            "farm": asdict(farm),
            "ridge_count": season.ridge_count,
            "layout": "parallel_equal_rectangles",
            "coordinates": "local_metres",
            "ridge_id_range": [0, season.ridge_count - 1],
        },
        "initial_state": {
            "time": boundary(start, zone=zone).isoformat(),
            "planted": False,
            "basis": "declared_regional_assumptions",
            "season": asdict(season),
        },
        "environment": {
            "weather": {
                "dataset": archive["dataset"],
                "point": archive["point"],
                "requested_point": archive.get("requested_point", archive["point"]),
                "timezone": zone.key,
                "year": start.year,
                "archive_fingerprint": archive["fingerprint"],
            },
            "timeline": timeline,
        },
        "objects": {"layout": layout},
        "task": {
            "deadline": boundary(end, zone=zone).isoformat(),
            "quota_mm": quota_mm,
            "success": "safe_full_season_storage",
        },
    }


def compile_season(
    document: JsonObject, archive: JsonObject, *, backend: str = "native"
) -> ScenarioSpec:
    """Validate and compile declared inputs, retaining archive and document identity."""
    verify_archive(archive)
    schema = document.get("schema")
    layout = document["objects"]["layout"]
    batch_layout = layout in ("grain_batches_v1", "grain_and_supply_v1")
    supply_layout = layout in ("equipment_supply_v1", "grain_and_supply_v1")
    if schema not in (1, 2) or document["identity"]["level"] != "L3":
        raise ValueError("Unsupported historical L3 schema")
    if any(
        event.get("type") != "historical_exposure_window"
        or event.get("effect") != "record_only"
        for event in document["environment"]["timeline"]
    ):
        raise ValueError(
            "Historical v1 does not implement additional or stage-triggered effects"
        )
    pointer = document["environment"]["weather"]
    if (
        archive.get("schema") != 1
        or archive.get("dataset") not in (DATASET, OPEN_METEO_DATASET)
        or pointer.get("dataset") != archive.get("dataset")
        or pointer.get("point") != archive.get("point")
        or pointer.get("archive_fingerprint") != archive.get("fingerprint")
    ):
        raise ValueError("Weather archive identity mismatch")
    year = pointer["year"]
    role = document["identity"]["role"]
    zone = ZoneInfo(archive["timezone"])
    farm = FarmSpec(**document["space"]["farm"])
    season = SeasonSpec.from_dict(document["initial_state"]["season"])
    if schema == 1:
        if (
            archive.get("requested_point", list(POINT)) != list(POINT)
            or zone.key != TIMEZONE
        ):
            raise ValueError("Weather archive identity mismatch for the Harbin pilot")
        start, end = season_dates(year)
        expected = make_season_document(archive, year, role)
    else:
        start = datetime.fromisoformat(document["initial_state"]["time"]).date()
        end = datetime.fromisoformat(document["task"]["deadline"]).date()
        timeline = document["environment"]["timeline"]
        if len(timeline) > 1:
            raise ValueError(
                "A regional declaration accepts at most one exposure window"
            )
        exposure = (
            (
                date.fromisoformat(timeline[0]["start_date"]),
                date.fromisoformat(timeline[0]["end_date"]),
            )
            if timeline
            else None
        )
        expected = make_regional_document(
            archive,
            region=document["identity"]["region"],
            start=start,
            end=end,
            kind=role,
            farm=farm,
            season=season,
            quota_mm=document["task"]["quota_mm"],
            exposure=exposure,
            grain_batches=batch_layout,
            equipment_supply=supply_layout,
        )
    if fingerprint(document) != fingerprint(expected):
        raise ValueError("Unsupported or inconsistent historical scenario declaration")
    if backend not in ("native", "wofost"):
        raise ValueError(f"Unknown historical biology backend: {backend}")
    identity = document["identity"]["id"]
    if season.cultivar != "HEINONG58":
        raise ValueError("Historical source declarations require HEINONG58")
    if backend == "wofost":
        if role in ("flooding", "insect_pressure", "disease_pressure", "planter_skip"):
            raise ValueError(
                f"WOFOST historical scenario capability unsupported: {role}"
            )
        season = replace(season, cultivar="Soybean_901_proxy")
        identity += "_wofost_901_proxy"
    days = [
        (start + timedelta(days=offset)).isoformat()
        for offset in range((end - start).days + 1)
    ]
    try:
        weather = [archive["daily"][day] for day in days]
        yesterday = (start - timedelta(days=1)).isoformat()
        previous = (
            [archive["daily"][yesterday]] if yesterday in archive["daily"] else []
        )
    except KeyError as exc:
        raise ValueError("Season has missing historic forcing") from exc
    if any(row["day"] != day for row, day in zip(weather, days, strict=True)):
        raise ValueError("Archived day key does not match the weather date")
    spec = full_season_spec(
        scenario_id=identity,
        weather=tuple(WeatherDay(**row) for row in weather),
        farm=farm,
        season=season,
        quota_mm=document["task"]["quota_mm"],
        start_time=document["initial_state"]["time"],
        deadline=document["task"]["deadline"],
        timezone=zone.key,
    ).to_dict()
    spec.update(
        task=(
            f"Manage unplanted {season.cultivar} soybean through safe storage under the declared water "
            "quota. Weather is historical gridded reanalysis; only completed daily reports "
            "are public. Soil and operations are simulated."
        ),
        sowing_date=start.isoformat(),
        start_time=document["initial_state"]["time"],
        deadline=document["task"]["deadline"],
        public_weather=previous + weather,
        objects=[
            (
                asdict(ObjectSpec(**row))
                if row["type_id"] != "weather_service"
                else {**row, "type_id": "historical_weather_service"}
            )
            for row in spec["objects"]
        ],
        operation_bindings=[
            row
            for row in spec["operation_bindings"]
            if row["operation"] != "get_forecast"
        ],
        tools=[name for name in spec["tools"] if name != "get_forecast"],
        max_advance_hours=48.0,
        construction_json=canonical(
            {
                "kind": "historical",
                "document": document,
                **(
                    {"backend": "wofost", "cultivar": season.cultivar}
                    if backend == "wofost"
                    else {}
                ),
                "weather_fingerprint": fingerprint(weather),
                "public_weather_fingerprint": fingerprint(previous + weather),
            }
        ),
    )
    if batch_layout:
        from fairy.acps.ontology.equipment.operations import GRAIN_PROVIDERS

        replaced = {"unload", "dry_grain", "store_grain", "inspect_inventory"}
        spec["objects"] = [
            obj for obj in spec["objects"] if obj["type_id"] != "grain_facility"
        ]
        spec["operation_bindings"] = [
            row
            for row in spec["operation_bindings"]
            if row["operation"] not in replaced
        ]
        spec["tools"] = [name for name in spec["tools"] if name not in replaced]
        for kind, operations in GRAIN_PROVIDERS.items():
            provider = kind + "-1"
            spec["objects"].append(asdict(ObjectSpec(provider, kind, farm.plot_id)))
            for op in operations:
                if op.name == "repair_dryer" and role != "dryer_failure":
                    continue
                spec["operation_bindings"].append(
                    asdict(OperationBinding(op.name, provider))
                )
                spec["tools"].append(op.name)
    if supply_layout:
        from fairy.acps.ontology.equipment.supply import (
            MOUNT_OPERATIONS,
            SUPPLY_OBJECTS,
        )

        tractor_id = next(
            obj["object_id"] for obj in spec["objects"] if obj["type_id"] == "tractor"
        )
        for kind in SUPPLY_OBJECTS:
            spec["objects"].append(asdict(ObjectSpec(kind + "-1", kind, farm.plot_id)))
        for provider, operations in (
            (tractor_id, MOUNT_OPERATIONS),
            ("supply_warehouse-1", SUPPLY_OBJECTS["supply_warehouse"]),
        ):
            for op in operations:
                spec["operation_bindings"].append(
                    asdict(OperationBinding(op.name, provider))
                )
                spec["tools"].append(op.name)
    if role == "cold_planting":
        spec["task"] = (
            "Use the seedbed sensor to wait until the modeled topsoil has a settled local-day "
            "reading and the selected ridges meet the planting temperature threshold. Prepare "
            "and plant within the available window, confirm emergence, and complete safe storage."
        )
        if batch_layout and supply_layout:
            spec["task"] += (
                " Use the correct mounted implement and supply seeds and fuel from the warehouse. "
                "Harvest in batches, unload to the trailer, dry wet grain and safely store each batch by ID."
            )
        spec["objects"].append(
            asdict(ObjectSpec("seedbed-1", "seedbed_sensor", farm.plot_id))
        )
        spec["operation_bindings"].append(
            asdict(OperationBinding("observe_seedbed_range", "seedbed-1"))
        )
        spec["tools"].append("observe_seedbed_range")
    if role == "flooding":
        if not season.ponding_zones:
            raise ValueError("Flooding requires declared ponding zones")
        spec["task"] = (
            "Observe standing water and modeled crop waterlogging in the low-lying zones. "
            "Drain settled standing water when observed and monitor the next local-day condition. "
            "Complete safe harvest and storage. Current and future aggregate rainfall is withheld."
        )
        provider = "surface-drainage-1"
        spec["objects"].append(
            asdict(ObjectSpec(provider, "surface_drainage", farm.plot_id))
        )
        for name in ("observe_surface_water", "drain_surface"):
            spec["operation_bindings"].append(asdict(OperationBinding(name, provider)))
            spec["tools"].append(name)
    if role in ("insect_pressure", "disease_pressure"):
        if not season.biotic_outbreaks:
            raise ValueError("A crop protection case requires declared pressure inputs")
        spec["task"] = (
            "Scout the field for simulated insect feeding and disease symptoms. Select protection "
            "only after field evidence, verify its next-day response and revisit if pressure returns. "
            "Complete safe harvest and storage under the shared protection inventory."
        )
        provider = "crop-protection-1"
        spec["objects"].append(
            asdict(ObjectSpec(provider, "crop_protection", farm.plot_id))
        )
        for name in ("scout_biotic", "treat_biotic"):
            spec["operation_bindings"].append(asdict(OperationBinding(name, provider)))
            spec["tools"].append(name)
    if role == "planter_skip":
        if not season.planter_skip_zones:
            raise ValueError("Planter skip case requires a declared missed area")
        spec["task"] = (
            "Inspect stand after emergence to identify missed seed sites. Replant only the affected "
            "vegetative ridges and confirm emergence. Harvest and store when both cohorts are mature."
        )
        for name, provider in (
            ("observe_stand_range", "stand_sensor"),
            ("replant_gaps", "stand_repair_tractor"),
        ):
            object_id = provider + "-1"
            spec["objects"].append(
                asdict(ObjectSpec(object_id, provider, farm.plot_id))
            )
            spec["operation_bindings"].append(asdict(OperationBinding(name, object_id)))
            spec["tools"].append(name)
    if role == "dryer_failure":
        if not season.dryer_failure_after_dried_batches:
            raise ValueError("Dryer failure case requires a declared batch trigger")
        spec["task"] = (
            "Manage harvest and storage in feasible batches. Inspect the dryer when needed, "
            "service any fault and ensure all grain is safely stored by the deadline."
        )
        if batch_layout:
            return ScenarioSpec.from_dict(spec)
        object_id = "dryer-maintenance-1"
        spec["objects"].append(
            asdict(ObjectSpec(object_id, "dryer_maintenance", farm.plot_id))
        )
        for name in ("inspect_dryer", "repair_dryer"):
            spec["operation_bindings"].append(asdict(OperationBinding(name, object_id)))
            spec["tools"].append(name)
    return ScenarioSpec.from_dict(spec)


def validate_historical_construction(spec: ScenarioSpec, manifest: JsonObject) -> None:
    """Check the frozen declaration and forcing when loading or restoring a scenario."""
    document = manifest["document"]
    source = document["environment"]["weather"]
    layout = document["objects"]["layout"]
    batch_layout = layout in ("grain_batches_v1", "grain_and_supply_v1")
    supply_layout = layout in ("equipment_supply_v1", "grain_and_supply_v1")
    from fairy.acps.ontology.equipment.supply import SUPPLY_ACTIONS, SUPPLY_OBJECTS

    supply_types = {obj.type_id for obj in spec.objects} & set(SUPPLY_OBJECTS)
    if supply_types != (set(SUPPLY_OBJECTS) if supply_layout else set()) or (
        set(spec.tools) & SUPPLY_ACTIONS
    ) != (SUPPLY_ACTIONS if supply_layout else set()):
        raise ValueError(
            "Historical equipment supply layout differs from declared equipment"
        )
    facilities = {obj.type_id for obj in spec.objects} & {
        "grain_trailer",
        "grain_dryer",
        "grain_warehouse",
    }
    if facilities != (
        {"grain_trailer", "grain_dryer", "grain_warehouse"} if batch_layout else set()
    ) or any(
        (name in spec.tools) != batch_layout
        for name in ("unload_batch", "dry_batch", "store_batch")
    ):
        raise ValueError(
            "Historical batch layout differs from declared grain facilities"
        )
    source_season = SeasonSpec.from_dict(document["initial_state"]["season"])
    backend = manifest.get("backend", "native")
    identity = document["identity"]["id"]
    if backend == "wofost":
        if (
            manifest.get("cultivar") != "Soybean_901_proxy"
            or source_season.ponding_zones
            or source_season.biotic_outbreaks
            or source_season.planter_skip_zones
        ):
            raise ValueError("Invalid historical WOFOST capability or cultivar")
        source_season = replace(source_season, cultivar="Soybean_901_proxy")
        identity += "_wofost_901_proxy"
    if (
        canonical(manifest) != spec.construction_json
        or document["schema"] not in (1, 2)
        or backend not in ("native", "wofost")
        or identity != spec.scenario_id
        or document["initial_state"]["time"] != spec.start_time
        or document["task"]["deadline"] != spec.deadline
        or document["task"]["quota_mm"] != spec.irrigation_quota_mm
        or source.get("timezone", TIMEZONE) != spec.timezone
        or source["dataset"] not in (DATASET, OPEN_METEO_DATASET)
        or len(source["archive_fingerprint"]) != 64
        or fingerprint(document["space"]["farm"]) != fingerprint(asdict(spec.farm))
        or spec.season is None
        or fingerprint(asdict(source_season)) != fingerprint(asdict(spec.season))
        or manifest["weather_fingerprint"]
        != fingerprint([asdict(row) for row in spec.weather])
        or manifest["public_weather_fingerprint"]
        != fingerprint([asdict(row) for row in spec.public_weather])
        or spec.weather_in_context
        or "get_forecast" in spec.tools
        or bool(spec.season.ponding_zones)
        != (document["identity"]["role"] == "flooding")
        or any(
            (tool in spec.tools) != (document["identity"]["role"] == "flooding")
            for tool in ("observe_surface_water", "drain_surface")
        )
        or bool(spec.season.biotic_outbreaks)
        != (document["identity"]["role"] in ("insect_pressure", "disease_pressure"))
        or any(
            (tool in spec.tools) != bool(spec.season.biotic_outbreaks)
            for tool in ("scout_biotic", "treat_biotic")
        )
        or bool(spec.season.planter_skip_zones)
        != (document["identity"]["role"] == "planter_skip")
        or any(
            (tool in spec.tools) != bool(spec.season.planter_skip_zones)
            for tool in ("observe_stand_range", "replant_gaps")
        )
        or bool(spec.season.dryer_failure_after_dried_batches)
        != (document["identity"]["role"] == "dryer_failure")
        or ("inspect_dryer" in spec.tools)
        != (batch_layout or bool(spec.season.dryer_failure_after_dried_batches))
        or ("repair_dryer" in spec.tools)
        != bool(spec.season.dryer_failure_after_dried_batches)
        or ("observe_seedbed_range" in spec.tools)
        != (document["identity"]["role"] == "cold_planting")
    ):
        raise ValueError(
            "Historical construction metadata differs from scenario inputs"
        )
