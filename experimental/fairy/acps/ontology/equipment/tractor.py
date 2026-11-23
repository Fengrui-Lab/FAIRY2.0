"""Tractor seedbed preparation, planting, implement changes and stand repair."""

from dataclasses import replace

from fairy.acps.ontology.contracts import Parameter, receipt_operation as operation
from fairy.acps.ontology.farm.observations import RANGE


PREPARE = operation(
    "prepare_seedbed", "Prepare unplanted ridges using tractor fuel and time.", RANGE
)

PLANT = operation(
    "plant",
    "Sow HEINONG58 on prepared ridges using seed inventory. Spacing is in metres and depth in mm, two rows per ridge.",
    RANGE
    + (
        Parameter("spacing_m", "number", 0.01, 0.5, unit="m"),
        Parameter("depth_mm", "number", 10, 100, unit="mm"),
    ),
)

PLANT = replace(PLANT, version=2)  # distinct public rejection reasons

REPLANT = replace(
    operation(
        "replant_gaps",
        "Sow only unfilled seed sites in selected emerged vegetative ridges; preserves existing plants.",
        RANGE,
    ),
    required_backend_actions=("full_season", "stand_repair"),
)

TRACTOR_ID = Parameter("tractor_id", "string")

IMPLEMENT_ID = Parameter("implement_id", "string")

MOUNT = operation(
    "mount_implement",
    "Mount a named implement on an empty tractor slot.",
    (TRACTOR_ID, IMPLEMENT_ID),
)

UNMOUNT = operation(
    "unmount_implement", "Release the tractor's mounted implement.", (TRACTOR_ID,)
)

MOUNT_OPERATIONS = (MOUNT, UNMOUNT)
