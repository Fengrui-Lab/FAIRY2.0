"""Shared declarative field/operation assembly; never constructs an engine."""

from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from evaluate.scenarios.season import SeasonSpec
    from fairy.acps.backends.biology.contracts import WeatherDay
    from fairy.acps.ontology.farm.spec import FarmSpec
from fairy.acps.ontology.contracts import ObjectSpec, OperationBinding
from fairy.acps.ontology.equipment.operations import PROVIDERS
from fairy.acps.ontology.crops.lib.soybean import SoybeanSpec
from evaluate.scenarios.spec import ScenarioSpec


def full_season_spec(
    *,
    scenario_id: str,
    weather: tuple[WeatherDay, ...],
    farm: FarmSpec,
    season: SeasonSpec,
    quota_mm: float,
    start_time: str = "2026-05-05T07:00:00+08:00",
    deadline: str = "2026-10-22T07:00:00+08:00",
    timezone: str = "Asia/Shanghai",
) -> ScenarioSpec:
    """声明全季场景输入、对象与操作，不创建引擎。"""
    objects = [
        ObjectSpec("plot-1", "plot"),
        ObjectSpec("weather-1", "weather_service", "plot-1"),
    ]
    bindings = [
        OperationBinding("get_current_weather", "weather-1"),
        OperationBinding("get_forecast", "weather-1"),
    ]
    for kind, operations in PROVIDERS.items():
        provider = kind + "-1"
        objects.append(ObjectSpec(provider, kind, "plot-1"))
        bindings.extend(OperationBinding(op.name, provider) for op in operations)
    bindings.append(OperationBinding("advance_time", "runtime"))
    return ScenarioSpec(
        scenario_id=scenario_id,
        task_type="full_season",
        season=season,
        task=f"Manage the unplanted 64-ridge {season.cultivar} soybean field through harvest and safe storage before the deadline. "
        "All ridges must be planted at 7.9 cm spacing and 4 cm depth, harvested, and all recovered grain safely stored "
        "at wet-basis moisture <= 0.135 with empty combine and trailer. The irrigation budget is "
        f"{quota_mm:g} mm averaged over the whole field. Stay within seed, fuel, battery and container capacities. "
        "Cultivar parameters are uncalibrated engineering proxies. Weather and observations are synthetic. This reduced model is not a validated field-yield predictor.",
        farm=farm,
        crop=SoybeanSpec(),
        sowing_date=start_time[:10],
        start_time=start_time,
        deadline=deadline,
        timezone=timezone,
        irrigation_quota_mm=quota_mm,
        max_advance_hours=720.0,
        weather=weather,
        public_weather=weather,
        weather_in_context=False,
        objects=tuple(objects),
        operation_bindings=tuple(bindings),
        tools=tuple(b.operation for b in bindings),
        seed=1202,
    )
