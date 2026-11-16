"""Backend contract: one call consumes exactly one completed physical day."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import date
from typing import Protocol
from fairy.acps.ontology.farm.spec import finite


@dataclass(frozen=True)
class WeatherDay:
    day: str
    tmin_c: float = 16.0
    tmax_c: float = 28.0
    rain_mm: float = 0.0
    radiation_mj_m2: float = 18.0
    wind_ms: float = 2.0
    vapor_pressure_hpa: float = 12.0
    et0_mm: float = 3.0

    def __post_init__(self):
        date.fromisoformat(self.day)
        finite(self.tmin_c, "tmin_c", -60, 60)
        finite(self.tmax_c, "tmax_c", self.tmin_c, 60)
        for name in ("rain_mm", "radiation_mj_m2", "wind_ms", "vapor_pressure_hpa", "et0_mm"):
            finite(getattr(self, name), name, 0)


@dataclass(frozen=True)
class BackendCapabilities:
    backend_id: str
    model: str
    version: str
    crops: tuple[str, ...] = ("soybean",)
    actions: tuple[str, ...] = ("irrigate",)
    timestep: str = "local_day"


@dataclass(frozen=True)
class SoilObservation:
    plot_id: str
    root_vwc: float
    measurement: str = "root_zone_vwc_rounded_0.001"
    unit: str = "m3/m3"


class BiologyBackend(Protocol):
    capabilities: BackendCapabilities

    def observe(self) -> SoilObservation: ...
    def advance_day(self, weather: WeatherDay, irrigation_mm: float) -> dict: ...
    def audit(self) -> dict: ...
    def export_state(self) -> dict: ...
    def load_state(self, payload: dict) -> None: ...



def _native(farm, crop, sowing_date):
    from .native.backend import NativeBackend
    return NativeBackend(farm, crop, sowing_date)


def _wofost(farm, crop, sowing_date):
    from .wofost.backend import WofostBackend
    return WofostBackend(farm, crop, sowing_date)


class BiologyBackendRegistry:
    """Frozen explicit backend factories. Imports occur only for the selected backend."""

    def __init__(self, entries):
        from types import MappingProxyType
        factories = {}
        for name, factory in entries:
            if name in factories:
                raise ValueError(f"Duplicate backend: {name}")
            if not isinstance(name, str) or not name or not callable(factory):
                raise ValueError("Invalid backend registration")
            factories[name] = factory
        self.factories = MappingProxyType(factories)

    def create(self, name, farm, crop, sowing_date):
        if name not in self.factories:
            raise ValueError(f"Unsupported backend: {name}")
        backend = self.factories[name](farm, crop, sowing_date)
        if backend.capabilities.backend_id != name:
            raise ValueError("Backend identity disagrees with registration")
        for method in ("observe", "advance_day", "audit", "export_state", "load_state"):
            if not callable(getattr(backend, method, None)):
                raise ValueError(f"Backend missing interface: {method}")
        return backend


BIOLOGY_BACKENDS = BiologyBackendRegistry((("native", _native), ("wofost", _wofost)))


def make_backend(name, farm, crop, sowing_date, season=None):
    if season is not None:
        if name != "native":
            raise ValueError("Backend lacks full-season HEINONG58 and harvest capabilities; no fallback")
        from .native.season import NativeSeasonBackend
        return NativeSeasonBackend(farm, crop, sowing_date, season)
    return BIOLOGY_BACKENDS.create(name, farm, crop, sowing_date)


def restore_backend(name, farm, crop, sowing_date, payload, season=None):
    backend = make_backend(name, farm, crop, sowing_date, season)
    backend.load_state(payload)
    return backend
