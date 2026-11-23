"""Shared ridge range arguments for field operations."""

from fairy.acps.ontology.contracts import Parameter


RANGE = (Parameter("start_ridge", "integer", 0, 63), Parameter("end_ridge", "integer", 0, 63))
