"""Evaluate decision-time soil-water classification on historical WOFOST before integration."""

from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace
from datetime import timedelta
from multiprocessing import get_context
from pathlib import Path
from typing import Sequence

import numpy as np

from evaluate.oracle.stress_supervision import REGIONS, Probe, probes
from evaluate.experiments.historical.operations import (
    TARGET,
    call,
    next_day,
    plant_prefix,
    require,
    window,
)
from fairy.acps.backends.biology.wofost.season import WofostSeasonBackend
from fairy.acps.ontology.contracts import JsonObject
from fairy.agents.loop.context.history import public_history
from fairy.agents.loop.context.water import (
    LOW_FRACTION,
    SEVERE_FRACTION,
    soil_profile,
    zone_irrigation,
    water_reading,
)
from fairy.events.artifacts import load_json, write_json
from fairy.events.provenance import source_identity


def evaluate_probe(probe: Probe) -> list[JsonObject]:
    """Pair observations at the decision timestamp with unrounded PCSE soil truth."""
    runtime = plant_prefix(probe.spec, backend="wofost")
    profile = soil_profile(probe.spec)
    records = []
    doses = 0
    pending = False
    while runtime.now.date() < runtime.deadline.date():
        settled, pending = pending, False
        mature = True
        for zone in REGIONS:
            crop = require(call(runtime, "inspect_crop", **window(zone)))["receipt"]
            rows = crop["observations"]
            mature &= all(row["harvestable"] for row in rows)
            if not all(row["phase"] in ("vegetative", "reproductive") for row in rows):
                continue

            def observe(kind: str, *, capture_irrigation: bool = False) -> JsonObject:
                receipt = require(call(runtime, "observe_soil_range", **window(zone)))[
                    "receipt"
                ]
                public = {
                    row["ridge_id"]: row["root_vwc"] for row in receipt["observations"]
                }
                reading = water_reading(public, profile)
                engine = runtime.backend
                assert isinstance(engine, WofostSeasonBackend)
                truth = np.mean(
                    [
                        np.clip(
                            (
                                float(engine.engines[i].model.get_variable("SM"))
                                - profile[i][0]
                            )
                            / (profile[i][1] - profile[i][0]),
                            0.0,
                            1.0,
                        )
                        for i in public
                    ]
                )
                reference = (
                    "severe_deficit"
                    if truth <= SEVERE_FRACTION
                    else "low" if truth <= LOW_FRACTION else "adequate"
                )
                record = dict(
                    observed_at=receipt["time"],
                    zone=zone,
                    kind=kind,
                    public=reading,
                    reference_available_fraction=float(truth),
                    reference_state=reference,
                )
                if capture_irrigation:
                    record["irrigation"] = zone_irrigation(
                        public_history(runtime), runtime.now, set(public)
                    )
                records.append(record)
                return record

            fresh_settlement = settled and zone == TARGET
            record = observe(
                "just_settled" if fresh_settlement else "ordinary",
                capture_irrigation=settled,
            )
            if fresh_settlement:
                if (
                    record["irrigation"]["last_local_midnight_settled_zone_mean_mm"]
                    != 10
                ):
                    raise ValueError(
                        "Expected the accepted dose in the new-day settlement"
                    )
            elif settled and any(record["irrigation"].values()):
                raise ValueError(
                    "Irrigation was incorrectly attributed to another zone"
                )
            if (
                zone == TARGET
                and probe.irrigated
                and doses < 2
                and not settled
                and rows[0]["phase"] == "reproductive"
                and record["public"]["available_fraction"]
                <= (0.42 if doses == 0 else SEVERE_FRACTION)
            ):
                require(call(runtime, "irrigate_range", **window(zone), depth_mm=10.0))
                doses += 1
                pending = True
                check = observe("pending", capture_irrigation=True)
                if (
                    check["public"] != record["public"]
                    or check["observed_at"] != record["observed_at"]
                    or check["irrigation"]["pending_zone_mean_mm"] != 10
                    or check["irrigation"]["last_local_midnight_settled_zone_mean_mm"]
                    != 0
                ):
                    raise ValueError(
                        "Pending irrigation must not alter current water classification"
                    )

        if mature:
            return records
        next_day(runtime)
    raise ValueError(f"Probe did not mature before deadline: {probe.identity}")


def summarize(records: Sequence[JsonObject]) -> JsonObject:
    counts = Counter(
        (row["reference_state"], row["public"]["state"]) for row in records
    )
    return dict(
        windows=len(records),
        classes={
            key: sum(n for (label, _), n in counts.items() if label == key)
            for key in ("adequate", "low", "severe_deficit")
        },
        accuracy=sum(n for (label, pred), n in counts.items() if label == pred)
        / len(records),
        severe_false_positives=sum(
            n
            for (label, pred), n in counts.items()
            if label != "severe_deficit" and pred == "severe_deficit"
        ),
        severe_false_negatives=sum(
            n
            for (label, pred), n in counts.items()
            if label == "severe_deficit" and pred != "severe_deficit"
        ),
        confusion={f"{label}:{pred}": n for (label, pred), n in sorted(counts.items())},
        boundary_decisions=sum(row["public"]["near_class_boundary"] for row in records),
        mismatches_outside_boundary=sum(
            row["reference_state"] != row["public"]["state"]
            and not row["public"]["near_class_boundary"]
            for row in records
        ),
        available_fraction_mae=float(
            np.mean(
                [
                    abs(
                        row["reference_available_fraction"]
                        - row["public"]["available_fraction"]
                    )
                    for row in records
                ]
            )
        ),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("harbin", "changchun", "output"):
        parser.add_argument(f"--{name}", required=True, type=Path)
    parser.add_argument("--workers", type=int, choices=range(1, 9), default=4)
    args = parser.parse_args(argv)
    archives = [load_json(path) for path in (args.harbin, args.changchun)]
    cases = probes(*archives)
    irrigated_ids = {case.spec.scenario_id for case in cases if case.irrigated}
    cases += [
        replace(case, irrigated=True)
        for case in cases
        if case.split == "test" and case.spec.scenario_id not in irrigated_ids
    ]
    args.output.mkdir(parents=True, exist_ok=False)
    records: list[JsonObject] = []
    with ProcessPoolExecutor(
        max_workers=args.workers, mp_context=get_context("spawn")
    ) as pool:
        for case, rows in zip(cases, pool.map(evaluate_probe, cases), strict=True):
            print(f"{case.identity}: {len(rows)} observations", flush=True)
            records.extend(
                dict(row, case=case.identity, split=case.split, year=case.year)
                for row in rows
            )

    heldout = [row for row in records if row["split"] == "test"]
    summary = summarize(heldout)
    transitions = {}
    for kind in ("pending", "just_settled"):
        subset = [row for row in heldout if row["kind"] == kind]
        if not subset:
            raise ValueError(f"Missing held-out {kind} decisions")
        transitions[kind] = summarize(subset)
    if not summary["classes"]["severe_deficit"]:
        raise ValueError("Missing held-out severe deficits")
    write_json(args.output / "records.json", records)
    write_json(
        args.output / "metrics.json",
        dict(
            source=source_identity()["fingerprint"],
            thresholds=dict(severe=SEVERE_FRACTION, low=LOW_FRACTION),
            archives=[archive["fingerprint"] for archive in archives],
            partitions={
                part: summarize([row for row in records if row["split"] == part])
                for part in ("train", "validation", "test")
            },
            transitions=transitions,
            test_dates_with_irrigation=sorted(
                {row["observed_at"][:10] for row in heldout if row["kind"] == "pending"}
            ),
        ),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
