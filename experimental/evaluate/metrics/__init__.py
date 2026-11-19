"""Explicit task evaluation dispatch shared by CLI, artifacts and replay."""

from __future__ import annotations
from typing import TYPE_CHECKING, TypedDict

if TYPE_CHECKING:
    from fairy.events.session import Runtime


class TaskMetrics(TypedDict, total=False):
    """按任务返回独立指标；L1/L3附加字段不混成通用综合分。"""

    task_success: bool
    replay_verified: bool | None
    tool_errors: int
    water_used_mm: float
    water_used_m3: float
    required_queries_completed: bool
    target_reached: bool
    observed_before_irrigation: bool
    observed_after_settlement: bool
    root_vwc: float
    planted_ridges: int
    harvested_ridges: int
    stored_grain_kg: float
    stored_dry_grain_kg: float
    safe_storage: bool
    containers_empty: bool
    dry_mass_balance_error_kg: float
    water_mass_balance_error_kg: float
    fuel_used_l: float
    electricity_kwh: float
    model_status: str


def evaluate(runtime: Runtime, replay_verified: bool | None = None) -> TaskMetrics:
    """按任务类型选择评价器；工程成功、资源消耗与回放分别报告。"""
    if runtime.scenario.task_type == "irrigation":
        from .irrigation import evaluate as evaluator
    elif runtime.scenario.task_type == "full_season":
        from .season import evaluate as evaluator
    else:
        raise ValueError("No evaluator registered for this task")
    return evaluator(runtime, replay_verified)
