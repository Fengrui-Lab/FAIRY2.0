"""Historical full-field dose experiment over the public FAIRY runtime."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import date, timedelta
import json
from importlib.resources import files
from typing import Literal, Sequence

import gymnasium as gym
from gymnasium import spaces
import numpy as np
from numpy.typing import NDArray

from evaluate.metrics import TaskMetrics, evaluate
from evaluate.experiments.historical.operations import (
    FULL,
    call,
    close_harvest,
    next_day,
    plant_prefix,
    require,
)
from evaluate.scenarios.historical.compiler import (
    compile_season,
    make_regional_document,
    make_season_document,
)
from evaluate.scenarios.historical.weather import rank_seasons
from evaluate.scenarios.season import SeasonSpec
from evaluate.scenarios.spec import ScenarioSpec
from evaluate.oracle.stress_supervision import HARBIN_SPLIT, Split
from fairy.acps.ontology.contracts import JsonObject
from fairy.acps.ontology.farm.spec import FarmSpec
from fairy.agents.loop.context.water import LOW_FRACTION, SEVERE_FRACTION, soil_profile
from fairy.agents.models.numeric.irrigation import DOSES, PHASES
from fairy.agents.models.numeric.water_expert import load_water_expert
from fairy.agents.models.numeric.irrigation_window import (
    HISTORY_DAYS,
    HISTORY_FIELDS,
    SNAPSHOT_FIELDS,
)
from fairy.events.session import Runtime

Arm = Literal["A", "B", "C", "D"]
QUOTA_MM = 60
MASS_SCALE_KG = 3500
WATER_COST = 0.2
RIDGES = tuple(range(64))
SHAPES = {"A": 10, "B": len(SNAPSHOT_FIELDS)}
SHAPES.update(
    dict.fromkeys(("C", "D"), SHAPES["B"] + HISTORY_DAYS * len(HISTORY_FIELDS))
)


@dataclass(frozen=True)
class Case:
    region: str
    year: int
    split: Split
    climate_balance_mm: float
    spec: ScenarioSpec

    @property
    def key(self) -> str:
        return f"{self.region}-{self.year}"


def cases(harbin: JsonObject, changchun: JsonObject) -> list[Case]:
    """Keep the supervision holdouts and add two dry unassigned training seasons."""
    balance = rank_seasons(harbin)["climatic_balance_mm"]
    assigned = {y for years in HARBIN_SPLIT.values() for y in years}
    extra = sorted(
        (int(y) for y in balance if int(y) not in assigned),
        key=lambda y: (balance[str(y)], y),
    )[:2]
    result = []
    for split, years in HARBIN_SPLIT.items():
        for year in (*years, *(extra if split == "train" else ())):
            source = make_season_document(harbin, year, "drought")
            end = date.fromisoformat(source["task"]["deadline"][:10])
            start = date.fromisoformat(source["initial_state"]["time"][:10])
            doc = make_regional_document(
                harbin,
                region="harbin",
                start=start,
                end=end,
                kind="drought",
                farm=FarmSpec(**source["space"]["farm"]),
                season=SeasonSpec.from_dict(source["initial_state"]["season"]),
                quota_mm=QUOTA_MM,
                exposure=(date(year, 7, 1), date(year, 8, 15)),
            )
            result.append(
                Case(
                    "harbin",
                    year,
                    split,
                    balance[str(year)],
                    compile_season(doc, harbin, backend="wofost"),
                )
            )

    profile_path = files("evaluate").joinpath(
        "scenarios/lib/l3/configs/changchun_hn58_2016_profile.json"
    )
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    if (
        changchun["timezone"] != profile["timezone"]
        or changchun.get("requested_point", changchun["point"])
        != profile["requested_point"]
    ):
        raise ValueError("Changchun archive does not match the regional profile")
    start, end = map(date.fromisoformat, (profile["start_date"], profile["end_date"]))
    first, last = map(date.fromisoformat, profile["exposure"])
    doc = make_regional_document(
        changchun,
        region=profile["region"],
        start=start,
        end=end,
        kind=profile["role"],
        farm=FarmSpec(**profile["farm"]),
        season=SeasonSpec.from_dict(profile["season"]),
        quota_mm=QUOTA_MM,
        exposure=(first, last),
    )
    climate = sum(
        row["rain_mm"] - row["et0_mm"]
        for day, row in changchun["daily"].items()
        if first.isoformat() <= day <= last.isoformat()
    )
    result.append(
        Case(
            "changchun",
            start.year,
            "test",
            climate,
            compile_season(doc, changchun, backend="wofost"),
        )
    )
    return result


@dataclass(frozen=True)
class Reading:
    """A fresh, full-field public receipt at the daily decision boundary."""

    day: date
    phase: tuple[str, ...]
    moisture: NDArray[np.float64]
    available: NDArray[np.float64]
    severity: float
    stress_fraction: float
    remaining_mm: float
    pending_mm: float
    rain_mm: float
    et0_mm: float
    weather_present: bool
    harvestable: bool

    @property
    def active(self) -> bool:
        return all(phase in ("vegetative", "reproductive") for phase in self.phase)

    @property
    def reproductive(self) -> bool:
        return all(phase == "reproductive" for phase in self.phase)


def read(runtime: Runtime, limits: NDArray[np.float64]) -> Reading:
    """Read tool receipts and static soil limits; do not access backend internals."""
    crop = require(call(runtime, "inspect_crop", **FULL))["receipt"]["observations"]
    soil = require(call(runtime, "observe_soil_range", **FULL))["receipt"][
        "observations"
    ]
    weather = call(runtime, "get_current_weather")
    if not weather.ok and weather.error != "WEATHER_UNAVAILABLE":
        require(weather)
    day = runtime.now.date()
    report = require(weather)["weather"] if weather.ok else None
    if report is not None and report["day"] != (day - timedelta(days=1)).isoformat():
        raise ValueError("Historical weather is not the last completed local day")
    by_ridge = {row["ridge_id"]: row for row in crop}
    soil_by_ridge = {row["ridge_id"]: row["root_vwc"] for row in soil}
    phase = tuple(by_ridge[ridge]["phase"] for ridge in RIDGES)
    moisture = np.array([soil_by_ridge[ridge] for ridge in RIDGES])
    available = np.clip((moisture - limits[:, 0]) / (limits[:, 1] - limits[:, 0]), 0, 1)
    active = all(value in ("vegetative", "reproductive") for value in phase)
    expert = load_water_expert()
    severity = (
        float(np.mean([expert.severity(value) for value in available]))
        if active
        else 0.0
    )
    status = runtime.public_status()
    return Reading(
        day,
        phase,
        moisture,
        available,
        severity,
        float(np.mean(available <= SEVERE_FRACTION)),
        status["irrigation_remaining_mm"],
        status["pending_irrigation_mm"],
        report["rain_mm"] if report else 0.0,
        report["et0_mm"] if report else 0.0,
        report is not None,
        all(row["harvestable"] for row in crop),
    )


def legal(reading: Reading) -> NDArray[np.bool_]:
    """One whole-field dose per local day, available during active growth."""
    allowed = reading.active and reading.pending_mm <= 1e-9
    return np.array(
        [
            dose == 0 or (allowed and dose <= reading.remaining_mm + 1e-9)
            for dose in DOSES
        ]
    )


def objective(metrics: TaskMetrics) -> float:
    """Legacy mass and water objective reported separately from task validity."""
    return (
        metrics["stored_dry_grain_kg"] / MASS_SCALE_KG
        - WATER_COST * metrics["water_used_mm"] / QUOTA_MM
    )


class RepresentationEnv(gym.Env):
    """One decision at 07:00, with shared real operations and a fixed harvest driver."""

    metadata = {}

    def __init__(self, seasons: Sequence[Case], arm: Arm, offset: int = 0) -> None:
        self.seasons = tuple(seasons)
        if not self.seasons:
            raise ValueError("At least one historical season is required")
        self.arm = arm
        self.offset = offset
        self.action_space = spaces.Discrete(len(DOSES))
        self.observation_space = spaces.Box(
            0, np.inf, shape=(SHAPES[arm],), dtype=np.float32
        )
        self.history: deque[tuple[float, ...]] = deque(maxlen=HISTORY_DAYS)
        self.actions: list[JsonObject] = []
        self.episode = 0
        self.runtime: Runtime
        self.reading: Reading
        self.limits: NDArray[np.float64]
        self.sowed: date

    def reset(
        self, *, seed: int | None = None, options: JsonObject | None = None
    ) -> tuple[NDArray[np.float32], JsonObject]:
        super().reset(seed=seed)
        if seed is not None:
            self.episode = 0
        index = (self.offset + self.episode) % len(self.seasons)
        self.episode += 1
        self.case = self.seasons[index]
        self.runtime = plant_prefix(self.case.spec, backend="wofost")
        self.sowed = self.runtime.now.date()
        if (
            self.runtime.now.hour,
            self.runtime.now.minute,
            self.runtime.now.second,
        ) != (7, 0, 0):
            next_day(self.runtime)
        profiles = soil_profile(self.case.spec)
        self.limits = np.array([profiles[ridge] for ridge in RIDGES])
        self.history.clear()
        self.actions.clear()
        self.reading = read(self.runtime, self.limits)
        return self.vector(), {"case": self.case.key}

    def vector(self) -> NDArray[np.float32]:
        r = self.reading
        if self.arm == "A":
            return np.array(
                (
                    np.mean([PHASES.index(value) / 5 for value in r.phase]),
                    np.mean(r.moisture),
                    np.min(r.moisture),
                    np.max(r.moisture),
                    0.0,
                    r.et0_mm / 10,
                    r.remaining_mm / QUOTA_MM,
                    r.pending_mm / 50,
                    0.0,
                    float(r.weather_present),
                ),
                dtype=np.float32,
            )
        data = [
            *(r.phase.count(phase) / len(RIDGES) for phase in PHASES),
            (r.day - self.sowed).days / 180,
            *np.quantile(r.available, (0.1, 0.5, 0.9)),
            np.mean(r.available <= LOW_FRACTION),
            r.stress_fraction,
            r.severity,
            float(r.active),
            r.rain_mm / 20,
            r.et0_mm / 10,
            float(r.weather_present),
            r.remaining_mm / QUOTA_MM,
            r.pending_mm / QUOTA_MM,
        ]
        if self.arm in ("C", "D"):
            size = len(HISTORY_FIELDS)
            data.extend([0.0] * ((HISTORY_DAYS - len(self.history)) * size))
            data.extend(value for row in self.history for value in row)
        return np.asarray(data, dtype=np.float32)

    def action_masks(self) -> NDArray[np.bool_]:
        return legal(self.reading)

    def step(
        self, action: int
    ) -> tuple[NDArray[np.float32], float, bool, bool, JsonObject]:
        if self.runtime.done:
            raise ValueError("Reset a completed episode before stepping")
        if (
            not isinstance(action, (int, np.integer))
            or not 0 <= action < len(DOSES)
            or not self.action_masks()[action]
        ):
            raise ValueError("Illegal irrigation dose")
        current = self.reading
        dose = DOSES[action]
        if dose:
            receipt = require(
                call(self.runtime, "irrigate_range", **FULL, depth_mm=dose)
            )["receipt"]
            if (
                receipt["settlement"] != "next_local_midnight"
                or receipt["depth_mm"] != dose
            ):
                raise ValueError("Unexpected full-field irrigation receipt")
            self.actions.append(
                {
                    "day": current.day.isoformat(),
                    "dose_mm": dose,
                    "reproductive": current.reproductive,
                    "low": bool(np.mean(current.available) <= LOW_FRACTION),
                }
            )
        reward = -WATER_COST * dose / QUOTA_MM
        next_day(self.runtime)
        self.reading = next_reading = read(self.runtime, self.limits)
        if (
            next_reading.day != current.day + timedelta(days=1)
            or next_reading.pending_mm > 1e-9
        ):
            raise ValueError(
                "A decision step must settle once at the following local midnight"
            )
        self.history.append(
            (
                float(np.mean(current.moisture)),
                current.stress_fraction,
                next_reading.rain_mm / 20,
                next_reading.et0_mm / 10,
                dose / QUOTA_MM,
                1.0,
                float(next_reading.weather_present),
            )
        )
        if next_reading.harvestable or self.runtime.now >= self.runtime.deadline:
            failure = None
            try:
                close_harvest(self.runtime)
            except ValueError as exc:
                failure = str(exc)
                self.runtime.finish("representation_delivery_failure")
            metrics = evaluate(self.runtime)
            reward += (
                metrics["stored_dry_grain_kg"] / MASS_SCALE_KG
                if metrics["task_success"]
                else -1.0
            )
            return (
                self.vector(),
                reward,
                True,
                False,
                {"metrics": metrics, "failure": failure},
            )
        return self.vector(), reward, False, False, {}
