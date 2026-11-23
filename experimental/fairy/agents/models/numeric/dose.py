"""The numerical policy sees public evidence for an already chosen operation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class RidgeEvidence:
    ridge_id: int
    phase: str
    root_vwc: float
    wilting_vwc: float
    field_capacity_vwc: float


@dataclass(frozen=True)
class CompletedWeather:
    day: str
    rain_mm: float
    et0_mm: float


@dataclass(frozen=True)
class IrrigationDoseInput:
    """Decision-time evidence and a local depth bound in gross millimetres."""

    time: datetime
    ridges: tuple[RidgeEvidence, ...]
    maximum_depth_mm: float
    last_settled_zone_depth_mm: float
    weather: CompletedWeather | None


@dataclass(frozen=True)
class DoseChoice:
    depth_mm: float | None


class IrrigationDosePolicy(Protocol):
    """Return a dose or abstain after the controller checks evidence and constraints."""

    @property
    def identity(self) -> str: ...

    def choose(self, evidence: IrrigationDoseInput) -> DoseChoice: ...


@dataclass(frozen=True)
class FixedIrrigationDose:
    """Explicit integration control with a configurable nominal dose."""

    nominal_mm: float

    def __post_init__(self) -> None:
        from fairy.acps.ontology.farm.spec import finite

        finite(self.nominal_mm, "fixed irrigation depth", 1e-9, 50)

    @property
    def identity(self) -> str:
        return f"fixed_irrigation:{float(self.nominal_mm)!r}mm"

    def choose(self, evidence: IrrigationDoseInput) -> DoseChoice:
        return DoseChoice(min(self.nominal_mm, evidence.maximum_depth_mm))
