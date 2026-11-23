"""Matched PPO budgets, validation-only model selection, and held-out evaluation."""

from __future__ import annotations

from functools import partial
from importlib.metadata import version
from pathlib import Path
from typing import Callable, Sequence
import hashlib

from gymnasium import Env
import numpy as np
from numpy.typing import NDArray
import torch
from sb3_contrib import MaskablePPO
from stable_baselines3.common.vec_env import DummyVecEnv, SubprocVecEnv

from evaluate.oracle.representation_env import Arm, Case, RepresentationEnv
from evaluate.oracle.representation_study import (
    ARMS,
    CONTROLS,
    DROUGHT_BALANCE_MM,
    SEEDS,
    _control_job,
    _parallel,
    _season_identity,
    _valid,
    finish,
)
from fairy.acps.ontology.contracts import JsonObject
from fairy.agents.models.numeric.modeling import (
    IrrigationActorCriticPolicy,
    IrrigationWindowEncoder,
)
from fairy.events.artifacts import load_json, write_json
from fairy.events.provenance import source_identity
from fairy.events.records import fingerprint

SMOKE_STEPS = 4096
TOTAL_STEPS = 16384
HYPERPARAMETERS = dict(
    n_steps=128, batch_size=64, n_epochs=4, learning_rate=3e-4, gamma=0.99
)


def _file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _evaluate_policy(
    model: MaskablePPO, case: Case, arm: Arm, path: Path, metadata: JsonObject
) -> JsonObject:
    summary = path.with_suffix(".summary.json")
    if summary.exists():
        row = load_json(summary)
        record = load_json(path)
        if (
            row["policy"] != metadata
            or fingerprint(record) != row["artifact_fingerprint"]
            or record["state"]["scenario_fingerprint"]
            != fingerprint(case.spec.to_dict())
        ):
            raise ValueError(f"Incorrect or stale policy artifact: {path}")
        return row
    env = RepresentationEnv([case], arm)
    observation, _ = env.reset()

    def choose(env: RepresentationEnv, obs: NDArray[np.float32]) -> int:
        action, _ = model.predict(
            obs, deterministic=True, action_masks=env.action_masks()
        )
        return int(action.item())

    row, record = finish(env, choose, observation=observation, details=metadata)
    row["policy"] = metadata
    write_json(path, record, overwrite=path.exists())
    write_json(summary, row)
    return row


def _policy_metadata(
    model: MaskablePPO, checkpoint: Path, arm: Arm, seed: int, steps: int
) -> JsonObject:
    return dict(
        arm=arm,
        seed=seed,
        timesteps=steps,
        checkpoint=checkpoint.name,
        checkpoint_sha256=_file_digest(checkpoint),
        parameters=sum(p.numel() for p in model.policy.parameters()),
    )


def _fit_arm(
    train: Sequence[Case],
    validation: Sequence[Case],
    arm: Arm,
    seed: int,
    folder: Path,
    workers: int,
    *,
    smoke: bool,
) -> JsonObject:
    """Restart incomplete fits from the same seed; reuse only completed selections."""
    selection = folder / "selection.json"
    if selection.exists():
        row = load_json(selection)
        if (
            _file_digest(folder / row["policy"]["checkpoint"])
            != row["policy"]["checkpoint_sha256"]
        ):
            raise ValueError(f"Stored checkpoint checksum differs: {selection}")
        return row
    folder.mkdir(parents=True, exist_ok=True)
    from shutil import rmtree

    # An interrupted fit cannot restore the rollout buffer or environments from
    # an SB3 inference checkpoint. Its incomplete validation subtrees are reset.
    validation_path = folder / "validation"
    if validation_path.exists():
        rmtree(validation_path)
    # n_steps * n_envs divides both planned budgets exactly.
    lanes = max(i for i in (1, 2, 4, 8) if i <= workers)
    constructors: list[Callable[[], Env]] = [
        partial(RepresentationEnv, train, arm, offset=i * len(train) // lanes)
        for i in range(lanes)
    ]
    env = (
        SubprocVecEnv(constructors, start_method="spawn")
        if lanes > 1
        else DummyVecEnv(constructors)
    )
    outputs: list[JsonObject] = []
    try:
        model = MaskablePPO(
            IrrigationActorCriticPolicy,
            env,
            policy_kwargs=(
                {"features_extractor_class": IrrigationWindowEncoder}
                if arm == "D"
                else {}
            ),
            seed=seed,
            device="cpu",
            **HYPERPARAMETERS,
        )
        stop = SMOKE_STEPS if smoke else TOTAL_STEPS
        for steps in range(SMOKE_STEPS, stop + 1, SMOKE_STEPS):
            model.learn(SMOKE_STEPS, reset_num_timesteps=False)
            if model.num_timesteps != steps:
                raise ValueError("PPO training steps do not match the declared budget")
            checkpoint = folder / f"steps-{steps}.zip"
            model.save(checkpoint)
            metadata = _policy_metadata(model, checkpoint, arm, seed, steps)
            rows = [
                _evaluate_policy(
                    model,
                    case,
                    arm,
                    validation_path / f"steps-{steps}-{case.key}.json",
                    metadata,
                )
                for case in validation
            ]
            score = (
                float(np.mean([r["objective"] for r in rows]))
                if all(map(_valid, rows))
                else None
            )
            outputs.append(dict(policy=metadata, validation=rows, mean_objective=score))
            print(f"{arm}/{seed} {steps}/{stop}: validation {score}", flush=True)
    finally:
        env.close()
    valid = [row for row in outputs if row["mean_objective"] is not None]
    best = max(valid, key=lambda row: row["mean_objective"], default=None)
    result = dict(
        policy=(best or outputs[-1])["policy"],
        deployable=best is not None,
        validation_candidates=outputs,
    )
    write_json(selection, result)
    return result


def _paired_report(
    frozen: list[JsonObject],
    rows: list[JsonObject],
    controls: list[JsonObject],
    strongest: str,
) -> JsonObject:
    drought_cases = sorted(
        {r["case"] for r in controls if r["climate_balance_mm"] <= DROUGHT_BALANCE_MM}
    )
    baseline = {(r["case"], r["control"]): r for r in controls}
    by_policy = {(r["policy"]["arm"], r["policy"]["seed"], r["case"]): r for r in rows}
    deltas = []
    seed_wins = dict.fromkeys(ARMS, 0)
    ablations: dict[str, list[JsonObject]] = {"A_to_B": [], "B_to_C": [], "C_to_D": []}
    for selection in frozen:
        arm, seed = selection["policy"]["arm"], selection["policy"]["seed"]
        wins = 0
        for case in sorted({r["case"] for r in controls}):
            row = by_policy[arm, seed, case]
            rule, best = (baseline[case, name] for name in ("public_rule", strongest))
            margin = row["objective"] - max(rule["objective"], best["objective"])
            valid = (
                selection["deployable"]
                and _valid(row)
                and _valid(rule)
                and _valid(best)
            )
            deltas.append(
                dict(
                    arm=arm,
                    seed=seed,
                    case=case,
                    drought=case in drought_cases,
                    valid=valid,
                    objective_delta_vs_both_controls=margin,
                    dry_kg_delta_vs_public_rule=row["metrics"]["stored_dry_grain_kg"]
                    - rule["metrics"]["stored_dry_grain_kg"],
                    water_mm_delta_vs_public_rule=row["metrics"]["water_used_mm"]
                    - rule["metrics"]["water_used_mm"],
                )
            )
            wins += bool(case in drought_cases and valid and margin > 1e-9)
        all_valid = all(
            _valid(by_policy[arm, seed, case]) for case in {r["case"] for r in controls}
        )
        seed_wins[arm] += bool(all_valid and wins > len(drought_cases) / 2)
    for name, (left, right) in zip(ablations, zip(ARMS, ARMS[1:])):
        for seed in SEEDS:
            for case in sorted({r["case"] for r in controls}):
                a, b = (by_policy[arm, seed, case] for arm in (left, right))
                ablations[name].append(
                    dict(
                        seed=seed,
                        case=case,
                        both_valid=_valid(a) and _valid(b),
                        objective_delta=b["objective"] - a["objective"],
                    )
                )
    return dict(
        drought_cases=drought_cases,
        paired_deltas=deltas,
        ablations=ablations,
        winning_seeds=seed_wins,
        eligible_for_llm=[arm for arm in ARMS if seed_wins[arm] >= 2],
    )


def train_study(
    seasons: Sequence[Case], out: Path, workers: int, *, smoke: bool
) -> None:
    """Check the timing gate and seal every validation choice before held-out execution."""
    timing = load_json(out / "gate.json")
    source = source_identity()["fingerprint"]
    identity = _season_identity(seasons)
    if timing["source_fingerprint"] != source or timing["seasons"] != identity:
        raise ValueError(
            "Run the timing gate with the same executable and scenario inputs"
        )
    if not timing["timing_passed"]:
        print("Training deferred: the historical timing gate did not pass.", flush=True)
        return
    torch.set_num_threads(1)
    phase = "smoke" if smoke else "formal"
    folder = out / phase
    manifest_path = folder / "manifest.json"
    train = [c for c in seasons if c.split == "train"]
    validation = [c for c in seasons if c.split == "validation"]
    test = [c for c in seasons if c.split == "test"]
    seeds = SEEDS[:1] if smoke else SEEDS
    manifest = dict(
        source_fingerprint=source,
        seasons=identity,
        training_gate=fingerprint(timing),
        workers=workers,
        arms=list(ARMS),
        seeds=list(seeds),
        ppo=HYPERPARAMETERS,
        budget=SMOKE_STEPS if smoke else TOTAL_STEPS,
        versions={
            p: version(p)
            for p in ("sb3-contrib", "stable-baselines3", "gymnasium", "torch", "pcse")
        },
    )
    if manifest_path.exists():
        if load_json(manifest_path) != manifest:
            raise ValueError("Training configuration differs from the existing run")
    else:
        write_json(manifest_path, manifest)
    frozen = []
    for arm in ARMS:
        for seed in seeds:
            ordered = np.random.default_rng(seed).permutation(len(train))
            ordered_train = [train[i] for i in ordered]
            frozen.append(
                _fit_arm(
                    ordered_train,
                    validation,
                    arm,
                    seed,
                    folder / f"{arm}-{seed}",
                    workers,
                    smoke=smoke,
                )
            )
    freeze_path = folder / "frozen.json"
    if freeze_path.exists():
        if load_json(freeze_path) != frozen:
            raise ValueError("Frozen validation checkpoint selection differs")
    else:
        write_json(freeze_path, frozen)
    if smoke:
        return
    controls = _parallel(
        partial(_control_job, out / "heldout-controls"),
        [(case, control) for case in test for control in CONTROLS],
        workers,
    )
    rows = []
    for selection in frozen:
        metadata = selection["policy"]
        arm, seed = metadata["arm"], metadata["seed"]
        checkpoint = folder / f"{arm}-{seed}" / metadata["checkpoint"]
        model = MaskablePPO.load(checkpoint, device="cpu")
        for case in test:
            rows.append(
                _evaluate_policy(
                    model,
                    case,
                    arm,
                    folder / "heldout" / f"{arm}-{seed}-{case.key}.json",
                    metadata,
                )
            )
    comparison = _paired_report(
        frozen, rows, controls, timing["strongest_validation_control"]
    )
    write_json(
        folder / "report.json",
        dict(controls=controls, rows=rows, **comparison),
        overwrite=True,
    )
