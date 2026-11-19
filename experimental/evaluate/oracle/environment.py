"""Gymnasium adapter over FAIRY's real public operations, not another crop model."""

from __future__ import annotations
from typing import Any, Sequence, TYPE_CHECKING

if TYPE_CHECKING:
    from evaluate.metrics import TaskMetrics
    from evaluate.scenarios.spec import ScenarioSpec
    from fairy.acps.ontology.contracts import JsonObject
    from fairy.agents.loop.context.public import PublicContext
    from numpy.typing import NDArray
from dataclasses import replace
from datetime import date, timedelta
import random
import numpy as np
import gymnasium as gym
from gymnasium import spaces
from fairy.acps.ontology.contracts import OperationRequest
from fairy.acps.ontology.farm.spec import FarmSpec
from fairy.acps.backends.biology.contracts import WeatherDay
from fairy.events.session import Runtime
from fairy.events.artifacts import artifact
from fairy.agents.loop.context.public import build_context
from fairy.agents.loop.context.history import public_history
from fairy.agents.models.numeric.irrigation import FEATURES, DOSES, features, action_masks
from evaluate.scenarios.lib.l3.common import full_season_spec
from evaluate.scenarios.lib.l3.soybean_normal import soybean_normal
from evaluate.scenarios.season import SeasonSpec
from evaluate.metrics import evaluate


def training_spec(seed: int, backend: str = "native") -> ScenarioSpec:
    """Separate bounded normal-condition variants; never edits the registered test case."""
    rng = random.Random(seed)
    start, end = date(2026, 5, 5), date(2026, 10, 22)
    weather = tuple(
        WeatherDay(
            (start + timedelta(days=i)).isoformat(),
            tmin_c=16,
            tmax_c=28,
            rain_mm=rng.uniform(2.7, 3.3),
            radiation_mj_m2=18,
            wind_ms=2,
            vapor_pressure_hpa=12,
            et0_mm=rng.uniform(2.8, 3.2),
        )
        for i in range((end - start).days + 1)
    )
    return full_season_spec(
        scenario_id=f"{backend}_soybean_training_normal_{seed}",
        weather=weather,
        farm=FarmSpec(length_m=268, width_m=64 * 1.1, initial_vwc=rng.uniform(0.29, 0.32)),
        season=(
            replace(SeasonSpec(), cultivar="Soybean_901_proxy")
            if backend == "wofost"
            else SeasonSpec()
        ),
        quota_mm=60,
    )


def call(runtime: Runtime, tool: str, **args: Any) -> JsonObject:
    """通过Runtime公共入口执行固定验证操作，拒绝时中止该验证。"""
    result = runtime.invoke(OperationRequest(tool, args), actor="numeric_training_driver")
    if not result.ok:
        raise ValueError("Training driver rejected: " + str(result.error))
    return result.data


def observe(runtime: Runtime) -> PublicContext:
    """读取公开观测；不推进业务时钟，不结算pending，也不更新生长。"""
    call(runtime, "inspect_crop", start_ridge=0, end_ridge=63)
    call(runtime, "observe_soil_range", start_ridge=0, end_ridge=63)
    call(runtime, "get_current_weather")
    call(runtime, "get_forecast", days=3)
    return build_context(runtime, public_history(runtime), max_chars=60000, max_turns=4)


def finish_driver(runtime: Runtime) -> TaskMetrics:
    """Fixed non-learning setup/cleanup is only for isolating dose learning."""
    for first in range(0, 64, 8):
        call(runtime, "harvest", start_ridge=first, end_ridge=first + 7)
        call(runtime, "unload")
        inventory = call(runtime, "inspect_inventory")["receipt"]
        if inventory["trailer"]["moisture"] > runtime.season_config.safe_moisture:
            call(runtime, "dry_grain", target_moisture=0.13)
        call(runtime, "store_grain")
    runtime.finish("numeric_training_complete")
    return evaluate(runtime)


class IrrigationEnv(gym.Env):
    """隔离灌溉剂量学习的Gymnasium环境。

    动作使用公共工具，播前和收后采用固定driver；该driver不能冒充真实LLM工作流。
    runtime在reset之前为None，context仅由公开观测构建。"""

    metadata = {}

    def __init__(
        self,
        *,
        seeds: Sequence[int] = tuple(range(8)),
        canonical: bool = False,
        mass_scale_kg: float = 3500.0,
        water_cost: float = 0.2,
        backend: str = "native",
        scenario: ScenarioSpec | None = None,
    ) -> None:
        """保存显式依赖与初态；模型服务调用发生在决策阶段而非构造阶段。"""
        self.fixed_scenario = scenario
        self.backend_name = backend
        if backend not in ("native", "wofost"):
            raise ValueError("Unknown training backend")
        self.seeds = tuple(seeds)
        self.canonical = canonical
        self.episode = 0
        self.mass_scale_kg = mass_scale_kg
        self.water_cost = water_cost
        if not self.seeds or mass_scale_kg <= 0 or water_cost < 0:
            raise ValueError("Invalid training configuration")
        self.action_space = spaces.Discrete(len(DOSES))
        self.observation_space = spaces.Box(
            low=0, high=np.inf, shape=(len(FEATURES),), dtype=np.float32
        )
        self.runtime: Runtime | None = None

    def reset(
        self, *, seed: int | None = None, options: JsonObject | None = None
    ) -> tuple[NDArray[np.float32], JsonObject]:
        """建立一个新训练场景并通过公共操作播种观测，返回特征与场景身份。"""
        super().reset(seed=seed)
        if seed is not None:
            self.episode = 0
        index = (
            self.seeds[self.episode % len(self.seeds)]
            if options is None
            else options.get("scenario_seed", self.seeds[0])
        )
        self.episode += 1
        from evaluate.scenarios.lib.l3.soybean_wofost import soybean_wofost_normal

        spec = (
            (soybean_wofost_normal() if self.backend_name == "wofost" else soybean_normal())
            if self.canonical
            else training_spec(index, self.backend_name)
        )
        self.runtime = Runtime(self.fixed_scenario or spec, self.backend_name)
        call(self.runtime, "prepare_seedbed", start_ridge=0, end_ridge=63)
        call(self.runtime, "plant", start_ridge=0, end_ridge=63, spacing_m=0.079, depth_mm=40)
        self.context = observe(self.runtime)
        return np.asarray(features(self.context), dtype=np.float32), {
            "scenario_fingerprint": self.runtime.export_state()["scenario_fingerprint"]
        }

    def action_masks(self) -> NDArray[np.bool_]:
        return np.asarray(action_masks(self.context), dtype=bool)

    def step(self, action: int) -> tuple[NDArray[np.float32], float, bool, bool, JsonObject]:
        """执行合法剂量并推进一天；成熟后收储，奖励使用实际干物质和声明水成本。"""
        if self.runtime is None or self.runtime.done:
            raise ValueError("Reset an episode before stepping")
        if (
            not isinstance(action, (int, np.integer))
            or not 0 <= int(action) < len(DOSES)
            or not self.action_masks()[int(action)]
        ):
            raise ValueError("Illegal masked action; no operation executed")
        dose = DOSES[int(action)]
        if dose:
            call(self.runtime, "irrigate_range", start_ridge=0, end_ridge=63, depth_mm=dose)
        from datetime import timedelta

        if self.runtime.now + timedelta(hours=24) > self.runtime.deadline:
            self.runtime.finish("training_deadline")
            return (
                np.asarray(features(self.context), dtype=np.float32),
                -1.0,
                False,
                True,
                {"failure": "deadline"},
            )
        call(self.runtime, "advance_time", hours=24)
        self.context = observe(self.runtime)
        crop = self.context["history"][-4]["result"]["data"]["receipt"]["observations"]
        terminated = all(r.get("harvestable", r["stage"] == "R8") for r in crop)
        reward = -self.water_cost * dose / self.runtime.scenario.irrigation_quota_mm
        info = {}
        if terminated:
            try:
                metrics = finish_driver(self.runtime)
                info["metrics"] = metrics
            except ValueError as exc:
                self.runtime.finish("training_delivery_failure")
                return (
                    np.asarray(features(self.context), dtype=np.float32),
                    -1.0,
                    False,
                    True,
                    {"failure": str(exc)},
                )
            reward += (
                metrics["stored_dry_grain_kg"] / self.mass_scale_kg
                if metrics["task_success"]
                else -1.0
            )
        return np.asarray(features(self.context), dtype=np.float32), reward, terminated, False, info
