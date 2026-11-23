"""Charging station operation for electric equipment."""

from fairy.acps.ontology.contracts import Parameter, receipt_operation as operation


CHARGE = operation(
    "charge_equipment",
    "Charge a named electric platform (multispectral, thermal, or robot) from grid electricity.",
    (Parameter("equipment", "string"),),
)
