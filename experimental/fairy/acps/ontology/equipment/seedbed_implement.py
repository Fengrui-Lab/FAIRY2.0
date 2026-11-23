"""Passive implement for seedbed preparation."""

from fairy.acps.ontology.equipment.implement import ImplementSpec


TYPE_ID = "seedbed_implement"


def seedbed_implement(object_id: str) -> ImplementSpec:
    """Describe the named implement for seedbed work."""
    return ImplementSpec(object_id, "seedbed")
