"""Normal environment and management knowledge, separate from Agent policy."""

from __future__ import annotations
from typing import Mapping, TYPE_CHECKING

if TYPE_CHECKING:
    from fairy.acps.ontology.contracts import JsonObject
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from fairy.acps.backends.biology.contracts import WeatherDay


@dataclass(frozen=True)
class ManagementPhase:
    """管理知识的阶段目标与证据提示；不自动执行阶段操作。"""

    name: str
    stages: tuple[str, ...]
    goal: str
    observations: tuple[str, ...]
    operations: tuple[str, ...]


@dataclass(frozen=True)
class ManagementTemplate:
    """有版本身份的管理阶段集合；与场景物理天气、Agent策略分开。"""

    template_id: str
    phases: tuple[ManagementPhase, ...]

    def to_dict(self) -> JsonObject:
        """返回脱离内部对象的序列化声明，供来源指纹或可信恢复使用。"""
        return asdict(self)


SOYBEAN_NORMAL_MANAGEMENT = ManagementTemplate(
    "hn58_reduced_management_v1",
    (
        ManagementPhase(
            "preparation",
            ("unplanted",),
            "Prepare a trafficable seedbed and verify resources.",
            ("inspect_inventory", "observe_soil_range", "get_current_weather"),
            ("prepare_seedbed",),
        ),
        ManagementPhase(
            "establishment",
            ("unplanted", "pre_emergence"),
            "Plant the declared cultivar, spacing and depth; verify emergence.",
            ("inspect_crop", "observe_soil_range"),
            ("plant",),
        ),
        ManagementPhase(
            "vegetative",
            ("vegetative",),
            "Monitor crop and root-zone water; respond only to observed needs.",
            ("inspect_crop", "observe_soil_range", "get_forecast"),
            ("irrigate_range",),
        ),
        ManagementPhase(
            "reproductive",
            ("R1", "R3"),
            "Monitor flowering and pod establishment without assuming an abnormality.",
            ("inspect_crop", "observe_soil_range"),
            ("irrigate_range",),
        ),
        ManagementPhase(
            "pod_fill",
            ("R5", "R6"),
            "Monitor water and crop development; respect resource limits.",
            ("inspect_crop", "observe_soil_range"),
            ("irrigate_range",),
        ),
        ManagementPhase(
            "harvest",
            ("R7", "R8"),
            "Harvest mature, trafficable ridges within equipment capacity.",
            ("inspect_crop", "inspect_inventory", "get_current_weather"),
            ("harvest", "unload"),
        ),
        ManagementPhase(
            "storage",
            ("harvested",),
            "Dry when necessary and safely store all recovered grain.",
            ("inspect_inventory",),
            ("dry_grain", "store_grain"),
        ),
    ),
)

# Idealized engineering forcing, not Harbin station data or agronomic calibration.
NORMAL_WEATHER_PARAMETERS = dict(
    tmin_c=16.0,
    tmax_c=28.0,
    rain_mm=3.0,
    radiation_mj_m2=18.0,
    wind_ms=2.0,
    vapor_pressure_hpa=12.0,
    et0_mm=3.0,
)


def normal_weather(
    start: str, end: str, parameters: Mapping[str, float] | None = None
) -> tuple[WeatherDay, ...]:
    """生成显式理想化固定天气，不伪称历史站点记录。"""
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    if last < first:
        raise ValueError("Normal weather interval is reversed")
    values = dict(NORMAL_WEATHER_PARAMETERS if parameters is None else parameters)
    if set(values) != set(NORMAL_WEATHER_PARAMETERS):
        raise ValueError("Incomplete normal weather profile")
    return tuple(
        WeatherDay((first + timedelta(days=i)).isoformat(), **values)
        for i in range((last - first).days + 1)
    )
