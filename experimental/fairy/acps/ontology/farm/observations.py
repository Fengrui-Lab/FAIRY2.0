"""Synthetic field-observation contracts for the initial 64-ridge fixture."""

from fairy.acps.ontology.contracts import Parameter, receipt_operation as operation

RANGE = (Parameter("start_ridge", "integer", 0, 63), Parameter("end_ridge", "integer", 0, 63))

FIELD = operation(
    "inspect_crop",
    "Read synthetic visual crop stages and grain moisture for selected ridges. No potential yield is exposed.",
    RANGE,
    True,
)
SOIL = operation(
    "observe_soil_range",
    "Read rounded root-zone volumetric moisture for selected ridges; no time passes.",
    RANGE,
    True,
)
