"""Grain dryer specification, drying and maintenance operations."""

from dataclasses import dataclass

from fairy.acps.ontology.contracts import Parameter, receipt_operation as operation
from fairy.acps.ontology.material.grain import BATCH_ID


@dataclass(frozen=True)
class DryerSpec:
    """A dryer processes one lot on a trailer per scheduled job."""

    object_id: str
    capacity_kg: float
    repair_hours: float
    removal_kg_per_hour: float = 100.0
    energy_kwh_per_removed_kg: float = 0.8
    min_hours: float = 0.25

DRY = operation(
    "dry_grain",
    "Dry trailer grain to the requested wet-basis moisture fraction; removes water using electricity.",
    (Parameter("target_moisture", "number", 0.10, 0.135),),
)

DRY_BATCH = operation(
    "dry_batch",
    "Dry one whole named lot in trailer custody. Successful drying is complete when it returns. "
    "DRYER_CAPACITY_EXCEEDED means this lot cannot fit. Waiting, changing the target, or retrying "
    "does not make the lot fit. Check harvest_batch_limit before harvesting a new lot.",
    (
        BATCH_ID,
        Parameter("dryer_id", "string"),
        Parameter("target_moisture", "number", 0.10, 0.135),
    ),
)

INSPECT_DRYER = operation("inspect_dryer", "Read the modeled dryer's current availability and repair duration.", read=True)

REPAIR_DRYER = operation("repair_dryer", "Service the locked-out dryer. Advances time and leaves grain in place.")
