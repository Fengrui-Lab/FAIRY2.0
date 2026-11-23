"""Public preparation rule for mounted equipment and local stock."""

from fairy.acps.ontology.contracts import JsonObject
from evaluate.experiments.historical.operations import Invoke, require


def provision_work(
    invoke: Invoke,
    operation: str,
    bounds: dict[str, int],
    *,
    inventory: JsonObject | None = None,
    fill_hopper: bool = False,
) -> None:
    """Select the required implement and provision from public balances only."""
    if inventory is None:
        inventory = require(invoke("inspect_inventory"))["receipt"]
    supply = inventory.get("equipment_supply")
    if supply is None:
        return
    tractor = supply["tractor_id"]
    machine = supply["combine_id"] if operation == "harvest" else tractor
    warehouse = supply["warehouse"]["object_id"]
    if operation != "harvest":
        capability = "seedbed" if operation == "prepare_seedbed" else "planting"
        target = next(
            i["object_id"]
            for i in supply["implements"]
            if i["capability"] == capability
        )
        mounted = supply["mounted_implement_id"]
        if mounted != target:
            if mounted is not None:
                require(invoke("unmount_implement", tractor_id=tractor))
            require(invoke("mount_implement", tractor_id=tractor, implement_id=target))
    n = bounds["end_ridge"] - bounds["start_ridge"] + 1
    hopper = supply["seed_hopper"]
    if operation == "plant":
        target_seeds = (
            hopper["capacity"] if fill_hopper else n * supply["seeds_per_ridge"]
        )
        amount = target_seeds - hopper["seeds"]
        if amount > 0:
            require(
                invoke(
                    "load_seeds",
                    warehouse_id=warehouse,
                    equipment_id=tractor,
                    seeds=amount,
                )
            )
    tank = supply["fuel_tanks"][machine]
    # Provision the tractor per job; preload the combine for a series of batches.
    fuel_needed = n * supply["fuel_per_ridge_l"][operation]
    if tank["fuel_l"] + 1e-9 < fuel_needed:
        target_l = tank["capacity_l"] if operation == "harvest" else fuel_needed
        amount_l = min(target_l - tank["fuel_l"], supply["warehouse"]["fuel_l"])
        if amount_l + tank["fuel_l"] + 1e-9 < fuel_needed:
            raise ValueError("Public fuel stocks cannot cover the requested work")
        require(
            invoke(
                "refuel_equipment",
                warehouse_id=warehouse,
                equipment_id=machine,
                litres=amount_l,
            )
        )
