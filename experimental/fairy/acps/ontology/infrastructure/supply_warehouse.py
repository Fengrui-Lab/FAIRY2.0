"""Supply warehouse transfers of seeds and fuel to and from equipment."""

from fairy.acps.ontology.contracts import Parameter, receipt_operation as operation


EQUIPMENT_ID = Parameter("equipment_id", "string")

WAREHOUSE_ID = Parameter("warehouse_id", "string")

LITRES = Parameter("litres", "number", exclusive_minimum=0, unit="L")

SEEDS = Parameter("seeds", "integer", 1, unit="seeds")

LOAD_SEEDS = operation(
    "load_seeds",
    "Move seeds from warehouse to the tractor seed hopper.",
    (WAREHOUSE_ID, EQUIPMENT_ID, SEEDS),
)

RETURN_SEEDS = operation(
    "return_seeds",
    "Return unused seeds from the tractor to the warehouse.",
    (WAREHOUSE_ID, EQUIPMENT_ID, SEEDS),
)

REFUEL = operation(
    "refuel_equipment",
    "Transfer fuel from warehouse to a named machine tank.",
    (WAREHOUSE_ID, EQUIPMENT_ID, LITRES),
)

RETURN_FUEL = operation(
    "return_fuel",
    "Return fuel from a named machine tank to the warehouse.",
    (WAREHOUSE_ID, EQUIPMENT_ID, LITRES),
)

TRANSFER_OPERATIONS = (LOAD_SEEDS, RETURN_SEEDS, REFUEL, RETURN_FUEL)
