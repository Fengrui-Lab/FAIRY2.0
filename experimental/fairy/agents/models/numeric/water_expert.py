"""A two-parameter crop-water expert fitted under standard evaporative demand."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files
import json
from math import isfinite

REFERENCE_ET0_MM = 4.0
LIMITED = 0.10
SEVERE = 0.20


@dataclass(frozen=True)
class WaterExpert:
    """Map a normalized root-water observation to a reference-demand stress score."""

    intercept: float
    slope: float

    def severity(self, available_fraction: float) -> float:
        return min(1.0, max(0.0, self.intercept + self.slope * available_fraction))


@lru_cache(maxsize=1)
def load_water_expert() -> WaterExpert:
    """Load the fitted soybean expert without importing the training dependencies."""
    config = json.loads(files(__package__).joinpath("water_expert.json").read_text())
    model = WaterExpert(config["intercept"], config["slope"])
    if (
        config["schema"] != 1
        or config["cultivar"] != "Soybean_901"
        or config["reference_et0_mm"] != REFERENCE_ET0_MM
        or not all(isfinite(value) for value in (model.intercept, model.slope))
        or model.slope >= 0
    ):
        raise ValueError("Invalid current crop-water expert")
    return model


def stress_class(severity: float) -> str:
    if severity >= SEVERE:
        return "severe_water_limitation"
    if severity >= LIMITED:
        return "water_limited"
    return "no_water_limitation_detected"
