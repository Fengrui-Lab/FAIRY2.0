"""Optional CPU MaskablePPO training and held-out evaluation with frozen semantics."""

from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from evaluate.metrics import TaskMetrics
    from fairy.acps.ontology.contracts import JsonObject
from pathlib import Path
import hashlib
import time
from fairy.events.artifacts import write_json, artifact, replay
from fairy.events.provenance import source_identity
from .irrigation import DOSES, FEATURES


def train(
    destination: str | Path, *, timesteps: int = 2048, seed: int = 42, backend: str = "native"
) -> JsonObject:
    """运行真实有限PPO训练，记录参数更新、来源和权重哈希；不宣称已收敛。"""
    import torch
    from sb3_contrib import MaskablePPO
    from evaluate.oracle.environment import IrrigationEnv

    if type(timesteps) is not int or timesteps < 128:
        raise ValueError("At least 128 training timesteps required")
    path = Path(destination)
    if path.suffix != ".zip" or path.exists() or path.with_suffix(".json").exists():
        raise ValueError("Choose a new .zip policy path")
    path.parent.mkdir(parents=True, exist_ok=True)
    torch.set_num_threads(1)
    env = IrrigationEnv(backend=backend)
    source = source_identity()["fingerprint"]
    # Benchmark a complete episode before choosing/running the requested finite budget.
    started = time.monotonic()
    env.reset()
    done = False
    while not done:
        _, _, terminated, truncated, _ = env.step(0)
        done = terminated or truncated
    benchmark = time.monotonic() - started
    model = MaskablePPO(
        "MlpPolicy",
        env,
        n_steps=128,
        batch_size=64,
        n_epochs=4,
        seed=seed,
        learning_rate=3e-4,
        device="cpu",
        policy_kwargs={"net_arch": [32, 32]},
        verbose=0,
    )
    before = {k: v.detach().clone() for k, v in model.policy.state_dict().items()}
    started = time.monotonic()
    model.learn(total_timesteps=timesteps)
    seconds = time.monotonic() - started
    delta = sum(float((v - before[k]).abs().sum()) for k, v in model.policy.state_dict().items())
    actor_delta = sum(
        float((v - before[k]).abs().sum())
        for k, v in model.policy.state_dict().items()
        if "policy_net" in k or "action_net" in k
    )
    if delta <= 0 or actor_delta <= 0:
        raise ValueError("No learned actor update occurred")
    if source_identity()["fingerprint"] != source:
        raise ValueError("Executable source changed during training")
    import tempfile, os

    with tempfile.TemporaryDirectory(dir=path.parent) as temporary:
        checkpoint = Path(temporary) / "policy.zip"
        model.save(checkpoint)
        os.link(checkpoint, path)  # atomic no-clobber publication
    import stable_baselines3, sb3_contrib, gymnasium
    from evaluate.scenarios.lib.l3.soybean_normal import soybean_normal
    from fairy.events.records import fingerprint
    from evaluate.scenarios.lib.l3.soybean_wofost import (
        soybean_wofost_normal,
        soybean_wofost_calendar_drought,
        soybean_wofost_reproductive_drought,
    )

    cases = (
        [
            soybean_wofost_normal(),
            soybean_wofost_calendar_drought(),
            soybean_wofost_reproductive_drought(),
        ]
        if backend == "wofost"
        else [soybean_normal()]
    )
    spec = cases[0]
    assert spec.season is not None and env.runtime is not None
    manifest = dict(
        schema=2,
        feature_version=2,
        algorithm="MaskablePPO",
        backend=backend,
        crop=spec.season.cultivar,
        features=list(FEATURES),
        doses_mm=list(DOSES),
        qualification_scenario_fingerprint=fingerprint(spec.to_dict()),
        qualification_scenario_fingerprints=[fingerprint(s.to_dict()) for s in cases],
        parameter_scope={
            "engine": env.runtime.backend.capabilities.model,
            "version": env.runtime.backend.capabilities.version,
            "crop_data_sha256": env.runtime.backend.export_state().get("crop_data_sha256"),
            "cultivar": spec.season.cultivar,
        },
        qualification_scope="declared backend and listed scenario fingerprints only; performance separately evaluated",
        source_fingerprint=source,
        weights_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        seed=seed,
        training_scenario_seeds=list(range(8)),
        heldout_scenario_seeds=list(range(100, 105)),
        hyperparameters={
            "gamma": 0.99,
            "n_steps": 128,
            "batch_size": 64,
            "n_epochs": 4,
            "learning_rate": 3e-4,
            "policy_hidden_units": [32, 32],
            "device": "cpu",
        },
        training_variation={
            "rain_mm": [2.7, 3.3],
            "et0_mm": [2.8, 3.2],
            "initial_vwc": [0.29, 0.32],
            "normal_test_case_mutated": False,
            "weather_generation": "independent random.Random(seed); full daily trajectories differ",
        },
        reward={
            "stored_dry_mass_scale_kg": 3500,
            "water_cost": 0.2,
            "terminal_failure": -1,
            "definition": "step: -water_cost*dose/quota; terminal: actual safely stored dry mass / scale on task success, else -1",
        },
        timesteps=int(model.num_timesteps),
        weight_l1_change=delta,
        actor_l1_change=actor_delta,
        benchmark_episode_seconds=benchmark,
        training_seconds=seconds,
        packages={
            "torch": torch.__version__,
            "stable_baselines3": stable_baselines3.__version__,
            "sb3_contrib": sb3_contrib.__version__,
            "gymnasium": gymnasium.__version__,
        },
        status="trained; numerical superiority not established",
    )
    write_json(path.with_suffix(".json"), manifest)
    return manifest


def evaluate_policy(path: str | Path, destination: str | Path) -> dict[str, TaskMetrics]:
    """用显式零/固定剂量/学习策略对照验证冻结权重，逐条保存并严格回放。"""
    from evaluate.oracle.environment import IrrigationEnv
    from .irrigation import PPOAdvisor

    policy = PPOAdvisor(path, expected_source=source_identity()["fingerprint"])
    backend = policy.manifest["backend"]
    out = Path(destination)
    out.mkdir(parents=True, exist_ok=False)
    # Separate random held-out inputs from fixed named evaluation cases.
    cases = [(str(seed), IrrigationEnv(seeds=(seed,), backend=backend)) for seed in range(100, 105)]
    cases.append(("canonical", IrrigationEnv(canonical=True, backend=backend)))
    if backend == "wofost":
        from evaluate.scenarios.lib.l3.soybean_wofost import (
            soybean_wofost_calendar_drought,
            soybean_wofost_reproductive_drought,
        )

        cases.extend(
            (name, IrrigationEnv(backend=backend, scenario=factory()))
            for name, factory in [
                ("calendar_drought", soybean_wofost_calendar_drought),
                ("phase_drought", soybean_wofost_reproductive_drought),
            ]
        )
    records = {}
    for label, env in cases:
        for strategy in ("zero", "fixed10", "ppo"):
            env.reset()
            done = False
            while not done:
                masks = env.action_masks()
                action = (
                    0 if strategy == "zero" else (2 if strategy == "fixed10" and masks[2] else 0)
                )
                if strategy == "ppo":
                    action = policy.advise(env.context)["action"]
                _, _, terminated, truncated, _ = env.step(action)
                done = terminated or truncated
            assert env.runtime is not None
            record = artifact(
                env.runtime,
                {"kind": "numeric_evaluation_driver", "strategy": strategy, "real_llm": False},
            )
            _, metrics = replay(record)
            name = label + "_" + strategy
            write_json(out / (name + ".json"), record)
            records[name] = metrics
            print(name, metrics["task_success"], metrics["stored_dry_grain_kg"], flush=True)
    write_json(out / "comparison.json", records)
    return records
