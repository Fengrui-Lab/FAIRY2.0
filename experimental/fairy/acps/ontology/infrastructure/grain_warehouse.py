"""Grain warehouse specification, intake and inventory operations."""

from dataclasses import dataclass

from fairy.acps.ontology.contracts import Parameter, receipt_operation as operation
from fairy.acps.ontology.material.grain import BATCH_ID


@dataclass(frozen=True)
class WarehouseSpec:
    """A warehouse occupying capacity by actual wet mass of stored lots."""

    object_id: str
    capacity_kg: float
    safe_moisture: float

STORE = operation(
    "store_grain",
    "Transfer trailer grain into safe storage; checks moisture and warehouse capacity.",
)

INVENTORY = operation(
    "inspect_inventory",
    "Read seed, fuel, equipment battery, grain containers and stored batches.",
    read=True,
)

STORE_BATCH = operation(
    "store_batch",
    "Move one safe lot from its trailer into the named warehouse.",
    (BATCH_ID, Parameter("warehouse_id", "string")),
)
