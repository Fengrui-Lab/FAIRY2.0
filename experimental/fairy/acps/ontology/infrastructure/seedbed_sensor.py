"""Temperature and moisture observation for the seedbed."""

from dataclasses import replace

from fairy.acps.ontology.contracts import receipt_operation as operation
from fairy.acps.ontology.farm.observations import RANGE


SEEDBED = replace(
    operation(
        "observe_seedbed_range",
        "Observe simulated seedbed temperature and topsoil moisture. After the first "
        "settled local day, planting requires every selected ridge to be temperature-ready.",
        RANGE,
        True,
    ),
    required_backend_actions=("full_season", "seedbed_observation"),
)
