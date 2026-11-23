"""One mounted implement and the partition of remaining seed and fuel by location."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, replace
from math import ceil
from typing import TypedDict, cast

from fairy.acps.backends.physical.contracts import PhysicalState, WorkPlan
from fairy.acps.ontology.contracts import JsonObject, OperationRejected
from fairy.acps.ontology.equipment.planter_implement import planter_implement
from fairy.acps.ontology.equipment.seedbed_implement import seedbed_implement
from fairy.acps.ontology.equipment.supply import (
    FUEL_PER_RIDGE,
    PLANTER_IMPLEMENT,
    SEEDBED_IMPLEMENT,
    SUPPLY_ACTIONS,
)
from fairy.acps.ontology.farm.spec import finite


class SupplyState(TypedDict):
    mounted: str | None
    onboard_seeds: int
    onboard_fuel: dict[str, float]


class EquipmentSupply:
    """Warehouse balances are derived from remaining totals minus onboard quantities."""

    seed_capacity = 500_000
    mount_hours = 0.25
    transfer_hours = 0.25

    def __init__(
        self, ids: dict[str, str], *, cultivar: str, seeds_per_ridge: int
    ) -> None:
        self.tractor = ids["tractor"]
        self.combine = ids["combine"]
        self.warehouse = ids["supply_warehouse"]
        self.implements = {
            ids[SEEDBED_IMPLEMENT]: seedbed_implement(ids[SEEDBED_IMPLEMENT]),
            ids[PLANTER_IMPLEMENT]: planter_implement(ids[PLANTER_IMPLEMENT]),
        }
        self.tank_capacity = {self.tractor: 90.0, self.combine: 75.0}
        self.cultivar = cultivar
        self.seeds_per_ridge = seeds_per_ridge
        self.state = self.initial_state()

    def initial_state(self) -> SupplyState:
        return dict(
            mounted=None,
            onboard_seeds=0,
            onboard_fuel=dict.fromkeys(self.tank_capacity, 0.0),
        )

    def remaining(self, totals: PhysicalState) -> JsonObject:
        return dict(
            seeds=totals["seeds"] - self.state["onboard_seeds"],
            fuel_l=max(
                0.0, totals["fuel_l"] - sum(self.state["onboard_fuel"].values())
            ),
        )

    def inventory(self, totals: PhysicalState) -> JsonObject:
        return {
            "warehouse": {"object_id": self.warehouse, **self.remaining(totals)},
            "tractor_id": self.tractor,
            "combine_id": self.combine,
            "implements": [asdict(item) for item in self.implements.values()],
            "mounted_implement_id": self.state["mounted"],
            "seed_hopper": {
                "cultivar": self.cultivar,
                "capacity": self.seed_capacity,
                "seeds": self.state["onboard_seeds"],
            },
            "fuel_tanks": {
                key: {"capacity_l": cap, "fuel_l": self.state["onboard_fuel"][key]}
                for key, cap in self.tank_capacity.items()
            },
            "seeds_per_ridge": self.seeds_per_ridge,
            "fuel_per_ridge_l": dict(FUEL_PER_RIDGE),
        }

    def work_check(self, operation: str, seeds: int, fuel: float) -> None:
        if operation not in FUEL_PER_RIDGE:
            return
        if operation != "harvest":
            mounted = self.implements.get(self.state["mounted"] or "")
            required = "seedbed" if operation == "prepare_seedbed" else "planting"
            if mounted is None or mounted.capability != required:
                raise OperationRejected("WRONG_IMPLEMENT")
        if seeds > self.state["onboard_seeds"]:
            raise OperationRejected("INSUFFICIENT_ONBOARD_SEEDS")
        if fuel > self.state["onboard_fuel"][self.machine(operation)] + 1e-9:
            raise OperationRejected("INSUFFICIENT_ONBOARD_FUEL")

    def machine(self, operation: str) -> str:
        return self.combine if operation == "harvest" else self.tractor

    def consume(self, operation: str, seeds: int, fuel: float) -> None:
        self.work_check(operation, seeds, fuel)
        if operation in FUEL_PER_RIDGE:
            machine = self.machine(operation)
            self.state["onboard_fuel"][machine] = max(
                0.0, self.state["onboard_fuel"][machine] - fuel
            )
            self.state["onboard_seeds"] -= seeds

    def plan(self, op: str, args: JsonObject, totals: PhysicalState) -> WorkPlan:
        state = self.state
        if op in ("mount_implement", "unmount_implement"):
            if args["tractor_id"] != self.tractor:
                raise OperationRejected("UNKNOWN_TRACTOR")
            if op == "mount_implement":
                if args["implement_id"] not in self.implements:
                    raise OperationRejected("UNKNOWN_IMPLEMENT")
                if state["mounted"] is not None:
                    raise OperationRejected("IMPLEMENT_SLOT_OCCUPIED")
            elif state["mounted"] is None:
                raise OperationRejected("NO_MOUNTED_IMPLEMENT")
            return dict(duration_hours=self.mount_hours, fuel_l=0.0)
        if op not in SUPPLY_ACTIONS:
            raise ValueError("Unexpected supply operation")
        if args["warehouse_id"] != self.warehouse:
            raise OperationRejected("UNKNOWN_SUPPLY_WAREHOUSE")
        machine = args["equipment_id"]
        loaded: float
        capacity: float
        if op in ("load_seeds", "return_seeds"):
            if machine != self.tractor:
                raise OperationRejected("EQUIPMENT_HAS_NO_SEED_HOPPER")
            amount = args["seeds"]
            stock, loaded, capacity = (
                self.remaining(totals)["seeds"],
                state["onboard_seeds"],
                self.seed_capacity,
            )
        else:
            if machine not in self.tank_capacity:
                raise OperationRejected("UNKNOWN_FUEL_TANK")
            amount = args["litres"]
            stock, loaded, capacity = (
                self.remaining(totals)["fuel_l"],
                state["onboard_fuel"][machine],
                self.tank_capacity[machine],
            )
        if op.startswith("return_"):
            if amount > loaded:
                raise OperationRejected("INSUFFICIENT_ONBOARD_STOCK")
        elif amount > capacity - loaded:
            raise OperationRejected("ONBOARD_CAPACITY_EXCEEDED")
        elif amount > stock:
            raise OperationRejected("INSUFFICIENT_WAREHOUSE_STOCK")
        return dict(duration_hours=self.transfer_hours, fuel_l=0.0)

    def execute(self, op: str, args: JsonObject, totals: PhysicalState) -> JsonObject:
        plan = self.plan(op, args, totals)
        before = self.inventory(totals)
        moved = None
        if op == "mount_implement":
            self.state["mounted"] = args["implement_id"]
        elif op == "unmount_implement":
            self.state["mounted"] = None
        elif op in ("load_seeds", "return_seeds"):
            moved = args["seeds"]
            self.state["onboard_seeds"] += moved if op == "load_seeds" else -moved
        else:
            moved = args["litres"]
            self.state["onboard_fuel"][args["equipment_id"]] += (
                moved if op == "refuel_equipment" else -moved
            )
        return {
            "operation": op,
            **plan,
            "before": before,
            "after": self.inventory(totals),
            "transferred": moved,
        }

    def decode(self, payload: JsonObject, totals: PhysicalState) -> SupplyState:
        data = deepcopy(payload)
        if set(data) != SupplyState.__required_keys__:
            raise ValueError("Incomplete equipment supply state")
        if data["mounted"] not in {None, *self.implements}:
            raise ValueError("Unknown mounted implement")
        if type(data["onboard_seeds"]) is not int or not 0 <= data[
            "onboard_seeds"
        ] <= min(self.seed_capacity, totals["seeds"]):
            raise ValueError("Invalid onboard seed balance")
        fuel = data["onboard_fuel"]
        if not isinstance(fuel, dict) or set(fuel) != set(self.tank_capacity):
            raise ValueError("Unknown machine fuel tank")
        for machine, capacity in self.tank_capacity.items():
            finite(fuel[machine], "onboard fuel", 0, capacity)
        if sum(fuel.values()) > totals["fuel_l"] + 1e-8:
            raise ValueError("Equipment fuel exceeds total remaining stock")
        return cast(SupplyState, data)
