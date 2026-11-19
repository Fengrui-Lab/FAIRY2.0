"""Normal and composed cases are declarations only, with identical public tasks."""

from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from evaluate.scenarios.spec import ScenarioSpec
from fairy.acps.ontology.farm.spec import FarmSpec
from evaluate.scenarios.season import SeasonSpec
from evaluate.scenarios.normal import normal_weather
from evaluate.scenarios.disturbances import Drought
from evaluate.scenarios.compose import attach_normal, compile_scenario
from evaluate.scenarios.lib.l3.common import full_season_spec

NORMAL_ID = "soybean_l3_hn58_normal_v1"
R1_DROUGHT = Drought("flowering_drought", "R1", duration_days=12)
R5_DROUGHT = Drought("podfill_drought", "R5", duration_days=10)


def soybean_normal() -> ScenarioSpec:
    """返回明确后端/品种范围的不可变场景声明，不实例化引擎或运行Agent。"""
    return attach_normal(
        full_season_spec(
            scenario_id=NORMAL_ID,
            weather=normal_weather("2026-05-05", "2026-10-22"),
            farm=FarmSpec(length_m=268, width_m=64 * 1.1, initial_vwc=0.31),
            season=SeasonSpec(),
            quota_mm=60.0,
        )
    )


def soybean_drought_r1() -> ScenarioSpec:
    """返回明确后端/品种范围的不可变场景声明，不实例化引擎或运行Agent。"""
    return compile_scenario(
        soybean_normal(), (R1_DROUGHT,), scenario_id="soybean_l3_hn58_drought_r1_v1"
    )


def soybean_drought_r5() -> ScenarioSpec:
    """返回明确后端/品种范围的不可变场景声明，不实例化引擎或运行Agent。"""
    return compile_scenario(
        soybean_normal(), (R5_DROUGHT,), scenario_id="soybean_l3_hn58_drought_r5_v1"
    )


def soybean_drought_r1_r5() -> ScenarioSpec:
    """返回明确后端/品种范围的不可变场景声明，不实例化引擎或运行Agent。"""
    return compile_scenario(
        soybean_normal(), (R1_DROUGHT, R5_DROUGHT), scenario_id="soybean_l3_hn58_drought_r1_r5_v1"
    )
