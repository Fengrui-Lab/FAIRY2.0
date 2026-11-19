"""Explicit, uncalibrated reduced soybean parameters for the L1 pilot."""

from __future__ import annotations
from typing import TYPE_CHECKING
from dataclasses import dataclass
from fairy.acps.ontology.farm.spec import finite


@dataclass(frozen=True)
class SoybeanSpec:
    """L1简化大豆参数及PCSE品种选择；Native GDD与PCSE DVS语义不同。"""

    crop_id: str = "soybean"
    wofost_variety: str = "Soybean_901"
    base_temperature_c: float = 8.0
    emergence_gdd: float = 95.0
    maturity_gdd: float = 1100.0
    canopy_cover: float = 0.35
    crop_coefficient: float = 1.0

    def __post_init__(self) -> None:
        """验证声明的单位范围、身份与结构；非法输入明确报错，不静默修正。"""
        if self.crop_id != "soybean" or self.wofost_variety != "Soybean_901":
            raise ValueError("Pilot supports only soybean / Soybean_901")
        finite(self.base_temperature_c, "base_temperature_c", 0, 30)
        finite(self.emergence_gdd, "emergence_gdd", 1)
        finite(self.maturity_gdd, "maturity_gdd", self.emergence_gdd + 1)
        finite(self.canopy_cover, "canopy_cover", 0, 1)
        finite(self.crop_coefficient, "crop_coefficient", 0, 2)
