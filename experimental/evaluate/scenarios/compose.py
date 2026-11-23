"""Compile supported disturbances into frozen inputs, never instantiate engines."""

from __future__ import annotations
from typing import Iterable, Sequence, TYPE_CHECKING

if TYPE_CHECKING:
    from evaluate.scenarios.spec import ScenarioSpec
    from fairy.acps.backends.biology.contracts import WeatherDay
    from fairy.acps.ontology.contracts import JsonObject
from dataclasses import asdict, replace
from datetime import date, timedelta
from importlib.resources import files
import hashlib
import json
from pathlib import Path
from fairy.events.records import canonical, detached, fingerprint
from evaluate.scenarios.normal import (
    NORMAL_WEATHER_PARAMETERS,
    SOYBEAN_NORMAL_MANAGEMENT,
    normal_weather,
)
from evaluate.scenarios.disturbances import Drought, STAGES

REFERENCE_RESOURCE = "lib/baselines/l3/hn58_normal_reference.json"
REFERENCE_PATH = Path(__file__).parent / REFERENCE_RESOURCE
PROCESS_FILES = (
    "fairy/acps/backends/biology/native/season.py",
    "fairy/acps/backends/biology/native/soil_engine.py",
    "fairy/acps/backends/biology/native/canopy_biomass_engine.py",
    "fairy/acps/backends/biology/native/biotic_pressure_engine.py",
    "fairy/acps/backends/physical/native/backend.py",
    "fairy/acps/ontology/crops/lib/soybean.py",
    "fairy/events/session.py",
    "evaluate/scenarios/season.py",
)


def process_files() -> dict[str, str]:
    """记录影响冻结Native参考日历的过程文件哈希。"""
    result = {}
    for name in PROCESS_FILES:
        package, relative = name.split("/", 1)
        result[name] = hashlib.sha256(
            files(package).joinpath(relative).read_bytes()
        ).hexdigest()
    return result


def weather_fingerprint(weather: Iterable[WeatherDay]) -> str:
    """对完整逐日天气规范化计算内容身份。"""
    return fingerprint([asdict(day) for day in weather])


def base_inputs(spec: ScenarioSpec) -> JsonObject:
    """提取基线声明供构建身份校验，不包含活引擎。"""
    return {
        k: v
        for k, v in spec.to_dict().items()
        if k not in ("scenario_id", "weather", "public_weather", "construction_json")
    }


def normal_manifest(spec: ScenarioSpec) -> JsonObject:
    """构造无扰动基线的来源与管理模板描述。"""
    return {
        "schema": 1,
        "baseline_id": spec.scenario_id,
        "baseline_fingerprint": fingerprint(spec.to_dict()),
        "base_inputs_fingerprint": fingerprint(base_inputs(spec)),
        "normal_profile": {
            "kind": "synthetic_constant_v1",
            "parameters": NORMAL_WEATHER_PARAMETERS,
        },
        "normal_weather_fingerprint": weather_fingerprint(spec.weather),
        "compiled_weather_fingerprint": weather_fingerprint(spec.weather),
        "management_template": SOYBEAN_NORMAL_MANAGEMENT.to_dict(),
        "reference_calendar": None,
        "reference_fingerprint": None,
        "events": [],
        "recipe_fingerprint": fingerprint([]),
    }


def attach_normal(spec: ScenarioSpec) -> ScenarioSpec:
    """为声明场景附加经过校验的normal构建描述。"""
    if spec.construction_json is not None:
        raise ValueError("Normal declaration already has construction metadata")
    return replace(spec, construction_json=canonical(normal_manifest(spec)))


def compile_scenario(
    normal: ScenarioSpec, disturbances: Sequence[Drought], *, scenario_id: str
) -> ScenarioSpec:
    """按冻结参考阶段日历编译降雨扰动；不依候选动作重新排期。"""
    if not isinstance(disturbances, tuple) or any(
        type(d) is not Drought for d in disturbances
    ):
        raise ValueError(
            "Only an immutable tuple of supported Drought declarations is accepted"
        )
    if not isinstance(normal.construction_json, str):
        raise ValueError("Compile from a declared normal baseline")
    manifest = json.loads(normal.construction_json)
    if manifest["events"]:
        raise ValueError("Compile from normal, not from an already disturbed scenario")
    if len({d.event_id for d in disturbances}) != len(disturbances):
        raise ValueError("Duplicate disturbance ID")
    reference = (
        json.loads(files("evaluate").joinpath("scenarios", REFERENCE_RESOURCE).read_text(encoding="utf-8"))
        if disturbances
        else None
    )
    if reference:
        validate_reference(reference, manifest)
    events = []
    for disturbance in disturbances:
        if disturbance.target_id != normal.farm.plot_id:
            raise ValueError(
                "First-round drought supports the whole declared plot only"
            )
        assert reference is not None  # 非空扰动已加载并验证冻结日历。
        starts = reference["stage_starts"]
        start = date.fromisoformat(starts[disturbance.stage]) + timedelta(
            days=disturbance.offset_days
        )
        end = start + timedelta(days=disturbance.duration_days - 1)
        next_stage = (STAGES + ("R8",))[(STAGES + ("R8",)).index(disturbance.stage) + 1]
        if end >= date.fromisoformat(starts[next_stage]):
            raise ValueError("Drought exceeds the reference stage window")
        events.append(
            {
                **asdict(disturbance),
                "start_day": start.isoformat(),
                "end_day": end.isoformat(),
            }
        )
    events.sort(key=lambda e: (e["start_day"], e["event_id"]))
    occupied = set()
    weather = list(normal.weather)
    for event in events:
        for i, day in enumerate(weather):
            if event["start_day"] <= day.day <= event["end_day"]:
                if day.day in occupied:
                    raise ValueError(
                        "Overlapping rainfall modifiers require an explicit merge rule"
                    )
                occupied.add(day.day)
                weather[i] = replace(
                    day, rain_mm=day.rain_mm * event["rainfall_factor"]
                )
    manifest.update(
        events=events,
        reference_calendar=reference,
        reference_fingerprint=fingerprint(reference) if reference else None,
        recipe_fingerprint=fingerprint(events),
        compiled_weather_fingerprint=weather_fingerprint(weather),
    )
    return replace(
        normal,
        scenario_id=scenario_id,
        weather=tuple(weather),
        public_weather=tuple(weather),
        construction_json=canonical(manifest),
    )


def validate_reference(reference: JsonObject, manifest: JsonObject) -> None:
    """校验参考日历、过程文件与normal输入身份一致。"""
    if (
        reference.get("schema") != 1
        or reference.get("reference_backend") != "native"
        or reference.get("baseline_fingerprint") != manifest["baseline_fingerprint"]
        or reference.get("template_fingerprint")
        != fingerprint(manifest["management_template"])
        or reference.get("process_files_sha256") != process_files()
    ):
        raise ValueError(
            "Reference calendar does not match normal/model; explicitly rebuild and revalidate"
        )
    starts = reference["stage_starts"]
    keys = ("pre_emergence",) + STAGES + ("R8",)
    values = [date.fromisoformat(starts[k]) for k in keys]
    if values != sorted(set(values)):
        raise ValueError("Invalid reference stage ordering")


def validate_construction(spec: ScenarioSpec) -> None:
    """Restore also verifies the recipe actually produces the persisted forcing."""
    if not isinstance(spec.construction_json, str) or spec.task_type != "full_season":
        raise ValueError("Construction metadata requires a full-season declaration")
    manifest = json.loads(spec.construction_json)
    if manifest.get("kind") == "historical":
        from evaluate.scenarios.historical.compiler import validate_historical_construction

        validate_historical_construction(spec, manifest)
        return
    if (
        manifest.get("schema") == 2
        and manifest.get("kind") == "wofost_proxy_fixed_weather"
    ):
        from evaluate.scenarios.lib.l3.soybean_wofost import validate_proxy_construction

        validate_proxy_construction(spec, manifest)
        return
    required = {
        "schema",
        "baseline_id",
        "baseline_fingerprint",
        "base_inputs_fingerprint",
        "normal_profile",
        "normal_weather_fingerprint",
        "compiled_weather_fingerprint",
        "management_template",
        "reference_calendar",
        "reference_fingerprint",
        "events",
        "recipe_fingerprint",
    }
    if type(manifest) is not dict or set(manifest) != required:
        raise ValueError("Incomplete or unexpected construction fields")
    if canonical(manifest) != spec.construction_json or manifest.get("schema") != 1:
        raise ValueError("Construction metadata must be canonical schema-1 JSON")
    if manifest["management_template"] != detached(SOYBEAN_NORMAL_MANAGEMENT.to_dict()):
        raise ValueError("Unsupported management template")
    if fingerprint(base_inputs(spec)) != manifest["base_inputs_fingerprint"]:
        raise ValueError("Normal constraints or initial conditions changed")
    profile = manifest["normal_profile"]
    if profile != {
        "kind": "synthetic_constant_v1",
        "parameters": NORMAL_WEATHER_PARAMETERS,
    }:
        raise ValueError("Unsupported normal forcing profile")
    normal = normal_weather(spec.sowing_date, spec.deadline[:10], profile["parameters"])
    if weather_fingerprint(normal) != manifest["normal_weather_fingerprint"]:
        raise ValueError("Normal weather identity mismatch")
    base = spec.to_dict()
    base.update(
        scenario_id=manifest["baseline_id"],
        construction_json=None,
        weather=[asdict(w) for w in normal],
        public_weather=[asdict(w) for w in normal],
    )
    if fingerprint(base) != manifest["baseline_fingerprint"]:
        raise ValueError("Normal declaration identity mismatch")
    events, reference = manifest["events"], manifest["reference_calendar"]
    if (
        type(events) is not list
        or fingerprint(events) != manifest["recipe_fingerprint"]
    ):
        raise ValueError("Invalid disturbance recipe fingerprint")
    if events != sorted(events, key=lambda e: (e["start_day"], e["event_id"])):
        raise ValueError("Compiled events must be ordered")
    if events:
        validate_reference(reference, manifest)
        if fingerprint(reference) != manifest["reference_fingerprint"]:
            raise ValueError("Reference calendar fingerprint mismatch")
    elif reference is not None or manifest["reference_fingerprint"] is not None:
        raise ValueError("Normal baseline must not carry a future disturbance calendar")
    factors, ids = {}, set()
    for event in events:
        declaration = Drought(
            **{k: v for k, v in event.items() if k not in ("start_day", "end_day")}
        )
        if declaration.target_id != spec.farm.plot_id or declaration.event_id in ids:
            raise ValueError("Duplicate disturbance or unsupported target")
        ids.add(declaration.event_id)
        start = date.fromisoformat(
            reference["stage_starts"][declaration.stage]
        ) + timedelta(days=declaration.offset_days)
        end = start + timedelta(days=declaration.duration_days - 1)
        next_stage = (STAGES + ("R8",))[(STAGES + ("R8",)).index(declaration.stage) + 1]
        if (
            event["start_day"] != start.isoformat()
            or event["end_day"] != end.isoformat()
            or end >= date.fromisoformat(reference["stage_starts"][next_stage])
        ):
            raise ValueError("Compiled date disagrees with the stage anchor")
        for i in range(declaration.duration_days):
            day = (start + timedelta(days=i)).isoformat()
            if day in factors or day not in {w.day for w in normal}:
                raise ValueError("Conflicting or out-of-range forcing")
            factors[day] = declaration.rainfall_factor
    expected = tuple(
        replace(w, rain_mm=w.rain_mm * factors.get(w.day, 1)) for w in normal
    )
    if (
        expected != spec.weather
        or expected != spec.public_weather
        or weather_fingerprint(expected) != manifest["compiled_weather_fingerprint"]
    ):
        raise ValueError("Compiled forcing does not match the normal plus recipe")


def forcing_audit(spec: ScenarioSpec, day: str) -> JsonObject | None:
    """返回该日适用扰动的可信来源信息；不将完整配方暴露给Agent。"""
    if spec.construction_json is None:
        return None
    manifest = json.loads(spec.construction_json)
    if manifest.get("kind") == "historical":
        source = manifest["document"]["environment"]["weather"]
        return {
            "construction_fingerprint": fingerprint(manifest),
            "archive_fingerprint": source["archive_fingerprint"],
            "disturbance_ids": [
                f"biotic:{kind}:{first}-{last}:{day}"
                for date_key, first, last, kind, _ in (spec.season.biotic_outbreaks if spec.season else ())
                if date_key == day
            ],
        }
    return {
        "construction_fingerprint": fingerprint(manifest),
        "disturbance_ids": [
            e["event_id"]
            for e in manifest["events"]
            if e["start_day"] <= day <= e["end_day"]
        ],
    }
