"""Immutable farm geometry and soil parameters, in public SI-based units."""

from __future__ import annotations
from typing import Any, TYPE_CHECKING
from dataclasses import dataclass
from math import isfinite


def finite(
    value: Any, name: str, minimum: float | None = None, maximum: float | None = None
) -> float:
    """验证数值有限性与边界，排除bool和溢出，返回float表示。"""
    try:
        valid = not isinstance(value, bool) and isinstance(value, (int, float)) and isfinite(value)
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError(f"{name} must be a finite number")
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} must be <= {maximum}")
    return float(value)


@dataclass(frozen=True)
class FarmSpec:
    """农场几何与固定根区参数；面积m²、深度m、水分m³/m³，效率为毛水到有效水的比例。"""

    farm_id: str = "pilot"
    plot_id: str = "plot-1"
    length_m: float = 100.0
    width_m: float = 10.0
    latitude_deg: float = 45.7
    longitude_deg: float = 126.6
    elevation_m: float = 150.0
    root_depth_m: float = 0.4
    wilting_point_vwc: float = 0.14
    field_capacity_vwc: float = 0.31
    saturation_vwc: float = 0.45
    initial_vwc: float = 0.22
    irrigation_efficiency: float = 0.92

    def __post_init__(self) -> None:
        """验证声明的单位范围、身份与结构；非法输入明确报错，不静默修正。"""
        if any(
            not isinstance(value, str) or not value.strip()
            for value in (self.farm_id, self.plot_id)
        ):
            raise ValueError("Farm and plot need string identifiers")
        for name in ("length_m", "width_m", "root_depth_m"):
            if finite(getattr(self, name), name, 0) == 0:
                raise ValueError(f"{name} must be positive")
        if finite(self.area_m2, "area_m2", 0) == 0:
            raise ValueError("Derived farm area must be positive")
        finite(self.root_depth_m * 1000, "root storage scale", 0)
        finite(self.latitude_deg, "latitude_deg", -90, 90)
        finite(self.longitude_deg, "longitude_deg", -180, 180)
        finite(self.elevation_m, "elevation_m")
        for name in ("wilting_point_vwc", "field_capacity_vwc", "saturation_vwc", "initial_vwc"):
            finite(getattr(self, name), name, 0, 1)
        if not 0 <= self.wilting_point_vwc < self.field_capacity_vwc < self.saturation_vwc <= 1:
            raise ValueError("Soil requires wilting < field capacity < saturation")
        if not self.wilting_point_vwc <= self.initial_vwc <= self.saturation_vwc:
            raise ValueError("Initial water content outside modeled range")
        finite(self.irrigation_efficiency, "irrigation_efficiency", 0.01, 1)

    @property
    def area_m2(self) -> float:
        """计算农场面积m²；不隐式换算为公顷。"""
        return self.length_m * self.width_m

    def volume_m3(self, depth_mm: float) -> float:
        """将全田平均水深mm换算为体积m³，验证有限非负输入。"""
        depth = finite(depth_mm, "depth_mm", 0)
        return finite(self.area_m2 * (depth / 1000.0), "water volume", 0)
