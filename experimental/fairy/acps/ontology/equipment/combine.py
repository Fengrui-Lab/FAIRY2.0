"""Combine harvesting and legacy single-load grain transfer."""

from dataclasses import replace

from fairy.acps.ontology.contracts import receipt_operation as operation
from fairy.acps.ontology.farm.observations import RANGE


HARVEST = operation(
    "harvest",
    "Harvest selected mature ridges into the combine. In named-batch scenarios, inspect_crop "
    "returns harvest_batch_limit for the first contiguous mature range it inspected. Use its "
    "inclusive start_ridge and end_ridge or a smaller range. A wet batch must fit the dryer, "
    "and every batch must fit the combine and trailer. On a harvest capacity rejection, reduce "
    "the ridge count or clear an occupied combine; waiting alone does not resolve the rejection. "
    "Field conditions and fuel are checked separately.",
    RANGE,
)

HARVEST = replace(HARVEST, version=3)

UNLOAD = operation(
    "unload", "Transfer all combine grain to the trailer; conserves dry grain and water."
)
