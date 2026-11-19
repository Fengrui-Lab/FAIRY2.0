"""Only statically checked: importing this file must not launch an engine/model."""

from datetime import datetime
from typing_extensions import assert_type
from fairy.acps.backends.biology.contracts import (
    BiologyBackend,
    SeasonBiologyBackend,
    SoilReading,
    make_backend,
)
from fairy.acps.backends.biology.native.backend import NativeBackend
from fairy.acps.backends.biology.native.season import NativeSeasonBackend
from fairy.acps.backends.biology.wofost.backend import WofostBackend
from fairy.acps.backends.biology.wofost.season import WofostSeasonBackend
from fairy.acps.backends.physical.contracts import GrainBatch, WorkPlan
from fairy.acps.ontology.infrastructure.soil_sensor import SoilSensor
from fairy.acps.ontology.farm.spec import FarmSpec
from fairy.acps.ontology.crops.lib.soybean import SoybeanSpec
from fairy.agents.loop.context.public import PublicContext
from fairy.agents.models.decision import PublicAgent, PrivilegedAgent, Decision
from fairy.events.session import Runtime
from evaluate.scenarios.season import SeasonSpec
from evaluate.scenarios.spec import ScenarioSpec
from evaluate.metrics import evaluate


def navigate(sensor: SoilSensor, backend: BiologyBackend, now: datetime) -> None:
    assert_type(sensor.observe(backend, now), SoilReading)
    assert_type(sensor.observe(backend, now)["root_vwc"], float)


def factories(farm: FarmSpec, crop: SoybeanSpec, season: SeasonSpec) -> None:
    assert_type(make_backend("native", farm, crop, "2026-05-05"), NativeBackend)
    assert_type(make_backend("wofost", farm, crop, "2026-05-05"), WofostBackend)
    assert_type(make_backend("native", farm, crop, "2026-05-05", season), NativeSeasonBackend)
    assert_type(make_backend("wofost", farm, crop, "2026-05-05", season), WofostSeasonBackend)


def boundaries(
    runtime: Runtime, public: PublicContext, agent: PublicAgent, oracle: PrivilegedAgent
) -> None:
    assert_type(runtime.backend, BiologyBackend)
    assert_type(runtime.season_backend, SeasonBiologyBackend)
    assert_type(agent.decide(public), Decision)
    assert_type(oracle.decide(runtime), Decision)
    assert_type(evaluate(runtime)["task_success"], bool)
    assert_type(runtime.season_physical.state["combine"], GrainBatch)
    assert_type(
        runtime.season_physical.plan(
            "harvest", {}, runtime.season_backend, runtime.weather["2026-05-05"]
        ),
        WorkPlan,
    )
