"""Cross-entity supply catalog and fuel consumption for the season fixture."""

from fairy.acps.ontology.equipment.planter_implement import TYPE_ID as PLANTER_IMPLEMENT
from fairy.acps.ontology.equipment.seedbed_implement import TYPE_ID as SEEDBED_IMPLEMENT
from fairy.acps.ontology.equipment.tractor import MOUNT_OPERATIONS as MOUNT_OPERATIONS
from fairy.acps.ontology.infrastructure.supply_warehouse import TRANSFER_OPERATIONS


SUPPLY_OBJECTS = {
    SEEDBED_IMPLEMENT: (),
    PLANTER_IMPLEMENT: (),
    "supply_warehouse": TRANSFER_OPERATIONS,
}

SUPPLY_ACTIONS = frozenset(op.name for op in (*MOUNT_OPERATIONS, *TRANSFER_OPERATIONS))

FUEL_PER_RIDGE = {
    "prepare_seedbed": 0.6,
    "plant": 0.4,
    "harvest": 0.9,
    "replant_gaps": 0.4,
}
