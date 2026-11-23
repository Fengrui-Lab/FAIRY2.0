"""Fit a compact water expert against a decision-day PCSE reference condition."""

from __future__ import annotations

import argparse
import json
from hashlib import sha256
from pathlib import Path
from typing import Sequence

import numpy as np
from pcse.crop.evapotranspiration import SWEAF

from fairy.acps.ontology.contracts import JsonObject
from fairy.agents.models.numeric.water_expert import REFERENCE_ET0_MM
from fairy.events.artifacts import write_json


def fit(
    records: Sequence[JsonObject], dataset_sha256: str
) -> tuple[JsonObject, JsonObject]:
    """Fit on original training years; score exact held-out partitions independently."""
    groups = np.asarray([row["split"] for row in records])
    years = np.asarray([row["year"] for row in records])
    if any(len(set(groups[years == year])) != 1 for year in set(years)):
        raise ValueError("A weather year appears in more than one partition")

    public = np.asarray([row["public"]["available_fraction"] for row in records])
    truth = np.asarray([row["reference_available_fraction"] for row in records])
    # Soybean_901 uses CFET=1 and DEPNR=5. The existing probes use homogeneous
    # ridge water states in each zone; current-water fractions are clipped at FC.
    critical_fraction = 1 - SWEAF(REFERENCE_ET0_MM / 10, 5.0)
    label = np.clip(1 - truth / critical_fraction, 0, 1)
    features = np.column_stack((np.ones(len(public)), public))
    linear = (groups == "train") & (label > 0) & (label < 1)
    if linear.sum() < 2:
        raise ValueError(
            "Insufficient training observations in the unsaturated response range"
        )
    offset, slope = np.linalg.lstsq(features[linear], label[linear], rcond=None)[0]
    predicted = np.clip(features @ np.array([offset, slope]), 0, 1)
    config = dict(
        schema=1,
        reference_et0_mm=REFERENCE_ET0_MM,
        intercept=float(offset),
        slope=float(slope),
        cultivar="Soybean_901",
        input_dataset_sha256=dataset_sha256,
    )
    report = {}
    for part in ("train", "validation", "test"):
        mask = groups == part
        y, p = label[mask], predicted[mask]
        severe = y >= 0.20
        report[part] = dict(
            windows=int(mask.sum()),
            positive=int((y >= 0.10).sum()),
            severe=int(severe.sum()),
            mae=float(np.abs(y - p).mean()),
            false_positive=int(((y < 0.10) & (p >= 0.10)).sum()),
            false_negative=int(((y >= 0.10) & (p < 0.10)).sum()),
            severe_missed=int((severe & (p < 0.10)).sum()),
            severe_underclassified=int((severe & (p < 0.20)).sum()),
        )
    return config, report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("records", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args(argv)
    config, report = fit(
        json.loads(args.records.read_text()),
        sha256(args.records.read_bytes()).hexdigest(),
    )
    args.output.mkdir(parents=True, exist_ok=False)
    write_json(args.output / "water_expert.json", config)
    write_json(args.output / "metrics.json", report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
