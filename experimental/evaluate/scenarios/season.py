"""Full-season scenario composition: crop parameters, spatial modifiers and resource limits."""

from __future__ import annotations
from typing import TYPE_CHECKING
from dataclasses import dataclass, fields
from datetime import date
from fairy.acps.ontology.farm.spec import finite


@dataclass(frozen=True)
class SeasonSpec:
    # Explicit scenario input; empty means homogeneous FarmSpec soil.
    """当前全季固定场景的作物、分区、装备与初始库存配置。

    生物参数尚未校准；种子为整数，容量为湿粮kg，安全含水率为湿基质量分数。"""
    hydraulic_zones: tuple[tuple[int, int, float, float, float], ...] = ()
    cultivar: str = "HEINONG58"
    ridge_count: int = 64
    seed_spacing_cm: float = 7.9
    sowing_depth_cm: float = 4.0
    emergence_gdd: float = 90.0
    maturity_gdd: float = 1120.0
    max_lai: float = 4.95
    rue_g_mj_par: float = 2.35
    grain_partition: float = 0.44
    # Fraction of gross grain (both dry matter and water) recovered by machinery.
    # An explicit engineering assumption, not a calibrated biological parameter.
    harvest_recovery_fraction: float = 0.96
    initial_seed_count: int = 1000000
    initial_fuel_l: float = 190.0
    combine_capacity_kg: float = 1800.0
    trailer_capacity_kg: float = 7000.0
    storage_capacity_kg: float = 20000.0
    safe_moisture: float = 0.135
    ponding_zones: tuple[tuple[int, int, float, float], ...] = ()
    biotic_outbreaks: tuple[tuple[str, int, int, str, float], ...] = ()
    plant_protection_units: int = 0
    planter_skip_zones: tuple[tuple[int, int, float], ...] = ()
    dryer_failure_after_dried_batches: int = 0
    dryer_repair_hours: float = 6.0

    def __post_init__(self) -> None:
        """验证声明的单位范围、身份与结构；非法输入明确报错，不静默修正。"""
        if self.cultivar not in ("HEINONG58", "Soybean_901_proxy"):
            raise ValueError(
                "Full-season supports declared HEINONG58 or Soybean_901_proxy only"
            )
        if type(self.ridge_count) is not int or self.ridge_count != 64:
            raise ValueError("The current spatial fixture has exactly 64 ridges")
        if type(self.initial_seed_count) is not int:
            raise ValueError("Seed inventory must be integral")
        if type(self.dryer_failure_after_dried_batches) is not int:
            raise ValueError("Dryer fault trigger must be an integer batch count")
        finite(self.dryer_repair_hours, "dryer repair duration", 0.01, 48)
        for f in fields(self):
            if f.name not in (
                "cultivar",
                "hydraulic_zones",
                "ponding_zones",
                "biotic_outbreaks",
                "planter_skip_zones",
            ):
                finite(getattr(self, f.name), f.name, 0)
        for name in (
            "seed_spacing_cm",
            "sowing_depth_cm",
            "emergence_gdd",
            "max_lai",
            "rue_g_mj_par",
            "combine_capacity_kg",
            "trailer_capacity_kg",
            "storage_capacity_kg",
        ):
            finite(getattr(self, name), name, 0.001)
        finite(self.maturity_gdd, "maturity_gdd", self.emergence_gdd + 1)
        finite(self.grain_partition, "grain_partition", 0.01, 1)
        finite(self.harvest_recovery_fraction, "harvest_recovery_fraction", 0, 1)
        if self.harvest_recovery_fraction == 0:
            raise ValueError("Harvest recovery must be positive")
        finite(self.seed_spacing_cm, "seed_spacing_cm", 1, 50)
        finite(self.sowing_depth_cm, "sowing_depth_cm", 1, 10)
        finite(self.safe_moisture, "safe_moisture", 0.10, 0.135)
        covered: set[int] = set()
        if not isinstance(self.hydraulic_zones, tuple):
            raise ValueError("Zones must be immutable")
        for zone in self.hydraulic_zones:
            if not isinstance(zone, tuple) or len(zone) != 5:
                raise ValueError("Invalid hydraulic zone")
            first, last, capacity, capture, efficiency = zone
            if (
                type(first) is not int
                or type(last) is not int
                or not 0 <= first <= last < self.ridge_count
            ):
                raise ValueError("Invalid hydraulic range")
            if covered.intersection(range(first, last + 1)):
                raise ValueError("Overlapping hydraulic zones")
            covered.update(range(first, last + 1))
            finite(capacity, "field capacity", 0.01, 0.99)
            finite(capture, "rain capture", 0, 1)
            finite(efficiency, "irrigation efficiency", 0.01, 1)

        surface_ridges: set[int] = set()
        if not isinstance(self.ponding_zones, tuple) or (
            self.ponding_zones and self.cultivar != "HEINONG58"
        ):
            raise ValueError("Ponding zones require the native HEINONG58 configuration")
        for ponding_zone in self.ponding_zones:
            if not isinstance(ponding_zone, tuple) or len(ponding_zone) != 4:
                raise ValueError("Invalid ponding zone")
            first, last, capacity_mm, passive_mm_day = ponding_zone
            if (
                type(first) is not int
                or type(last) is not int
                or not 0 <= first <= last < self.ridge_count
            ):
                raise ValueError("Invalid ponding ridge range")
            if surface_ridges.intersection(range(first, last + 1)):
                raise ValueError("Overlapping ponding zones")
            surface_ridges.update(range(first, last + 1))
            finite(capacity_mm, "ponding capacity", 0.01)
            finite(passive_mm_day, "passive drainage", 0.01, capacity_mm)

        if (
            not isinstance(self.biotic_outbreaks, tuple)
            or type(self.plant_protection_units) is not int
            or (self.biotic_outbreaks and self.cultivar != "HEINONG58")
            or (self.plant_protection_units and not self.biotic_outbreaks)
        ):
            raise ValueError("Invalid Native plant protection configuration")
        occupied: set[tuple[str, str, int]] = set()
        for pulse in self.biotic_outbreaks:
            if not isinstance(pulse, tuple) or len(pulse) != 5:
                raise ValueError("Invalid biotic outbreak declaration")
            day, start, end, kind, pressure = pulse
            if (
                date.fromisoformat(day).isoformat() != day
                or kind not in ("insect", "disease")
                or type(start) is not int
                or type(end) is not int
                or not 0 <= start <= end < self.ridge_count
            ):
                raise ValueError("Invalid biotic outbreak date, area or kind")
            finite(pressure, "outbreak pressure floor", 0.01, 1)
            affected = {(day, kind, i) for i in range(start, end + 1)}
            if affected & occupied:
                raise ValueError("Overlapping daily biotic outbreaks")
            occupied.update(affected)

        if not isinstance(self.planter_skip_zones, tuple) or (
            self.planter_skip_zones and self.cultivar != "HEINONG58"
        ):
            raise ValueError(
                "Planter skip zones require a Native HEINONG58 configuration"
            )
        skip_ridges: set[int] = set()
        for skip_zone in self.planter_skip_zones:
            if not isinstance(skip_zone, tuple) or len(skip_zone) != 3:
                raise ValueError("Invalid planter skip zone")
            first, last, fraction = skip_zone
            if (
                type(first) is not int
                or type(last) is not int
                or not 0 <= first <= last < self.ridge_count
            ):
                raise ValueError("Invalid planter skip range")
            area = set(range(first, last + 1))
            if skip_ridges & area:
                raise ValueError("Overlapping planter skip zones")
            skip_ridges.update(area)
            finite(fraction, "skipped seed fraction", 0.01, 0.95)

    @classmethod
    def from_dict(cls, value: dict) -> "SeasonSpec":
        """Decode JSON ridge tuples without changing the public constructor's immutability."""
        data = dict(value)
        for field_name in (
            "hydraulic_zones",
            "ponding_zones",
            "biotic_outbreaks",
            "planter_skip_zones",
        ):
            data[field_name] = tuple(tuple(zone) for zone in data.get(field_name, ()))
        return cls(**data)
