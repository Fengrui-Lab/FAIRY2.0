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


SEEDBED = replace(
    operation(
        "observe_seedbed_range",
        "Observe simulated seedbed temperature and topsoil moisture. After the first "
        "settled local day, planting requires every selected ridge to be temperature-ready.",
        RANGE,
        True,
    ),
    required_backend_actions=("full_season", "seedbed_observation"),
)

SURFACE_WATER = replace(
    operation(
        "observe_surface_water",
        "Observe standing water depth in mm and elapsed modeled crop waterlogging days.",
        RANGE,
        True,
    ),
    required_backend_actions=("full_season", "surface_drainage"),
)
DRAIN_SURFACE = replace(
    operation(
        "drain_surface",
        "Immediately remove available standing water up to the specified depth on each "
        "selected drainage-enabled ridge. No soil water is removed.",
        RANGE + (Parameter("max_depth_mm", "number", 0.01, 50, unit="mm"),),
        timing="immediate",
    ),
    required_backend_actions=("full_season", "surface_drainage"),
)
SCOUT_BIOTIC = replace(
    operation(
        "scout_biotic",
        "Read simulated insect feeding and disease pressure indices and protection days on selected ridges.",
        RANGE,
        True,
    ),
    required_backend_actions=("full_season", "biotic_management"),
)
TREAT_BIOTIC = replace(
    operation(
        "treat_biotic",
        "Queue simulated crop protection with kind insect or disease. Uses one inventory unit per ridge "
        "and settles at the next local midnight; rainfall can reduce its effect.",
        RANGE + (Parameter("kind", "string"),),
        timing="next_local_midnight",
    ),
    required_backend_actions=("full_season", "biotic_management"),
)

OBSERVE_STAND = replace(
    operation(
        "observe_stand_range",
        "Observe emerged stand fraction and original/replanted stages. Stand is unavailable before emergence.",
        RANGE,
        True,
    ),
    required_backend_actions=("full_season", "stand_repair"),
)
REPLANT = replace(
    operation(
        "replant_gaps",
        "Sow only unfilled seed sites in selected emerged vegetative ridges; preserves existing plants.",
        RANGE,
    ),
    required_backend_actions=("full_season", "stand_repair"),
)

INSPECT_DRYER = operation("inspect_dryer", "Read the modeled dryer's current availability and repair duration.", read=True)
REPAIR_DRYER = operation("repair_dryer", "Service the locked-out dryer. Advances time and leaves grain in place.")

SEASON_PROVIDERS = {
    **PROVIDERS,
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
