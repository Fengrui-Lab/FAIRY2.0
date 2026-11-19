"""Physical adapters share Runtime's clock and transaction/recovery boundary."""

from __future__ import annotations
from typing import Final, Literal, Protocol, TypedDict
from dataclasses import dataclass
from fairy.acps.ontology.contracts import JsonObject
from fairy.acps.backends.biology.contracts import SeasonBiologyBackend, WeatherDay


@dataclass(frozen=True)
class PhysicalCapabilities:
    """物理引擎身份和操作目录；不表示已实现真实运动动力学。"""

    backend_id: str
    model: str
    version: str
    operations: tuple[str, ...] = ()


class GrainBatch(TypedDict):
    """容器中的干粮和水，分别以kg计；含水率由两者推导。"""

    dry_kg: float
    water_kg: float


class StoredBatch(GrainBatch):
    """已入库批次；moisture为水质量/总质量，非干基含水率。"""

    moisture: float


class PhysicalState(TypedDict):
    """唯一装备/物料账本；单位为kg、L、kWh，battery为百分比。"""

    prepared: list[bool]
    seeds: int
    seeds_used: int
    fuel_l: float
    fuel_used_l: float
    electricity_kwh: float
    battery: dict[str, float]
    combine: GrainBatch
    trailer: GrainBatch
    stored_batches: list[StoredBatch]
    harvested_dry_kg: float
    harvested_water_kg: float
    harvest_loss_dry_kg: float
    harvest_loss_water_kg: float
    removed_water_kg: float


COMPONENTS: Final[tuple[Literal["dry_kg", "water_kg"], ...]] = ("dry_kg", "water_kg")
CONTAINERS: Final[tuple[Literal["combine", "trailer"], ...]] = ("combine", "trailer")


class WorkPlan(TypedDict):
    """纯预检结果：作业耗时小时、燃料升；时钟仍归Runtime维护。"""

    duration_hours: float
    fuel_l: float


class PhysicalBackend(Protocol):
    """作业、库存和质量账本接口；完成时重新验证，失败由Runtime整体回滚。"""

    @property
    def capabilities(self) -> PhysicalCapabilities: ...
    def plan(
        self, op: str, args: JsonObject, biology: SeasonBiologyBackend, weather: WeatherDay
    ) -> WorkPlan:
        """不改变任何状态地检查前提、容量和资源，计算作业成本。"""
        ...

    def execute(
        self, op: str, args: JsonObject, biology: SeasonBiologyBackend, weather: WeatherDay
    ) -> JsonObject:
        """在完成时提交资源/粮食转移，不自行推进时钟。"""
        ...

    def inventory(self) -> JsonObject: ...
    def audit(self) -> JsonObject: ...
    def export_state(self) -> JsonObject: ...
    def load_state(self, payload: JsonObject) -> None: ...
    @staticmethod
    def weight(batch: GrainBatch) -> float: ...
    @classmethod
    def moisture(cls, batch: GrainBatch) -> float: ...
