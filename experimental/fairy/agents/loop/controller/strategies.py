"""Trusted built-in strategy types; model metadata cannot grant privilege."""

from __future__ import annotations
from typing import TYPE_CHECKING, TypeGuard

if TYPE_CHECKING:
    from evaluate.scenarios.spec import ScenarioSpec
    from fairy.agents.models.decision import PrivilegedAgent, PublicAgent
from .oracle import IrrigationOracle
from .season_oracle import SeasonOracle


def is_privileged(agent: PublicAgent | PrivilegedAgent) -> TypeGuard[PrivilegedAgent]:
    """只信任明确的内置策略类，不根据模型metadata中的privileged字段授予权限。"""
    return type(agent) in (IrrigationOracle, SeasonOracle)


def oracle_for(scenario: ScenarioSpec) -> PrivilegedAgent:
    """为声明任务选择可信脚本参考，未知任务拒绝。"""
    if scenario.task_type == "irrigation":
        return IrrigationOracle()
    if scenario.task_type == "full_season":
        return SeasonOracle()
    raise ValueError("No reference strategy for this task")
