"""Public sensor inputs and frozen inference for the WOFOST soil-stress probe."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from hashlib import sha256
from math import isfinite
from pathlib import Path
from typing import TYPE_CHECKING, Sequence

from fairy.acps.ontology.contracts import JsonObject
from fairy.events.artifacts import load_json

if TYPE_CHECKING:
    from .modeling import SoilWaterStressRegressor

FEATURES = (
    "root_vwc",
    "rain_mm",
    "et0_mm",
    "radiation_mj_m2",
    "mean_temperature_c",
    "reproductive",
)
WINDOW_DAYS = 7
DEFICIT_THRESHOLD = 0.10


@dataclass(frozen=True)
class StressObservation:
    """A zone reading taken after a dated weather day settled in the local clock."""

    day: date
    ridges: tuple[int, ...]
    features: tuple[float, ...]

    @classmethod
    def from_public(
        cls, soil: JsonObject, crop: JsonObject, current_weather: JsonObject
    ) -> StressObservation:
        """Read only the receipts of the public soil, crop and historical weather tools."""
        weather = current_weather["weather"]
        day = date.fromisoformat(weather["day"])
        observed = datetime.fromisoformat(soil["time"])
        if (
            observed != datetime.fromisoformat(crop["time"])
            or observed != datetime.fromisoformat(current_weather["time"])
            or day != observed.date() - timedelta(days=1)
        ):
            raise ValueError(
                "Stress readings must share a timestamp after the weather day settled"
            )
        rows, plants = soil["observations"], crop["observations"]
        ridges = tuple(row["ridge_id"] for row in rows)
        phases = {row["phase"] for row in plants}
        if (
            not ridges
            or len(set(ridges)) != len(ridges)
            or ridges != tuple(row["ridge_id"] for row in plants)
            or len(phases) != 1
            or not phases <= {"vegetative", "reproductive"}
        ):
            raise ValueError(
                "Stress readings require paired measurements for one living crop phase"
            )
        values = (
            sum(float(row["root_vwc"]) for row in rows) / len(rows),
            float(weather["rain_mm"]),
            float(weather["et0_mm"]),
            float(weather["radiation_mj_m2"]),
            (float(weather["tmin_c"]) + float(weather["tmax_c"])) / 2,
            float(phases == {"reproductive"}),
        )
        if not all(map(isfinite, values)) or not 0 <= values[0] <= 1:
            raise ValueError("Invalid public stress features")
        return cls(day, ridges, values)


def window_features(
    observations: Sequence[StressObservation], length: int = WINDOW_DAYS
) -> list[tuple[float, ...]]:
    """Reject missing days and cross-zone sequences before extracting model features."""
    if length <= 0 or len(observations) != length:
        raise ValueError(f"Expected {length} consecutive daily stress readings")
    first = observations[0]
    for offset, reading in enumerate(observations):
        if (
            reading.day != first.day + timedelta(days=offset)
            or reading.ridges != first.ridges
            or len(reading.features) != len(FEATURES)
            or not all(map(isfinite, reading.features))
        ):
            raise ValueError(
                "Stress input does not describe one consecutive zone history"
            )
    return [reading.features for reading in observations]


@dataclass(frozen=True)
class StressPrediction:
    """Estimated 1−RFWS for the last completed day and its deficit classification."""

    day: date
    severity: float
    water_deficit: bool


@dataclass(frozen=True)
class SoilStressPredictor:
    """CPU predictor loaded from an experiment's local checkpoint and manifest."""

    model: SoilWaterStressRegressor
    mean: tuple[float, ...]
    scale: tuple[float, ...]
    length: int

    @classmethod
    def load(cls, folder: str | Path, name: str = "sequence") -> SoilStressPredictor:
        """Verify the feature contract and checkpoint digest before loading weights."""
        import torch
        from .modeling import SoilWaterStressRegressor

        root = Path(folder)
        manifest = load_json(root / "manifest.json")
        expected_lengths = {"sequence": WINDOW_DAYS, "current_day": 1}
        if (
            name not in expected_lengths
            or manifest["schema"] != 1
            or manifest["features"] != list(FEATURES)
            or manifest["deficit_threshold"] != DEFICIT_THRESHOLD
        ):
            raise ValueError("Incompatible soil-stress checkpoint contract")
        config = manifest["models"][name]
        mean, scale = tuple(config["mean"]), tuple(config["scale"])
        if (
            config["window_days"] != expected_lengths[name]
            or len(mean) != len(FEATURES)
            or len(scale) != len(FEATURES)
            or not all(map(isfinite, mean))
            or not all(isfinite(x) and x > 0 for x in scale)
        ):
            raise ValueError("Invalid soil-stress feature normalization")
        path = root / f"{name}.pt"
        if sha256(path.read_bytes()).hexdigest() != config["sha256"]:
            raise ValueError("Soil-stress weights checksum mismatch")
        model = SoilWaterStressRegressor(len(FEATURES))
        model.load_state_dict(torch.load(path, map_location="cpu", weights_only=True))
        model.eval()
        return cls(model, mean, scale, config["window_days"])

    def predict(self, observations: Sequence[StressObservation]) -> StressPrediction:
        """Estimate past-day severity using only the supplied public window."""
        import torch

        rows = window_features(observations, self.length)
        with torch.inference_mode():
            values = torch.tensor(rows, dtype=torch.float32)
            values = (values - values.new_tensor(self.mean)) / values.new_tensor(
                self.scale
            )
            severity = float(self.model(values.unsqueeze(0)).item())
        return StressPrediction(
            observations[-1].day, severity, severity >= DEFICIT_THRESHOLD
        )
