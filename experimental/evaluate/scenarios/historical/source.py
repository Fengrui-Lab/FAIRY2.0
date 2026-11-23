"""Compile repository-authored L3 inputs to the existing frozen historical format."""

import json
from datetime import date
from importlib.resources import files

from evaluate.scenarios.historical.compiler import make_regional_document
from evaluate.scenarios.season import SeasonSpec
from fairy.acps.ontology.contracts import JsonObject
from fairy.acps.ontology.farm.spec import FarmSpec


def _fields(value: object, path: str, *names: str) -> JsonObject:
    if not isinstance(value, dict) or set(value) != set(names):
        raise ValueError(f"{path} must contain {', '.join(names)}")
    return value


def _date(value: object, path: str) -> date:
    if not isinstance(value, str):
        raise ValueError(f"{path} must be an ISO date")
    parsed = date.fromisoformat(value)
    if value != parsed.isoformat():
        raise ValueError(f"{path} must use YYYY-MM-DD")
    return parsed


def load_historical_source(name: str, archive: JsonObject) -> JsonObject:
    """Read a repository scene, then bind it to the supplied historical archive."""
    source = files("evaluate").joinpath("scenarios/lib/l3/configs", name + ".json")
    return compile_historical_source(json.loads(source.read_text(encoding="utf-8")), archive)


def compile_historical_source(source: JsonObject, archive: JsonObject) -> JsonObject:
    """Translate the supported source vocabulary without inventing new engine effects."""
    _fields(source, "source", "format", "identity", "space", "initial_state", "objects", "evolution", "task")
    if source["format"] != "fairy.historical_l3.authoring.v1":
        raise ValueError("Unsupported historical scene source format")
    identity = _fields(source["identity"], "identity", "region", "level", "role")
    role = identity["role"]
    if identity["level"] != "L3" or role not in ("cold_planting", "dryer_failure"):
        raise ValueError("Unsupported historical scene source role or level")
    space = _fields(source["space"], "space", "length_m", "width_m")
    initial = _fields(source["initial_state"], "initial_state", "date", "soil_vwc", "crop", "stocks")
    crop = _fields(initial["crop"], "initial_state.crop", "cultivar", "planted")
    if crop["planted"] is not False:
        raise ValueError("This L3 source starts before planting")
    stock = _fields(initial["stocks"], "initial_state.stocks", "seeds", "fuel_l")
    objects = _fields(source["objects"], "objects", "layout", "grain_storage_capacity_kg")
    layout = objects["layout"]
    if layout not in ("grain_batches_v1", "grain_and_supply_v1"):
        raise ValueError("Unsupported L3 source equipment layout")
    evolution = _fields(source["evolution"], "evolution", "calendar", "crop_stage", "equipment")
    if evolution["crop_stage"] != []:
        raise ValueError("Dynamic crop-stage triggers are not implemented by this L3 source compiler")
    calendar = _fields(evolution["calendar"], "evolution.calendar", "weather", "analysis_windows")
    weather = _fields(calendar["weather"], "evolution.calendar.weather", "source", "requested_point", "timezone")
    if (
        weather["source"] != "historical_archive"
        or weather["requested_point"] != archive.get("requested_point")
        or weather["timezone"] != archive.get("timezone")
    ):
        raise ValueError("Weather archive does not match the scene source")
    windows = calendar["analysis_windows"]
    if not isinstance(windows, list) or len(windows) > 1:
        raise ValueError("This L3 source accepts at most one calendar analysis window")
    exposure = None
    if windows:
        window = _fields(windows[0], "analysis window", "start_date", "end_date")
        exposure = (
            _date(window["start_date"], "analysis window start"),
            _date(window["end_date"], "analysis window end"),
        )

    equipment = evolution["equipment"]
    failure_count, repair_hours = 0, SeasonSpec.dryer_repair_hours
    if role == "dryer_failure":
        if not isinstance(equipment, list) or len(equipment) != 1:
            raise ValueError("The dryer failure source requires one equipment event")
        event = _fields(equipment[0], "equipment event", "target_id", "trigger", "effect")
        trigger = _fields(event["trigger"], "equipment trigger", "operation", "condition", "count")
        effect = _fields(event["effect"], "equipment effect", "type", "repair_hours")
        if (
            event["target_id"] != "grain_dryer-1"
            or trigger["operation"] != "store_batch"
            or trigger["condition"] != "dried_by_target"
            or type(trigger["count"]) is not int
            or trigger["count"] < 1
            or effect["type"] != "lockout"
        ):
            raise ValueError("Unsupported dryer failure target, trigger or effect")
        failure_count, repair_hours = trigger["count"], effect["repair_hours"]
    elif equipment != []:
        raise ValueError("Unsupported equipment events for this L3 source role")

    task = _fields(source["task"], "task", "end_date", "irrigation_quota_mm")
    return make_regional_document(
        archive,
        region=identity["region"],
        start=_date(initial["date"], "initial_state.date"),
        end=_date(task["end_date"], "task.end_date"),
        kind=role,
        farm=FarmSpec(**space, initial_vwc=initial["soil_vwc"]),
        season=SeasonSpec(
            cultivar=crop["cultivar"],
            initial_seed_count=stock["seeds"],
            initial_fuel_l=stock["fuel_l"],
            storage_capacity_kg=objects["grain_storage_capacity_kg"],
            dryer_failure_after_dried_batches=failure_count,
            dryer_repair_hours=repair_hours,
        ),
        quota_mm=task["irrigation_quota_mm"],
        grain_batches=True,
        equipment_supply=layout == "grain_and_supply_v1",
        exposure=exposure,
    )
