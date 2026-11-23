"""Ground robot inspection operation."""

from fairy.acps.ontology.contracts import receipt_operation as operation
from fairy.acps.ontology.farm.observations import RANGE


GROUND = operation(
    "ground_inspect",
    "Ground robot visual stage and soil moisture inspection; consumes battery and time.",
    RANGE,
)
