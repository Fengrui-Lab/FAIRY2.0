"""Observation of stand density and emergence."""

from dataclasses import replace

from fairy.acps.ontology.contracts import receipt_operation as operation
from fairy.acps.ontology.farm.observations import RANGE


OBSERVE_STAND = replace(
    operation(
        "observe_stand_range",
        "Observe emerged stand fraction and original/replanted stages. Stand is unavailable before emergence.",
        RANGE,
        True,
    ),
    required_backend_actions=("full_season", "stand_repair"),
)
