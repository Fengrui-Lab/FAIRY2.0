"""Thermal drone survey operation."""

from fairy.acps.ontology.contracts import receipt_operation as operation
from fairy.acps.ontology.farm.observations import RANGE


THERMAL = operation(
    "survey_thermal",
    "Synthetic canopy-air temperature anomaly using the thermal drone; consumes battery and time.",
    RANGE,
)
