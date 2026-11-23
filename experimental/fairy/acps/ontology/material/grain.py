"""Grain identity and custody. All masses are actual kilograms."""

from typing import TypedDict

from fairy.acps.ontology.contracts import Parameter


class GrainLot(TypedDict):
    """A harvest lot whose identity survives unloading, drying and storage."""

    batch_id: str
    plot_id: str
    crop_id: str
    cultivar: str
    ridges: list[int]
    harvest_request_seq: int
    location_id: str
    dry_kg: float
    water_kg: float
    dried_by: str | None


BATCH_ID = Parameter("batch_id", "string")
