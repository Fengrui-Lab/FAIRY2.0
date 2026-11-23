"""Compare retrospective water-stress diagnosis on the original held-out windows."""

from __future__ import annotations

import argparse
from hashlib import sha256
from importlib.resources import files
from pathlib import Path
from typing import Sequence

import numpy as np
import torch
import yaml
from pcse.crop.evapotranspiration import SWEAF

from evaluate.oracle.stress_supervision import collect_probes, fit, probes, scores
from evaluate.scenarios.spec import ScenarioSpec
from fairy.acps.ontology.contracts import JsonObject
from fairy.agents.models.numeric.soil_stress import (
    DEFICIT_THRESHOLD,
    WINDOW_DAYS,
    SoilStressPredictor,
)
from fairy.events.artifacts import load_json, write_json
from fairy.events.provenance import source_identity

SEVERE_THRESHOLD = 0.20
FEATURES = (
    "start_root_vwc",
    "et0_mm",
    "wilting_vwc",
    "field_capacity_vwc",
    "irrigation_mm",
)


def soil_limits(spec: ScenarioSpec, region: tuple[int, int]) -> tuple[float, float]:
    """Resolve declared static soil parameters for a homogeneous evaluated zone."""
    assert spec.season is not None
    capacities = {
        next(
            (
                capacity
                for first, last, capacity, _, _ in spec.season.hydraulic_zones
                if first <= ridge <= last
            ),
            spec.farm.field_capacity_vwc,
        )
        for ridge in range(region[0], region[1] + 1)
    }
    if len(capacities) != 1:
        raise ValueError("Ablation expects one soil profile per evaluated zone")
    return spec.farm.wilting_point_vwc, capacities.pop()


def analytical(
    vwc: tuple[float, ...],
    et0_mm: float,
    wilting: float,
    capacity: float,
    cfet: float,
    depnr: float,
) -> float:
    """Reconstruct daily 1−RFWS with rounded public per-ridge VWC and static parameters."""
    critical = wilting + (1.0 - SWEAF(cfet * et0_mm / 10.0, depnr)) * (
        capacity - wilting
    )
    return float(
        np.mean(1.0 - np.clip((np.asarray(vwc) - wilting) / (critical - wilting), 0, 1))
    )


def diagnostics(labels: np.ndarray, predicted: np.ndarray) -> JsonObject:
    report = scores(labels, predicted)
    severe = labels >= SEVERE_THRESHOLD
    report.update(
        severe_positives=int(severe.sum()),
        severe_false_negatives=int((severe & (predicted < DEFICIT_THRESHOLD)).sum()),
    )
    return report


def run(
    harbin: Path, changchun: Path, original: Path, output: Path, *, workers: int
) -> None:
    archives = [load_json(path) for path in (harbin, changchun)]
    cases = probes(*archives)
    original_manifest = load_json(original / "manifest.json")
    original_data = original / "dataset.npz"
    original_hash = sha256(original_data.read_bytes()).hexdigest()
    if original_hash != original_manifest["dataset_sha256"]:
        raise ValueError("Original dataset checksum mismatch")
    output.mkdir(parents=True, exist_ok=False)
    by_case = collect_probes(cases, workers)
    x, labels, groups, ids, days, regions, old_x = [], [], [], [], [], [], []
    reference = []
    parameters = yaml.safe_load(
        files("fairy").joinpath("acps/backends/biology/wofost/soybean.yaml").read_text()
    )["CropParameters"]["Varieties"]
    for case, samples in zip(cases, by_case, strict=True):
        crop = parameters[case.spec.crop.wofost_variety]
        for row in samples:
            wilting, capacity = soil_limits(case.spec, row.region)
            x.append(
                [
                    (
                        sum(day.start_vwc) / len(day.start_vwc),
                        day.et0_mm,
                        wilting,
                        capacity,
                        day.irrigation_mm,
                    )
                    for day in row.aligned
                ]
            )
            last = row.aligned[-1]
            reference.append(
                analytical(
                    last.start_vwc,
                    last.et0_mm,
                    wilting,
                    capacity,
                    crop["CFET"][0],
                    crop["DEPNR"][0],
                )
            )
            labels.append(row.severity)
            groups.append(case.split)
            ids.append(case.identity)
            days.append(row.day)
            regions.append(row.region)
            old_x.append(row.features)

    inputs = np.asarray(x, dtype=np.float32)
    baseline_inputs = np.asarray(old_x, dtype=np.float32)
    y = np.asarray(labels, dtype=np.float32)
    split, case_ids = np.asarray(groups), np.asarray(ids)
    with np.load(original_data, allow_pickle=False) as old:
        for key, value in (
            ("x", baseline_inputs),
            ("y", y),
            ("split", split),
            ("case", case_ids),
            ("day", days),
            ("region", regions),
        ):
            if not np.array_equal(old[key], value):
                raise ValueError(f"Experiment changed original rows: {key}")

    predictions = {"analytical": np.asarray(reference)}
    fitting = {}
    seed = original_manifest["seed"]
    for name, length, width in (
        ("day_start", 1, 4),
        ("day_start_receipt", 1, 5),
        ("seven_days", WINDOW_DAYS, 4),
        ("seven_days_receipt", WINDOW_DAYS, 5),
    ):
        model, mean, scale, predicted, epoch = fit(
            inputs[:, :, :width], y, split, length, seed
        )
        predictions[name] = predicted
        weight = output / f"{name}.pt"
        torch.save(model.state_dict(), weight)
        fitting[name] = dict(
            selected_epoch=epoch,
            window=length,
            features=list(FEATURES[:width]),
            parameters=sum(p.numel() for p in model.parameters()),
            mean=mean.tolist(),
            scale=scale.tolist(),
            sha256=sha256(weight.read_bytes()).hexdigest(),
        )
    for name in ("sequence", "current_day"):
        old_model = SoilStressPredictor.load(original, name)
        with torch.inference_mode():
            t = torch.from_numpy(baseline_inputs[:, -old_model.length :])
            t = (t - t.new_tensor(old_model.mean)) / t.new_tensor(old_model.scale)
            predictions[f"original_{name}"] = old_model.model(t).numpy()

    test = split == "test"
    receipt = inputs[:, -1, -1] > 0
    if not np.any(test & receipt):
        raise ValueError(
            "No accepted irrigation receipts in the held-out diagnosis days"
        )
    report = {
        name: {
            "validation": diagnostics(
                y[split == "validation"], pred[split == "validation"]
            ),
            "test": diagnostics(y[test], pred[test]),
            "test_on_receipt_day": diagnostics(y[test & receipt], pred[test & receipt]),
            "by_test_case": {
                case: diagnostics(y[case_ids == case], pred[case_ids == case])
                for case in sorted(set(case_ids[test]))
            },
        }
        for name, pred in predictions.items()
    }
    data = output / "predictions.npz"
    np.savez_compressed(
        data,
        x=inputs,
        y=y,
        split=split,
        case=case_ids,
        day=days,
        region=regions,
        model_names=np.asarray(list(predictions)),
        predicted=np.column_stack(tuple(predictions.values())),
    )
    write_json(
        output / "manifest.json",
        dict(
            seed=seed,
            features=FEATURES,
            positive_threshold=DEFICIT_THRESHOLD,
            severe_threshold=SEVERE_THRESHOLD,
            original_dataset_sha256=original_hash,
            predictions_sha256=sha256(data.read_bytes()).hexdigest(),
            weather_archive_fingerprints=[
                archive["fingerprint"] for archive in archives
            ],
            source_fingerprint=source_identity()["fingerprint"],
            models=fitting,
            irrigation_days={
                part: int(((split == part) & receipt).sum())
                for part in ("train", "validation", "test")
            },
        ),
    )
    write_json(output / "metrics.json", report)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("harbin", "changchun", "original", "output"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--workers", type=int, choices=range(1, 9), default=4)
    args = parser.parse_args(argv)
    torch.set_num_threads(1)
    run(args.harbin, args.changchun, args.original, args.output, workers=args.workers)
    print(args.output / "metrics.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
