"""Immutable farm geometry and soil parameters, in public SI-based units."""
from dataclasses import dataclass
from math import isfinite


def finite(value, name: str, minimum=None, maximum=None) -> float:
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

    def __post_init__(self):
        if any(not isinstance(value, str) or not value.strip() for value in (self.farm_id, self.plot_id)):
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
    def area_m2(self):
        return self.length_m * self.width_m

    def volume_m3(self, depth_mm):
        depth = finite(depth_mm, "depth_mm", 0)
        return finite(self.area_m2 * (depth / 1000.0), "water volume", 0)
