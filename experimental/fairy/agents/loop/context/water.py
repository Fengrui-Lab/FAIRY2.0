"""Decision-time root-water diagnostics from declared soil and public tool receipts."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from itertools import combinations
from fairy.agents.models.numeric.water_expert import load_water_expert, stress_class
from fairy.events.records import detached
from typing import TYPE_CHECKING, Literal, Mapping, Sequence

if TYPE_CHECKING:
    from evaluate.scenarios.spec import ScenarioSpec
    from fairy.acps.ontology.contracts import JsonObject
    from fairy.events.records import HistoryTurn
    from fairy.agents.loop.context.public import PublicContext

# Operational reference thresholds for modeled plant-available water.
SEVERE_FRACTION = 0.20
LOW_FRACTION = 0.50
WaterClass = Literal["severe_deficit", "low", "adequate"]


@dataclass(frozen=True)
class RootWater:
    """Fraction of the wilting-to-field-capacity range in the observed root zone."""

    mean_vwc: float
    available_fraction: float
    state: WaterClass
    severe_ridges: int


def soil_profile(spec: ScenarioSpec) -> dict[int, tuple[float, float]]:
    """Resolve wilting and field capacity per ridge from static scenario declarations."""
    if spec.season is None:
        return {}
    farm = spec.farm
    limits = dict.fromkeys(
        range(spec.season.ridge_count),
        (farm.wilting_point_vwc, farm.field_capacity_vwc),
    )
    for first, last, capacity, _, _ in spec.season.hydraulic_zones:
        if capacity <= farm.wilting_point_vwc:
            raise ValueError("Field capacity must exceed wilting VWC")
        for ridge in range(first, last + 1):
            limits[ridge] = (farm.wilting_point_vwc, capacity)
    return limits


def _available_fraction(vwc: float, limits: tuple[float, float]) -> float:
    wilting, capacity = limits
    return min(1.0, max(0.0, (vwc - wilting) / (capacity - wilting)))


def root_water(
    observations: Mapping[int, float], profile: Mapping[int, tuple[float, float]]
) -> RootWater:
    """Classify observed water availability equally by ridge area."""
    if not observations:
        raise ValueError("Root-water assessment requires a soil observation")
    availability = [
        _available_fraction(vwc, profile[ridge]) for ridge, vwc in observations.items()
    ]
    mean = sum(availability) / len(availability)
    state: WaterClass = "adequate"
    if mean <= SEVERE_FRACTION:
        state = "severe_deficit"
    elif mean <= LOW_FRACTION:
        state = "low"
    return RootWater(
        sum(observations.values()) / len(availability),
        mean,
        state,
        sum(value <= SEVERE_FRACTION for value in availability),
    )


def zone_irrigation(
    history: Sequence[HistoryTurn], now: datetime, ridges: set[int]
) -> JsonObject:
    """Summarize overlapping accepted applications using next-local-midnight settlement."""
    pending, settled = 0.0, 0.0
    pending_ridges: set[int] = set()
    settled_ridges: set[int] = set()
    yesterday = now.date() - timedelta(days=1)
    for turn in reversed(history):
        stamp = datetime.fromisoformat(turn["time"]).astimezone(now.tzinfo)
        if stamp.date() < yesterday:
            break
        req, res = turn["request"], turn["result"]
        if stamp > now or req["tool"] != "irrigate_range" or not res["ok"]:
            continue
        receipt = res["data"].get("receipt", {})
        if (
            receipt.get("settlement") != "next_local_midnight"
            or receipt["depth_mm"] <= 0
        ):
            continue
        span = req["arguments"]
        covered = ridges.intersection(range(span["start_ridge"], span["end_ridge"] + 1))
        zone_depth = receipt["depth_mm"] * len(covered) / len(ridges)
        if stamp.date() == now.date():
            pending += zone_depth
            pending_ridges.update(covered)
        elif stamp.date() == yesterday:
            settled += zone_depth
            settled_ridges.update(covered)

    return {
        "pending_zone_mean_mm": round(pending, 4),
        "pending_ridges": len(pending_ridges),
        "last_local_midnight_settled_zone_mean_mm": round(settled, 4),
        "last_local_midnight_settled_ridges": len(settled_ridges),
    }


def water_reading(
    observations: Mapping[int, float], profile: Mapping[int, tuple[float, float]]
) -> JsonObject:
    """Project the public estimate and any class boundary within sensor rounding."""
    result = root_water(observations, profile)
    # observe_soil_range publishes root VWC to three decimal places.
    lower, upper = (
        root_water(
            {ridge: value + offset for ridge, value in observations.items()}, profile
        )
        for offset in (-0.0005, 0.0005)
    )
    return {
        **asdict(result),
        "near_class_boundary": lower.state != upper.state,
    }


def _observed_rows(turn: HistoryTurn) -> list[JsonObject]:
    return turn["result"]["data"]["receipt"]["observations"]


def _yesterday_stress(
    history: Sequence[HistoryTurn],
    yesterday: str,
    ridges: set[int],
    profile: Mapping[int, tuple[float, float]],
) -> JsonObject | None:
    """Reconstruct the pinned Soybean_901 RFWS from public prior-day evidence."""
    soil: dict[int, float] | None = None
    living = False
    weather: JsonObject | None = None
    for turn in reversed(history):
        if turn["time"][:10] < yesterday:
            break
        name = turn["request"]["tool"]
        if not turn["result"]["ok"]:
            continue
        if name == "get_current_weather" and weather is None:
            data = turn["result"]["data"]
            if (
                data.get("source") == "historical_reanalysis"
                and data["weather"]["day"] == yesterday
            ):
                weather = data["weather"]
        elif (
            turn["time"][:10] == yesterday
            and name == "observe_soil_range"
            and soil is None
        ):
            observed = {
                row["ridge_id"]: row["root_vwc"] for row in _observed_rows(turn)
            }
            if ridges <= observed.keys():
                soil = observed
        elif turn["time"][:10] == yesterday and name == "inspect_crop" and not living:
            active = {
                row["ridge_id"]
                for row in _observed_rows(turn)
                if row["phase"] in ("vegetative", "reproductive")
            }
            living = ridges <= active
        if soil is not None and weather is not None and living:
            break
    if soil is None or weather is None or not living:
        return None

    # PCSE 6.0.13 SWEAF for the pinned cultivar's CFET=1 and DEPNR=5.
    et0_cm = weather["et0_mm"] / 10
    easily_available = min(0.95, max(0.10, 1 / (0.76 + 1.5 * et0_cm)))
    scores = []
    for ridge in sorted(ridges):
        wilting, capacity = profile[ridge]
        critical_offset = (1 - easily_available) * (capacity - wilting)
        scores.append(1 - min(1, max(0, (soil[ridge] - wilting) / critical_offset)))
    return {
        "day": yesterday,
        "severity": round(sum(scores) / len(scores), 4),
        "source": "estimated PCSE 1−RFWS from prior-day public soil and completed weather",
    }


def current_water_context(
    spec: ScenarioSpec, history: Sequence[HistoryTurn], now: datetime
) -> list[JsonObject]:
    """Project up to four observed ranges without reading Runtime or backend state."""
    profile = soil_profile(spec)
    recent = {}
    for turn in reversed(history):
        if turn["request"]["tool"] != "observe_soil_range" or not turn["result"]["ok"]:
            continue
        observed = {row["ridge_id"]: row["root_vwc"] for row in _observed_rows(turn)}
        ids = tuple(sorted(observed))
        if ids and set(ids) <= profile.keys() and ids not in recent:
            recent[ids] = (turn, observed)
        if len(recent) == 4:
            break

    yesterday = (now.date() - timedelta(days=1)).isoformat()
    result = []
    today = now.date().isoformat()
    first_soil: dict[tuple[int, ...], tuple[str, dict[int, float]]] = {}
    living: dict[int, str | None] = {}
    for turn in reversed(history):
        if turn["time"][:10] < today:
            break
        if not turn["result"]["ok"]:
            continue
        name = turn["request"]["tool"]
        if name == "inspect_crop":
            for row in _observed_rows(turn):
                living.setdefault(
                    row["ridge_id"],
                    (
                        turn["time"]
                        if row["phase"] in ("vegetative", "reproductive")
                        else None
                    ),
                )
        elif name == "observe_soil_range":
            soil = {row["ridge_id"]: row["root_vwc"] for row in _observed_rows(turn)}
            ids = tuple(sorted(soil))
            if ids in recent:
                first_soil[ids] = (turn["time"], soil)

    expert = load_water_expert() if first_soil else None
    for ids, (turn, observed) in recent.items():
        stamp = datetime.fromisoformat(turn["time"]).astimezone(now.tzinfo)
        current: JsonObject = {"observed_at": stamp.isoformat()}
        if stamp.date() == now.date() and stamp <= now:
            current.update(status="observed", **water_reading(observed, profile))
        else:
            current["status"] = "needs_fresh_soil_observation"
        ridges = set(ids)
        crop: JsonObject = {"status": "needs_fresh_soil_observation"}
        interval = None
        if ids in first_soil:
            crop["status"] = "needs_current_living_crop_observation"
            if all(living.get(ridge) for ridge in ids):
                assert expert is not None
                time, soil = first_soil[ids]
                # Predict and clip per ridge so wet ridges cannot erase severe
                # limitations on dry ridges before spatial aggregation.
                scores = [
                    sum(
                        expert.severity(_available_fraction(vwc + offset, profile[i]))
                        for i, vwc in soil.items()
                    )
                    / len(soil)
                    for offset in (-0.0005, 0, 0.0005)
                ]
                high, center, low = scores
                interval = (low, high)
                crop = dict(
                    status="estimated",
                    water=stress_class(center),
                    soil_observed_at=time,
                    near_class_boundary=stress_class(low) != stress_class(high),
                )
        result.append(
            {
                "ridge_range": [ids[0], ids[-1]],
                "current_root_water": current,
                "crop_water_expert": crop,
                "crop_water_score_interval": interval,
                "irrigation": zone_irrigation(history, now, ridges),
                "yesterday_stress": _yesterday_stress(
                    history, yesterday, ridges, profile
                ),
            }
        )
    return result


def relative_water_stress(rows: Sequence[JsonObject]) -> JsonObject:
    """Report pairwise order only when public measurement intervals separate."""
    higher, indistinguishable = [], []
    for left, right in combinations(rows, 2):
        a, b = left["ridge_range"], right["ridge_range"]
        first, second = left.get("crop_water_score_interval"), right.get(
            "crop_water_score_interval"
        )
        if first is None or second is None or max(a[0], b[0]) <= min(a[1], b[1]):
            continue
        if first[0] > second[1]:
            higher.append([a, b])
        elif second[0] > first[1]:
            higher.append([b, a])
        else:
            indistinguishable.append([a, b])
    return dict(higher_stress_first=higher, indistinguishable=indistinguishable)


def llm_expert_context(context: PublicContext, *, rank: bool = True) -> PublicContext:
    """Give the LLM categorical diagnoses while keeping public event data intact."""
    if "water_diagnostics" not in context:
        return context
    result = detached(context)
    details = result.pop("water_diagnostics")
    zones = []
    for row in details["zones"]:
        settled = row["irrigation"]
        yesterday = row["yesterday_stress"]
        first, last = row["ridge_range"]
        count = last - first + 1

        def coverage(covered: int) -> str:
            return "none" if covered == 0 else "all" if covered == count else "partial"

        zones.append(
            dict(
                ridge_range=row["ridge_range"],
                current_crop_water=row["crop_water_expert"],
                irrigation=dict(
                    pending=coverage(settled["pending_ridges"]),
                    settled_last_midnight=coverage(
                        settled["last_local_midnight_settled_ridges"]
                    ),
                ),
                yesterday_water_stress=(
                    dict(
                        day=yesterday["day"], water=stress_class(yesterday["severity"])
                    )
                    if yesterday is not None
                    else None
                ),
            )
        )
    result["water_diagnostics"] = dict(
        reference="crop water limitation under standard evaporative demand (ET0 4 mm/day)",
        nitrogen="unavailable: backend has no validated nitrogen limitation label",
        zones=zones,
    )
    if rank:
        ordering = relative_water_stress(details["zones"])
        if any(ordering.values()):
            result["water_diagnostics"]["relative_water_stress"] = ordering
    for turn in [*result["history"], *result["public_memory"].get("observations", ())]:
        if turn["request"]["tool"] != "observe_soil_range" or not turn["result"]["ok"]:
            continue
        receipt = turn["result"]["data"].get("receipt", {})
        if "observations" in receipt or "observations_grouped" in receipt:
            receipt.pop("observations", None)
            receipt.pop("observations_grouped", None)
            receipt["summary"] = (
                "Soil observation recorded; see the crop-water diagnostics."
            )
    return result
