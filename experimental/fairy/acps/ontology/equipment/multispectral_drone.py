"""Multispectral drone survey operation."""

from fairy.acps.ontology.contracts import receipt_operation as operation
from fairy.acps.ontology.farm.observations import RANGE


MULTISPECTRAL = operation(
    "survey_multispectral",
    "Synthetic NDVI survey using the multispectral drone; consumes battery and time.",
    RANGE,
)
