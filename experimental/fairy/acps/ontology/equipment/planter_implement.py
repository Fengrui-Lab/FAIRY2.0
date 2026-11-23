"""Passive implement for sowing and gap repair."""

from fairy.acps.ontology.equipment.implement import ImplementSpec


TYPE_ID = "planter_implement"


def planter_implement(object_id: str) -> ImplementSpec:
    """Describe the named implement for planting work."""
    return ImplementSpec(object_id, "planting")
