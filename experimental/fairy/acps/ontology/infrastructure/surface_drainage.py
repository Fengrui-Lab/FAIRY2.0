"""Surface drainage observation and water removal."""

from dataclasses import replace

from fairy.acps.ontology.contracts import Parameter, receipt_operation as operation
from fairy.acps.ontology.farm.observations import RANGE


SURFACE_WATER = replace(
    operation(
        "observe_surface_water",
        "Observe standing water depth in mm and elapsed modeled crop waterlogging days.",
        RANGE,
        True,
    ),
    required_backend_actions=("full_season", "surface_drainage"),
)

DRAIN_SURFACE = replace(
    operation(
        "drain_surface",
        "Immediately remove available standing water up to the specified depth on each "
        "selected drainage-enabled ridge. No soil water is removed.",
        RANGE + (Parameter("max_depth_mm", "number", 0.01, 50, unit="mm"),),
        timing="immediate",
    ),
    required_backend_actions=("full_season", "surface_drainage"),
)
