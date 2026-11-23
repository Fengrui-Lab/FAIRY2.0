"""Public crop and soil observations for seasonal plots."""

from fairy.acps.ontology.contracts import receipt_operation as operation
from fairy.acps.ontology.farm.observations import RANGE


FIELD = operation(
    "inspect_crop",
    "Read visual crop stages and grain moisture for selected ridges. Named-batch scenarios include "
    "harvest_batch_limit when mature ridges are present. Its inclusive start and end define a "
    "contiguous range that fits the equipment at observation time; null end means no range fits. "
    "Re-read after each harvest to size the next batch.",
    RANGE,
    True,
)

SOIL = operation(
    "observe_soil_range",
    "Read rounded root-zone volumetric moisture for selected ridges; no time passes.",
    RANGE,
    True,
)
