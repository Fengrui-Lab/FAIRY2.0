"""Explicit engineering proxy. Fixed dates are NOT WOFOST R1/R5 assertions."""

from __future__ import annotations
from typing import Sequence, TYPE_CHECKING

if TYPE_CHECKING:
    from evaluate.scenarios.spec import ScenarioSpec
    from fairy.acps.ontology.contracts import JsonObject
from dataclasses import replace
from evaluate.scenarios.lib.l3.common import full_season_spec
from evaluate.scenarios.normal import normal_weather
from evaluate.scenarios.season import SeasonSpec
from fairy.acps.ontology.farm.spec import FarmSpec

NORMAL_ID = "soybean_l3_901_proxy_normal_v1"
CALENDAR_ID = "soybean_l3_901_proxy_calendar_drought_v1"


def soybean_wofost_normal() -> ScenarioSpec:
    """返回明确后端/品种范围的不可变场景声明，不实例化引擎或运行Agent。"""
    return full_season_spec(
        scenario_id=NORMAL_ID,
        weather=normal_weather("2026-05-05", "2026-10-22"),
        farm=FarmSpec(length_m=268, width_m=64 * 1.1, initial_vwc=0.31),
        season=replace(SeasonSpec(), cultivar="Soybean_901_proxy"),
        quota_mm=60.0,
    )


def soybean_wofost_calendar_drought() -> ScenarioSpec:
    """返回明确后端/品种范围的不可变场景声明，不实例化引擎或运行Agent。"""
    normal = soybean_wofost_normal()
    weather = tuple(
        (
            replace(day, rain_mm=0.0)
            if "2026-06-02" <= day.day <= "2026-06-13" or "2026-06-30" <= day.day <= "2026-07-09"
            else day
        )
        for day in normal.weather
    )
    return replace(normal, scenario_id=CALENDAR_ID, weather=weather, public_weather=weather)


# Reference is generated once via public operations, never during scenario creation.
from pathlib import Path
from importlib.resources import files
import json
from fairy.events.records import canonical, fingerprint

REFERENCE_RESOURCE = "lib/baselines/l3/wofost_proxy_reference.json"
REFERENCE_PATH = Path(__file__).parents[2] / REFERENCE_RESOURCE
PHASE_ID = "soybean_l3_901_proxy_reproductive_drought_v1"


def process_identity() -> dict[str, str]:
    """记录影响WOFOST代理参考日历的过程文件哈希。"""
    import hashlib
    from importlib.resources import files

    paths = (
        "acps/backends/biology/wofost/season.py",
        "acps/backends/biology/wofost/backend.py",
        "acps/backends/biology/wofost/soybean.yaml",
        "acps/backends/physical/native/backend.py",
        "events/session.py",
        "acps/backends/biology/contracts.py",
    )
    return {p: hashlib.sha256(files("fairy").joinpath(p).read_bytes()).hexdigest() for p in paths}


def proxy_manifest(
    spec: ScenarioSpec, events: Sequence[JsonObject] = (), reference: JsonObject | None = None
) -> ScenarioSpec:
    """将代理场景与固定天气/参考来源绑定，返回带构建描述的新声明。"""
    normal = soybean_wofost_normal()
    manifest = dict(
        schema=2,
        kind="wofost_proxy_fixed_weather",
        baseline_fingerprint=fingerprint(normal.to_dict()),
        events=list(events),
        reference=reference,
        weather_fingerprint=fingerprint(
            [__import__("dataclasses").asdict(d) for d in spec.weather]
        ),
    )
    return replace(spec, construction_json=canonical(manifest))


def soybean_wofost_reproductive_drought() -> ScenarioSpec:
    """返回明确后端/品种范围的不可变场景声明，不实例化引擎或运行Agent。"""
    from datetime import date, timedelta

    normal = soybean_wofost_normal()
    reference = json.loads(files("evaluate").joinpath("scenarios", REFERENCE_RESOURCE).read_text(encoding="utf-8"))
    if reference["process_identity"] != process_identity() or reference[
        "baseline_fingerprint"
    ] != fingerprint(normal.to_dict()):
        raise ValueError("WOFOST reference changed; explicitly rebuild and validate")
    first = date.fromisoformat(reference["phase_starts"]["reproductive"]) + timedelta(days=5)
    last = first + timedelta(days=39)
    if last >= date.fromisoformat(reference["phase_starts"]["mature"]):
        raise ValueError("Reference drought exceeds reproductive window")
    events = [
        dict(
            event_id="reproductive_rainfall_deficit",
            start_day=first.isoformat(),
            end_day=last.isoformat(),
        )
    ]
    weather = tuple(
        replace(d, rain_mm=0.0) if first.isoformat() <= d.day <= last.isoformat() else d
        for d in normal.weather
    )
    return proxy_manifest(
        replace(normal, scenario_id=PHASE_ID, weather=weather, public_weather=weather),
        events,
        reference,
    )


def validate_proxy_construction(spec: ScenarioSpec, manifest: JsonObject) -> None:
    """核查代理品种、参考身份和实际天气，不重排干旱或替换后端。"""
    from dataclasses import asdict

    if (
        set(manifest)
        != {"schema", "kind", "baseline_fingerprint", "events", "reference", "weather_fingerprint"}
        or canonical(manifest) != spec.construction_json
    ):
        raise ValueError("Invalid WOFOST construction metadata")
    normal = soybean_wofost_normal()
    if manifest["baseline_fingerprint"] != fingerprint(normal.to_dict()):
        raise ValueError("Proxy baseline changed")
    clean = replace(
        spec,
        construction_json=None,
        scenario_id=normal.scenario_id,
        weather=normal.weather,
        public_weather=normal.public_weather,
    )
    if clean.to_dict() != normal.to_dict():
        raise ValueError("Proxy initial conditions changed")
    reference = manifest["reference"]
    if (
        reference is None
        or reference["process_identity"] != process_identity()
        or reference["baseline_fingerprint"] != fingerprint(normal.to_dict())
    ):
        raise ValueError("Invalid WOFOST reference identity")
    from datetime import date, timedelta

    start = date.fromisoformat(reference["phase_starts"]["reproductive"]) + timedelta(days=5)
    end = start + timedelta(days=39)
    if end >= date.fromisoformat(reference["phase_starts"]["mature"]):
        raise ValueError("Invalid proxy phase interval")
    expected = [
        dict(
            event_id="reproductive_rainfall_deficit",
            start_day=start.isoformat(),
            end_day=end.isoformat(),
        )
    ]
    if manifest["events"] != expected:
        raise ValueError("Proxy event date mismatch")
    weather = tuple(
        replace(d, rain_mm=0.0) if start.isoformat() <= d.day <= end.isoformat() else d
        for d in normal.weather
    )
    if (
        spec.weather != weather
        or spec.public_weather != weather
        or manifest["weather_fingerprint"] != fingerprint([asdict(d) for d in weather])
    ):
        raise ValueError("Proxy forcing differs from frozen recipe")
