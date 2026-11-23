"""Explicit season provider catalog assembled from individual ontology entities."""

from __future__ import annotations

from dataclasses import dataclass

from fairy.acps.ontology.contracts import ObjectSpec, OperationDefinition
from fairy.acps.ontology.equipment.combine import HARVEST, UNLOAD
from fairy.acps.ontology.equipment.ground_robot import GROUND
from fairy.acps.ontology.equipment.multispectral_drone import MULTISPECTRAL
from fairy.acps.ontology.equipment.supply import SUPPLY_OBJECTS
from fairy.acps.ontology.equipment.thermal_drone import THERMAL
from fairy.acps.ontology.equipment.tractor import MOUNT_OPERATIONS, PLANT, PREPARE, REPLANT
from fairy.acps.ontology.equipment.trailer import UNLOAD_BATCH
from fairy.acps.ontology.infrastructure.charging_station import CHARGE
from fairy.acps.ontology.infrastructure.crop_sensor import FIELD, SOIL
from fairy.acps.ontology.infrastructure.dryer import DRY, DRY_BATCH, INSPECT_DRYER, REPAIR_DRYER
from fairy.acps.ontology.infrastructure.grain_warehouse import INVENTORY, STORE, STORE_BATCH
from fairy.acps.ontology.infrastructure.irrigation import IRRIGATE_RANGE
from fairy.acps.ontology.infrastructure.seedbed_sensor import SEEDBED
from fairy.acps.ontology.infrastructure.stand_sensor import OBSERVE_STAND
from fairy.acps.ontology.infrastructure.surface_drainage import DRAIN_SURFACE, SURFACE_WATER
from fairy.acps.ontology.service.crop_protection import SCOUT_BIOTIC, TREAT_BIOTIC


PROVIDERS = {
    "crop_sensor": (FIELD, SOIL),
    "spatial_irrigation": (IRRIGATE_RANGE,),
    "tractor": (PREPARE, PLANT),
    "combine": (HARVEST, UNLOAD),
    "grain_facility": (DRY, STORE, INVENTORY),
    "multispectral_drone": (MULTISPECTRAL,),
    "thermal_drone": (THERMAL,),
    "ground_robot": (GROUND,),
    "charging_station": (CHARGE,),
}

GRAIN_PROVIDERS = {
    "grain_trailer": (UNLOAD_BATCH,),
    "grain_dryer": (DRY_BATCH, INSPECT_DRYER, REPAIR_DRYER),
    "grain_warehouse": (STORE_BATCH, INVENTORY),
}

SEASON_PROVIDERS = {
    **SUPPLY_OBJECTS,
    **GRAIN_PROVIDERS,
    **PROVIDERS,
    "tractor": (*PROVIDERS["tractor"], *MOUNT_OPERATIONS),
    "seedbed_sensor": (SEEDBED,),
    "surface_drainage": (SURFACE_WATER, DRAIN_SURFACE),
    "crop_protection": (SCOUT_BIOTIC, TREAT_BIOTIC),
    "stand_sensor": (OBSERVE_STAND,),
    "stand_repair_tractor": (REPLANT,),
    "dryer_maintenance": (INSPECT_DRYER, REPAIR_DRYER),
}

@dataclass(frozen=True)
class SeasonProvider:
    """全季对象的身份与操作目录；不持有第二份引擎或物料状态。"""

    spec: ObjectSpec

    @property
    def operations(self) -> tuple[OperationDefinition, ...]:
        """按声明的提供者类型返回固定操作目录，不实例化引擎。"""
        return SEASON_PROVIDERS[self.spec.type_id]
