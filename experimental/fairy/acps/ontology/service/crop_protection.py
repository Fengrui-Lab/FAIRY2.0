"""Crop protection scouting and treatment contracts."""

from dataclasses import replace

from fairy.acps.ontology.contracts import Parameter, receipt_operation as operation
from fairy.acps.ontology.farm.observations import RANGE


SCOUT_BIOTIC = replace(
    operation(
        "scout_biotic",
        "Read simulated insect feeding and disease pressure indices and protection days on selected ridges.",
        RANGE,
        True,
    ),
    required_backend_actions=("full_season", "biotic_management"),
)

TREAT_BIOTIC = replace(
    operation(
        "treat_biotic",
        "Queue simulated crop protection with kind insect or disease. Uses one inventory unit per ridge "
        "and settles at the next local midnight; rainfall can reduce its effect.",
        RANGE + (Parameter("kind", "string"),),
        timing="next_local_midnight",
    ),
    required_backend_actions=("full_season", "biotic_management"),
)
