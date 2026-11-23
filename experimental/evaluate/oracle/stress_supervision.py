"""Generate and evaluate a small supervised model from historical WOFOST trajectories."""

from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from datetime import date, timedelta
from hashlib import sha256
from importlib.metadata import version
import json
from importlib.resources import files
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Sequence

import numpy as np
import torch

from evaluate.experiments.historical.operations import (
    TARGET,
    call,
    next_day,
    plant_prefix,
    require,
    window,
)
from evaluate.scenarios.historical.compiler import (
    compile_season,
    make_regional_document,
    make_season_document,
)
from evaluate.scenarios.season import SeasonSpec
from fairy.acps.backends.biology.wofost.season import WofostSeasonBackend
from fairy.acps.ontology.farm.spec import FarmSpec
from fairy.agents.models.numeric.modeling import SoilWaterStressRegressor
from fairy.agents.models.numeric.soil_stress import (
    DEFICIT_THRESHOLD,
    FEATURES,
    WINDOW_DAYS,
    SoilStressPredictor,
    StressObservation,
    window_features,
)
from fairy.events.artifacts import load_json, write_json
from fairy.events.provenance import source_identity
from fairy.events.records import fingerprint

if TYPE_CHECKING:
    from evaluate.scenarios.spec import ScenarioSpec
    from fairy.acps.ontology.contracts import JsonObject

Split = Literal["train", "validation", "test"]
HARBIN_SPLIT: dict[Split, tuple[int, ...]] = {
    "train": (1993, 1995, 1998, 2002, 2008, 2018),
    "validation": (1994, 2011),
    "test": (2001, 2007, 2016),
}
REGIONS = ((0, 21), TARGET)


@dataclass(frozen=True)
class Probe:
    """A complete scenario and its predefined year group and management policy."""

    spec: ScenarioSpec
    year: int
    split: Split
    irrigated: bool

    @property
    def identity(self) -> str:
        return f"{self.spec.scenario_id}:{'public_irrigation' if self.irrigated else 'zero'}"


@dataclass(frozen=True)
class AlignedDay:
    """Observations and accepted water for a single PCSE rate day."""

    start_vwc: tuple[float, ...]
    et0_mm: float
    irrigation_mm: float


@dataclass(frozen=True)
class Sample:
    """One public seven-day window paired with a trusted day-end PCSE label."""

    day: str
    region: tuple[int, int]
    features: list[tuple[float, ...]]
    severity: float
    aligned: tuple[AlignedDay, ...]


def probes(harbin: JsonObject, changchun: JsonObject) -> list[Probe]:
    """Declare a year-level split before any rollouts or fitting."""
    cases = []
    for split, years in HARBIN_SPLIT.items():
        for year in years:
            spec = compile_season(
                make_season_document(harbin, year, "drought"), harbin, backend="wofost"
            )
            cases.append(Probe(spec, year, split, False))
            if year in (1995, 2002, 2016):
                cases.append(Probe(spec, year, split, True))

    profile_path = files("evaluate").joinpath("scenarios/lib/l3/configs/changchun_hn58_2016_profile.json")
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    if (
        changchun["timezone"] != profile["timezone"]
        or changchun.get("requested_point", changchun["point"])
        != profile["requested_point"]
    ):
        raise ValueError(
            "Changchun archive does not match the held-out regional profile"
        )
    exposure_start, exposure_end = map(date.fromisoformat, profile["exposure"])
    document = make_regional_document(
        changchun,
        region=profile["region"],
        start=date.fromisoformat(profile["start_date"]),
        end=date.fromisoformat(profile["end_date"]),
        kind=profile["role"],
        farm=FarmSpec(**profile["farm"]),
        season=SeasonSpec.from_dict(profile["season"]),
        quota_mm=profile["quota_mm"],
        exposure=(exposure_start, exposure_end),
    )
    spec = compile_season(document, changchun, backend="wofost")
    cases.append(Probe(spec, 2016, "test", False))
    return cases


def collect(probe: Probe) -> list[Sample]:
    """Advance with real tools, exposing only the public receipts to the feature extractor."""
    runtime = plant_prefix(probe.spec, backend="wofost")
    if not isinstance(runtime.backend, WofostSeasonBackend):
        raise ValueError("PCSE truth is required for supervision")
    histories: dict[tuple[int, int], list[StressObservation]] = {
        zone: [] for zone in REGIONS
    }
    aligned: dict[tuple[int, int], list[AlignedDay]] = {zone: [] for zone in REGIONS}
    previous_soil: dict[tuple[int, int], tuple[float, ...]] = {}
    previous_irrigation = dict.fromkeys(REGIONS, 0.0)
    samples = []
    doses = 0
    while runtime.now.date() < runtime.deadline.date():
        stopped = []
        irrigation = dict.fromkeys(REGIONS, 0.0)
        for zone, history in histories.items():
            crop = require(call(runtime, "inspect_crop", **window(zone)))["receipt"]
            stopped.append(all(row["harvestable"] for row in crop["observations"]))
            soil = require(call(runtime, "observe_soil_range", **window(zone)))[
                "receipt"
            ]
            start_vwc = previous_soil.get(zone)
            previous_soil[zone] = tuple(row["root_vwc"] for row in soil["observations"])
            phases = {row["phase"] for row in crop["observations"]}
            if len(phases) != 1 or not phases <= {"vegetative", "reproductive"}:
                history.clear()
                aligned[zone].clear()
                continue

            if start_vwc is None:
                raise ValueError("Living crop lacks a start-of-day soil observation")
            weather = require(call(runtime, "get_current_weather"))
            reading = StressObservation.from_public(soil, crop, weather)
            day = reading.day.isoformat()
            # The rates were computed for the weather day visible in this receipt.
            diagnostics = [
                runtime.backend.engines[i].last_water_stress for i in reading.ridges
            ]
            if any(item is not None and item.day != day for item in diagnostics):
                raise ValueError("PCSE label date and public weather day do not match")
            history.append(reading)
            aligned[zone].append(
                AlignedDay(
                    start_vwc, weather["weather"]["et0_mm"], previous_irrigation[zone]
                )
            )
            if len(history) > WINDOW_DAYS:
                del history[0]
                del aligned[zone][0]
            if len(history) == WINDOW_DAYS and all(
                item is not None for item in diagnostics
            ):
                severity = sum(
                    1.0 - item.rfws for item in diagnostics if item is not None
                ) / len(diagnostics)
                samples.append(
                    Sample(
                        day,
                        zone,
                        window_features(history),
                        severity,
                        tuple(aligned[zone]),
                    )
                )
            if (
                zone == TARGET
                and probe.irrigated
                and doses < 2
                and phases == {"reproductive"}
                and reading.features[0] < 0.18
            ):
                receipt = require(
                    call(runtime, "irrigate_range", **window(zone), depth_mm=10.0)
                )["receipt"]
                if receipt["settlement"] != "next_local_midnight":
                    raise ValueError("Unexpected irrigation settlement contract")
                irrigation[zone] = receipt["depth_mm"]
                doses += 1
        if all(stopped):
            break
        next_day(runtime)
        previous_irrigation = irrigation
    else:
        raise ValueError(
            f"Did not reach mature crop in both probe zones: {probe.identity}"
        )
    if not samples:
        raise ValueError(f"No supervised windows: {probe.identity}")
    return samples


def scores(target: np.ndarray, predicted: np.ndarray) -> JsonObject:
    """Report error and class performance at the fixed 0.10 severity threshold."""
    if target.ndim != 1 or target.shape != predicted.shape or not target.size:
        raise ValueError(
            "Stress scores require paired, non-empty prediction and label vectors"
        )
    truth, signal = target >= DEFICIT_THRESHOLD, predicted >= DEFICIT_THRESHOLD
    tp, fp = int((truth & signal).sum()), int((~truth & signal).sum())
    fn, tn = int((truth & ~signal).sum()), int((~truth & ~signal).sum())
    return dict(
        samples=len(target),
        positives=int(truth.sum()),
        severity_mae=float(np.abs(target - predicted).mean()),
        precision=tp / (tp + fp) if tp + fp else None,
        recall=tp / (tp + fn) if tp + fn else None,
        specificity=tn / (tn + fp) if tn + fp else None,
        f1=2 * tp / (2 * tp + fp + fn) if tp + fp + fn else None,
        confusion=dict(tp=tp, fp=fp, fn=fn, tn=tn),
    )


def fit(
    inputs: np.ndarray, labels: np.ndarray, groups: np.ndarray, length: int, seed: int
) -> tuple[SoilWaterStressRegressor, np.ndarray, np.ndarray, np.ndarray, int]:
    """Fit on training years and select the checkpoint by validation severity MAE."""
    torch.manual_seed(seed)
    train, val = groups == "train", groups == "validation"
    x = inputs[:, -length:, :]
    mean = x[train].mean(axis=(0, 1))
    scale = x[train].std(axis=(0, 1))
    scale[scale < 1e-8] = 1.0
    tensor = torch.from_numpy((x - mean) / scale)
    target = torch.from_numpy(labels)
    train_x, train_y = tensor[train], target[train]
    model = SoilWaterStressRegressor(inputs.shape[-1])
    optimizer = torch.optim.AdamW(model.parameters(), lr=0.003)
    best_loss = float("inf")
    best = {}
    best_epoch = 0
    for epoch in range(1, 81):
        model.train()
        for batch in torch.randperm(len(train_x)).split(128):
            prediction = model(train_x[batch])
            loss = torch.nn.functional.mse_loss(prediction, train_y[batch])
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
        model.eval()
        with torch.inference_mode():
            val_loss = float((model(tensor[val]) - target[val]).abs().mean().item())
        if val_loss < best_loss:
            best_loss = val_loss
            best = {
                name: weight.detach().clone()
                for name, weight in model.state_dict().items()
            }
            best_epoch = epoch
    model.load_state_dict(best)
    with torch.inference_mode():
        predicted = model(tensor).numpy()
    return model, mean, scale, predicted, best_epoch


def collect_probes(cases: Sequence[Probe], workers: int) -> list[list[Sample]]:
    """Spawn isolated PCSE runtimes without forking the torch runtime."""
    from multiprocessing import get_context

    with ProcessPoolExecutor(
        max_workers=workers, mp_context=get_context("spawn")
    ) as pool:
        collected = []
        for case, batch in zip(cases, pool.map(collect, cases), strict=True):
            collected.append(batch)
            print(f"{case.identity} {case.split}: {len(batch)} windows", flush=True)

    return collected


def run_experiment(
    harbin: JsonObject, changchun: JsonObject, out: Path, *, seed: int, workers: int
) -> JsonObject:
    """Publish the dataset, two fitted checkpoints and independent test-year metrics."""
    cases = probes(harbin, changchun)
    if any(
        len({case.split for case in cases if case.year == year}) != 1
        for year in {case.year for case in cases}
    ):
        raise ValueError("A weather year appears in multiple partitions")
    out.mkdir(parents=True, exist_ok=False)

    collected = collect_probes(cases, workers)

    samples = [row for batch in collected for row in batch]
    x = np.array([row.features for row in samples], dtype=np.float32)
    y = np.array([row.severity for row in samples], dtype=np.float32)
    split = np.array(
        [
            case.split
            for case, batch in zip(cases, collected, strict=True)
            for _ in batch
        ]
    )
    ids = np.array(
        [
            case.identity
            for case, batch in zip(cases, collected, strict=True)
            for _ in batch
        ]
    )
    if any(
        not (
            np.any(y[split == name] >= DEFICIT_THRESHOLD)
            and np.any(y[split == name] < DEFICIT_THRESHOLD)
        )
        for name in ("train", "validation", "test")
    ):
        raise ValueError("Every partition needs both stress and non-stress labels")
    dataset = out / "dataset.npz"
    np.savez_compressed(
        dataset,
        x=x,
        y=y,
        split=split,
        case=ids,
        day=[row.day for row in samples],
        region=[row.region for row in samples],
    )
    manifest: JsonObject = dict(
        schema=1,
        label="zone mean of (1 − PCSE RFWS) for the last completed local day",
        deficit_threshold=DEFICIT_THRESHOLD,
        features=list(FEATURES),
        source_fingerprint=source_identity()["fingerprint"],
        archives=dict(harbin=harbin["fingerprint"], changchun=changchun["fingerprint"]),
        dataset_sha256=sha256(dataset.read_bytes()).hexdigest(),
        versions={k: version(k) for k in ("pcse", "torch", "numpy")},
        seed=seed,
        epochs=80,
        models={},
        cases=[
            dict(
                id=case.identity,
                year=case.year,
                split=case.split,
                size=len(batch),
                scenario_fingerprint=fingerprint(case.spec.to_dict()),
            )
            for case, batch in zip(cases, collected, strict=True)
        ],
    )
    metrics: JsonObject = {}
    test = split == "test"
    first_test_index = int(np.flatnonzero(test)[0])
    first_test_prediction = None
    for name, length in (("sequence", WINDOW_DAYS), ("current_day", 1)):
        model, mean, scale, predicted, epoch = fit(x, y, split, length, seed)
        if name == "sequence":
            first_test_prediction = float(predicted[first_test_index])
        weights = out / f"{name}.pt"
        torch.save(model.state_dict(), weights)
        manifest["models"][name] = dict(
            window_days=length,
            mean=mean.tolist(),
            scale=scale.tolist(),
            selected_epoch=epoch,
            parameters=sum(tensor.numel() for tensor in model.parameters()),
            sha256=sha256(weights.read_bytes()).hexdigest(),
        )
        metrics[name] = dict(
            validation=scores(
                y[split == "validation"], predicted[split == "validation"]
            ),
            test=scores(y[test], predicted[test]),
            by_test_case={
                case_id: scores(y[ids == case_id], predicted[ids == case_id])
                for case_id in sorted(set(ids[test]))
            },
        )
    heuristic = (x[:, -1, 0] < 0.18).astype(np.float32)
    metrics["vwc_below_0p18"] = {
        k: v for k, v in scores(y[test], heuristic[test]).items() if k != "severity_mae"
    }

    write_json(out / "manifest.json", manifest)
    probe_sample = samples[first_test_index]
    first = date.fromisoformat(probe_sample.day)

    readings = [
        StressObservation(
            day=first - timedelta(days=WINDOW_DAYS - 1 - i),
            ridges=tuple(range(probe_sample.region[0], probe_sample.region[1] + 1)),
            features=values,
        )
        for i, values in enumerate(probe_sample.features)
    ]
    prediction = SoilStressPredictor.load(out).predict(readings)
    if (
        prediction.day != first
        or first_test_prediction is None
        or abs(prediction.severity - first_test_prediction) > 1e-6
    ):
        raise ValueError("Serialized model failed inference smoke check")
    write_json(out / "metrics.json", metrics)
    return metrics


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--harbin", required=True, type=Path)
    parser.add_argument("--changchun", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--seed", default=42, type=int)
    parser.add_argument("--workers", default=4, type=int)
    args = parser.parse_args(argv)
    if not 1 <= args.workers <= 8:
        parser.error("Expected 1–8 collector workers")
    torch.set_num_threads(1)
    run_experiment(
        load_json(args.harbin),
        load_json(args.changchun),
        args.output,
        seed=args.seed,
        workers=args.workers,
    )
    print(args.output / "metrics.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
