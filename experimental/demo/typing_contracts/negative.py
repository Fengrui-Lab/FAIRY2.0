"""Intentional errors: the checker must reject these domain boundary violations."""

from datetime import datetime
from fairy.acps.backends.biology.contracts import BiologyBackend
from fairy.acps.backends.physical.contracts import GrainBatch
from fairy.acps.ontology.infrastructure.soil_sensor import SoilSensor
from fairy.agents.models.decision import PublicAgent
from fairy.events.session import Runtime


def reject(
    sensor: SoilSensor, backend: BiologyBackend, now: datetime, agent: PublicAgent, runtime: Runtime
) -> None:
    sensor.observe("native", now)
    sensor.observe(backend, "2026-05-05")
    backend.advance_day("sunny", 10.0)
    backend.queue_irrigation(range(64), 10.0)
    grain: GrainBatch = {"dry_kg": "100", "water_kg": 10.0}
    agent.decide(runtime)
