"""CLI for the reproducible historical irrigation representation comparison."""

from __future__ import annotations

import argparse
from collections import deque
from concurrent.futures import ProcessPoolExecutor
from copy import copy
from dataclasses import dataclass
from functools import partial
from multiprocessing import get_context
from pathlib import Path
from typing import Callable, Iterable, Sequence, TypeVar

from numpy.typing import NDArray

import numpy as np

from evaluate.oracle.representation_env import (
    Arm,
    Case,
    QUOTA_MM,
    RepresentationEnv,
    cases,
    objective,
)
from fairy.acps.ontology.contracts import JsonObject
from fairy.agents.loop.context.water import LOW_FRACTION
from fairy.agents.models.numeric.irrigation import DOSES
from fairy.events.artifacts import artifact, load_json, replay, write_json
from fairy.events.records import fingerprint
from fairy.events.provenance import source_identity
from fairy.events.session import Runtime

CONTROLS = ("zero", "fixed10", "public_rule", "early60", "late60")
ARMS: tuple[Arm, ...] = ("A", "B", "C", "D")
SEEDS = (42, 43, 44)
MIN_TIMING_GAIN_KG = 35.0
DROUGHT_BALANCE_MM = -50.0

_Job = TypeVar("_Job")
_Result = TypeVar("_Result")


def control(env: RepresentationEnv, name: str) -> int:
    r = env.reading
    requested = {
        "zero": 0,
        "fixed10": 2,
        "early60": 3,
        "late60": 3 if r.reproductive else 0,
        "public_rule": 3 if np.mean(r.available) <= LOW_FRACTION else 0,
    }[name]
    return requested if env.action_masks()[requested] else 0


def finish(
    env: RepresentationEnv,
    choose: Callable[[RepresentationEnv, NDArray[np.float32]], int],
    *,
    observation: NDArray[np.float32],
    first: int | None = None,
    details: JsonObject | None = None,
) -> tuple[JsonObject, JsonObject]:
    """Run a full continuation and strictly replay its operation trace."""
    while True:
        action = first if first is not None else choose(env, observation)
        first = None
        observation, _, done, truncated, info = env.step(action)
        if truncated:
            raise ValueError("Unexpected truncated representation rollout")
        if done:
            break
    record = artifact(env.runtime, details)
    _, metrics = replay(record)
    applications = env.actions
    water = metrics["water_used_mm"]
    return {
        "case": env.case.key,
        "split": env.case.split,
        "climate_balance_mm": env.case.climate_balance_mm,
        "metrics": metrics,
        "objective": objective(metrics),
        "failure": info["failure"],
        "reproductive_water_fraction": (
            sum(row["dose_mm"] for row in applications if row["reproductive"]) / water
            if water
            else None
        ),
        "low_water_window_fraction": (
            sum(row["dose_mm"] for row in applications if row["low"]) / water
            if water
            else None
        ),
        "quota_exhaustion_date": applications[-1]["day"] if water == QUOTA_MM else None,
        "applications": applications,
        "artifact_fingerprint": fingerprint(record),
    }, record


def _valid(row: JsonObject) -> bool:
    return row["metrics"]["task_success"] and row["metrics"]["replay_verified"]


def _control_job(out: Path, job: tuple[Case, str]) -> JsonObject:
    case, name = job
    path = out / f"{case.key}-{name}.json"
    summary_path = path.with_suffix(".summary.json")
    if summary_path.exists():
        row = load_json(summary_path)
        record = load_json(path)
        if (
            fingerprint(record) != row["artifact_fingerprint"]
            or record["state"]["scenario_fingerprint"]
            != fingerprint(case.spec.to_dict())
            or row["source_fingerprint"] != source_identity()["fingerprint"]
        ):
            raise ValueError(f"Incorrect or stale cached control: {path}")
        return row
    env = RepresentationEnv([case], "A")
    obs, _ = env.reset()
    row, record = finish(
        env,
        lambda env, _: control(env, name),
        observation=obs,
        details={"control": name},
    )
    row.update(
        control=name, source_fingerprint=env.runtime.source_identity["fingerprint"]
    )
    write_json(path, record, overwrite=path.exists())
    write_json(summary_path, row)
    print(
        case.key,
        name,
        round(row["objective"], 4),
        row["metrics"]["task_success"],
        flush=True,
    )
    return row


def _parallel(
    fn: Callable[[_Job], _Result], jobs: Iterable[_Job], workers: int
) -> list[_Result]:
    if workers == 1:
        return list(map(fn, jobs))
    with ProcessPoolExecutor(
        max_workers=workers, mp_context=get_context("spawn")
    ) as pool:
        return list(pool.map(fn, jobs))


def gate(seasons: Sequence[Case], out: Path, workers: int) -> JsonObject:
    """Use development years only to verify practical, strictly replayed timing leverage."""
    development = [case for case in seasons if case.split != "test"]
    rows = _parallel(
        partial(_control_job, out / "controls"),
        [
            (case, name)
            for case in sorted(development, key=lambda c: c.climate_balance_mm)
            for name in CONTROLS
        ],
        workers,
    )
    timing = []
    for case in development:
        controls = {r["control"]: r for r in rows if r["case"] == case.key}
        early, late = (controls[key] for key in ("early60", "late60"))
        matched = (
            _valid(early)
            and _valid(late)
            and early["metrics"]["water_used_mm"]
            == late["metrics"]["water_used_mm"]
            == QUOTA_MM
            and early["applications"][-1]["day"] < late["applications"][0]["day"]
        )
        delta = (
            late["metrics"]["stored_dry_grain_kg"]
            - early["metrics"]["stored_dry_grain_kg"]
        )
        timing.append(
            dict(
                case=case.key,
                split=case.split,
                climate_balance_mm=case.climate_balance_mm,
                matched=matched,
                late_minus_early_kg=delta,
                pass_timing=matched
                and abs(delta) >= MIN_TIMING_GAIN_KG
                and case.climate_balance_mm <= DROUGHT_BALANCE_MM,
            )
        )
    validation = [r for r in rows if r["split"] == "validation"]
    competitive = [
        name
        for name in CONTROLS
        if validation and all(_valid(r) for r in validation if r["control"] == name)
    ]
    strongest = max(
        competitive,
        key=lambda name: np.mean(
            [r["objective"] for r in validation if r["control"] == name]
        ),
        default=None,
    )
    result = {
        "timing_passed": strongest is not None
        and any(r["pass_timing"] and r["split"] == "train" for r in timing),
        "climate_balance_max_for_drought_mm": DROUGHT_BALANCE_MM,
        "timing_floor_kg": MIN_TIMING_GAIN_KG,
        "strongest_validation_control": strongest,
        "timing": timing,
        "rows": rows,
        "source_fingerprint": source_identity()["fingerprint"],
        "seasons": _season_identity(seasons),
    }
    write_json(out / "gate.json", result, overwrite=True)
    return result


def _season_identity(seasons: Sequence[Case]) -> list[JsonObject]:
    return [
        dict(
            key=case.key,
            split=case.split,
            climate_balance_mm=case.climate_balance_mm,
            scenario_fingerprint=fingerprint(case.spec.to_dict()),
        )
        for case in seasons
    ]


@dataclass(frozen=True)
class Branch:
    case: Case
    day_offset: int
    dose_index: int


def _branch_job(out: Path, branch: Branch) -> JsonObject:
    path = out / f"{branch.case.key}-{branch.day_offset}-{branch.dose_index}.json"
    summary_path = path.with_suffix(".summary.json")
    if summary_path.exists():
        row = load_json(summary_path)
        record = load_json(path)
        if (
            fingerprint(record) != row["artifact_fingerprint"]
            or record["state"]["scenario_fingerprint"]
            != fingerprint(branch.case.spec.to_dict())
            or row["source_fingerprint"] != source_identity()["fingerprint"]
        ):
            raise ValueError(f"Corrupt checkpoint continuation: {path}")
        return row
    env = RepresentationEnv([branch.case], "A")
    obs, _ = env.reset()
    for _ in range(branch.day_offset):
        obs, _, done, _, _ = env.step(0)
        if done:
            raise ValueError("Checkpoint occurs after terminal event")
    if not env.action_masks()[branch.dose_index]:
        return dict(
            case=branch.case.key,
            offset=branch.day_offset,
            legal=False,
            dose_mm=DOSES[branch.dose_index],
        )
    # Use the exact common prefix, reconstructing the simulator in a fresh instance.
    restored = copy(env)
    restored.runtime = Runtime.from_state(env.runtime.export_state())
    restored.history = deque(env.history, maxlen=env.history.maxlen)
    restored.actions = list(env.actions)
    row, record = finish(
        restored,
        lambda e, _: control(e, "public_rule"),
        observation=obs,
        first=branch.dose_index,
        details={
            "checkpoint_offset": branch.day_offset,
            "dose_mm": DOSES[branch.dose_index],
        },
    )
    row.update(
        offset=branch.day_offset,
        legal=True,
        dose_mm=DOSES[branch.dose_index],
        legacy_vector=obs.tolist(),
        available_median=float(np.median(env.reading.available)),
        source_fingerprint=restored.runtime.source_identity["fingerprint"],
    )
    write_json(path, record, overwrite=path.exists())
    write_json(summary_path, row)
    return row


def checkpoints(seasons: Sequence[Case], out: Path, workers: int) -> JsonObject:
    from itertools import combinations

    dry = sorted(
        (s for s in seasons if s.split == "train"), key=lambda c: c.climate_balance_mm
    )[:2]
    rows = _parallel(
        partial(_branch_job, out / "branches"),
        [
            Branch(c, offset, i)
            for c in dry
            for offset in (45, 70)
            for i in range(len(DOSES))
        ],
        workers,
    )
    best = []
    for case in dry:
        for offset in (45, 70):
            options = [
                r
                for r in rows
                if r["case"] == case.key
                and r["offset"] == offset
                and r["legal"]
                and _valid(r)
            ]
            if options:
                best.append(max(options, key=lambda r: (r["objective"], -r["dose_mm"])))
    pairs = [
        dict(
            left=f'{a["case"]}@{a["offset"]}',
            right=f'{b["case"]}@{b["offset"]}',
            legacy_feature_distance=float(
                np.linalg.norm(
                    np.asarray(a["legacy_vector"]) - np.asarray(b["legacy_vector"])
                )
            ),
            best_doses_mm=[a["dose_mm"], b["dose_mm"]],
        )
        for a, b in combinations(best, 2)
    ]
    result = dict(branches=rows, best=best, comparisons=pairs)
    write_json(out / "checkpoints.json", result, overwrite=True)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("gate", "checkpoints", "smoke", "train"))
    parser.add_argument("--harbin", type=Path, required=True)
    parser.add_argument("--changchun", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args(argv)
    if args.workers < 1:
        parser.error("--workers must be positive")
    seasons = cases(load_json(args.harbin), load_json(args.changchun))
    if args.phase == "gate":
        result = gate(seasons, args.out, args.workers)
        print("Timing gate:", result["timing_passed"], flush=True)
    elif args.phase == "checkpoints":
        checkpoints(seasons, args.out, args.workers)
    else:
        from evaluate.oracle.representation_training import train_study

        train_study(seasons, args.out, args.workers, smoke=args.phase == "smoke")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
