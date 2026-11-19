"""Equipment operations and explicit provider catalog for the season fixture."""

from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fairy.acps.ontology.contracts import OperationDefinition
from dataclasses import dataclass, replace
from fairy.acps.ontology.contracts import ObjectSpec, Parameter, receipt_operation as operation
from fairy.acps.ontology.farm.observations import RANGE, FIELD, SOIL
from fairy.acps.ontology.infrastructure.season import IRRIGATE, DRY, STORE, INVENTORY, CHARGE

PREPARE = operation(
    "prepare_seedbed", "Prepare unplanted ridges using tractor fuel and time.", RANGE
)
PLANT = operation(
    "plant",
    "Sow HEINONG58 on prepared ridges using seed inventory. Spacing is in metres and depth in mm, two rows per ridge.",
    RANGE
    + (
        Parameter("spacing_m", "number", 0.01, 0.5, unit="m"),
        Parameter("depth_mm", "number", 10, 100, unit="mm"),
    ),
)
PLANT = replace(PLANT, version=2)  # distinct public rejection reasons
HARVEST = operation(
    "harvest",
    "Harvest selected mature, harvestable ridges into the combine; the backend determines maturity. Rejects daily rain above 10 mm, wet soil, immature crop and insufficient bin capacity.",
    RANGE,
)
HARVEST = replace(HARVEST, version=2)
UNLOAD = operation(
    "unload", "Transfer all combine grain to the trailer; conserves dry grain and water."
)
MULTISPECTRAL = operation(
    "survey_multispectral",
    "Synthetic NDVI survey using the multispectral drone; consumes battery and time.",
    RANGE,
)
THERMAL = operation(
    "survey_thermal",
    "Synthetic canopy-air temperature anomaly using the thermal drone; consumes battery and time.",
    RANGE,
)
GROUND = operation(
    "ground_inspect",
    "Ground robot visual stage and soil moisture inspection; consumes battery and time.",
    RANGE,
)

PROVIDERS = {
    "crop_sensor": (FIELD, SOIL),
    "spatial_irrigation": (IRRIGATE,),
    "tractor": (PREPARE, PLANT),
    "combine": (HARVEST, UNLOAD),
    "grain_facility": (DRY, STORE, INVENTORY),
    "multispectral_drone": (MULTISPECTRAL,),
    "thermal_drone": (THERMAL,),
    "ground_robot": (GROUND,),
    "charging_station": (CHARGE,),
}


@dataclass(frozen=True)
class SeasonProvider:
    """全季对象的身份与操作目录；不持有第二份引擎或物料状态。"""

    spec: ObjectSpec

    @property
    def operations(self) -> tuple[OperationDefinition, ...]:
        """按声明的提供者类型返回固定操作目录，不实例化引擎。"""
        return PROVIDERS[self.spec.type_id]
