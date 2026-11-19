"""Only supported, immutable rainfall-deficit declarations; no state patches."""

from __future__ import annotations
from typing import TYPE_CHECKING
from dataclasses import dataclass
from fairy.acps.ontology.farm.spec import finite

STAGES = ("vegetative", "R1", "R3", "R5", "R6", "R7")


@dataclass(frozen=True)
class Drought:
    event_id: str
    stage: str
    duration_days: int
    rainfall_factor: float = 0.0
    offset_days: int = 0
    target_id: str = "plot-1"
    kind: str = "rainfall_deficit"
    trigger_mode: str = "reference_stage_calendar"

    def __post_init__(self) -> None:
        """验证声明的单位范围、身份与结构；非法输入明确报错，不静默修正。"""
        if not isinstance(self.event_id, str) or not self.event_id.isidentifier():
            raise ValueError("Disturbance ID must be an identifier")
        if self.kind != "rainfall_deficit" or self.trigger_mode != "reference_stage_calendar":
            raise ValueError("Unsupported disturbance or trigger mode")
        if self.stage not in STAGES:
            raise ValueError("Unsupported drought stage")
        if type(self.duration_days) is not int or not 1 <= self.duration_days <= 171:
            raise ValueError("Drought duration must be a positive integral number of days")
        if type(self.offset_days) is not int or self.offset_days < 0:
            raise ValueError("Stage offset must be a nonnegative integer")
        if finite(self.rainfall_factor, "rainfall_factor", 0, 1) >= 1:
            raise ValueError("A drought must reduce rainfall")
        if not isinstance(self.target_id, str) or not self.target_id:
            raise ValueError("Disturbance target is required")
