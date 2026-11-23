"""Passive grain carrier with an independent wet load limit."""

from dataclasses import dataclass

from fairy.acps.ontology.contracts import Parameter, receipt_operation as operation
from fairy.acps.ontology.material.grain import BATCH_ID


@dataclass(frozen=True)
class TrailerSpec:
    object_id: str
    capacity_kg: float


UNLOAD_BATCH = operation(
    "unload_batch",
    "Transfer one combine lot to the named trailer.",
    (BATCH_ID, Parameter("trailer_id", "string")),
)
