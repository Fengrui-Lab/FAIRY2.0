"""Named grain logistics on the existing serial physical backend."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, replace
from math import isclose
from typing import Iterable, cast

from fairy.acps.backends.biology.contracts import SeasonBiologyBackend, WeatherDay
from fairy.acps.backends.physical.contracts import DryerState, GrainBatch, WorkPlan
from fairy.acps.backends.physical.native.backend import NativePhysicalBackend
from fairy.acps.ontology.contracts import JsonObject, OperationRejected
from fairy.acps.ontology.equipment.trailer import TrailerSpec
from fairy.acps.ontology.farm.spec import FarmSpec, finite
from fairy.acps.ontology.infrastructure.dryer import DryerSpec
from fairy.acps.ontology.infrastructure.grain_warehouse import WarehouseSpec
from fairy.acps.ontology.material.grain import GrainLot

from evaluate.scenarios.season import SeasonSpec


class BatchPhysicalBackend(NativePhysicalBackend):
    """Keep one authoritative lot table; legacy totals are calculated projections."""

    capabilities = replace(
        NativePhysicalBackend.capabilities,
        model="serial_grain_batch_ledger",
        version="2",
        operations=tuple(
            op
            for op in NativePhysicalBackend.capabilities.operations
            if op not in ("unload", "dry_grain", "store_grain")
        )
        + ("unload_batch", "dry_batch", "store_batch"),
    )

    def __init__(self, config: SeasonSpec, farm: FarmSpec, ids: dict[str, str]) -> None:
        super().__init__(config, farm)
        self.combine_id = ids["combine"]
        self.trailer = TrailerSpec(ids["grain_trailer"], config.trailer_capacity_kg)
        self.dryer = DryerSpec(
            ids["grain_dryer"],
            1000.0,
            config.dryer_repair_hours,
        )
        self.warehouse = WarehouseSpec(
            ids["grain_warehouse"], config.storage_capacity_kg, config.safe_moisture
        )
        self.lots: dict[str, GrainLot] = {}

    def _at(self, location: str) -> list[GrainLot]:
        return [lot for lot in self.lots.values() if lot["location_id"] == location]

    def _load(self, location: str) -> GrainBatch:
        lots = self._at(location)
        return dict(
            dry_kg=sum((b["dry_kg"] for b in lots), 0.0),
            water_kg=sum((b["water_kg"] for b in lots), 0.0),
        )

    def _combine_load(self) -> GrainBatch:
        return self._load(self.combine_id)

    def _harvest_capacity_error(self, batch: GrainBatch) -> str | None:
        """Reject a new indivisible lot that cannot pass through required equipment."""
        error = super()._harvest_capacity_error(batch)
        if error:
            return error
        wet_kg = self.weight(batch)
        if wet_kg > self.trailer.capacity_kg + 1e-9:
            return "HARVEST_TRAILER_CAPACITY_EXCEEDED"
        if (
            self.moisture(batch) > self.warehouse.safe_moisture + 1e-9
            and wet_kg > self.dryer.capacity_kg + 1e-9
        ):
            return "HARVEST_DRYER_CAPACITY_EXCEEDED"
        return None

    def harvest_batch_limit(
        self, biology: SeasonBiologyBackend, observed_ridges: list[int]
    ) -> JsonObject:
        """Project a contiguous keepable prefix without publishing modeled grain mass."""
        prefix: list[int] = []
        end_ridge = None
        limit = None
        for ridge in observed_ridges:
            prefix.append(ridge)
            limit = self._harvest_capacity_error(
                self._project_recovered_grain(biology, prefix)
            )
            if limit == "NO_RECOVERABLE_GRAIN":
                # A zero-yield ridge may still fit when grouped with its neighbors.
                continue
            if limit:
                break
            end_ridge = ridge
        return {
            "start_ridge": observed_ridges[0],
            "end_ridge": end_ridge,
            "limiting_error": limit,
            "dryer_capacity_kg": self.dryer.capacity_kg,
        }

    def _deposit_harvest(
        self, batch: GrainBatch, ids: list[int], args: JsonObject
    ) -> JsonObject:
        seq = args["_harvest_request_seq"]
        key = f"grain-{seq}"
        if key in self.lots:
            raise ValueError("Duplicate harvest batch")
        lot: GrainLot = {
            "batch_id": key,
            "plot_id": self.farm.plot_id,
            "crop_id": "soybean",
            "cultivar": self.config.cultivar,
            "ridges": ids,
            "harvest_request_seq": seq,
            "location_id": self.combine_id,
            "dried_by": None,
            **batch,
        }
        self.lots[key] = lot
        return {"batch": deepcopy(lot)}

    def dryer_observation(self) -> JsonObject:
        state = self.state["dryer"]
        fault = state is not None and state["fault_active"]
        return {
            **asdict(self.dryer),
            "availability": "out_of_service" if fault else "ready",
            "fault": "BATCH_LOCKOUT" if fault else None,
        }

    def inventory(self) -> JsonObject:
        projected = self._projected_grain()
        return {
            **super().inventory(),
            **projected,
            "combine": {
                **projected["combine"],
                "moisture": self.moisture(projected["combine"]),
            },
            "trailer": {
                **projected["trailer"],
                "moisture": self.moisture(projected["trailer"]),
            },
            "trailer_facility": asdict(self.trailer),
            "dryer_facility": self.dryer_observation(),
            "warehouse_facility": {
                **asdict(self.warehouse),
                "remaining_kg": max(
                    0.0,
                    self.warehouse.capacity_kg
                    - self.weight(self._load(self.warehouse.object_id)),
                ),
            },
            "grain_batches": [
                {**lot, "moisture": self.moisture(lot)}
                for lot in deepcopy(list(self.lots.values()))
            ],
        }

    def _projected_grain(self) -> JsonObject:
        return dict(
            combine=self._combine_load(),
            trailer=self._load(self.trailer.object_id),
            stored_batches=[
                {
                    "dry_kg": lot["dry_kg"],
                    "water_kg": lot["water_kg"],
                    "moisture": self.moisture(lot),
                }
                for lot in self._at(self.warehouse.object_id)
            ],
        )

    def plan(
        self,
        op: str,
        args: JsonObject,
        biology: SeasonBiologyBackend,
        weather: WeatherDay,
    ) -> WorkPlan:
        if op not in ("unload_batch", "dry_batch", "store_batch"):
            if op in ("unload", "dry_grain", "store_grain"):
                raise OperationRejected("BATCH_ID_REQUIRED")
            return super().plan(op, args, biology, weather)
        lot = self.lots.get(args["batch_id"])
        if lot is None:
            raise OperationRejected("UNKNOWN_GRAIN_BATCH")
        source = self.combine_id if op == "unload_batch" else self.trailer.object_id
        if lot["location_id"] != source:
            raise OperationRejected("BATCH_NOT_AT_SOURCE")
        duration = 0.25
        if op == "unload_batch":
            if args["trailer_id"] != self.trailer.object_id:
                raise OperationRejected("UNKNOWN_TRAILER")
            if (
                self.weight(self._load(self.trailer.object_id)) + self.weight(lot)
                > self.trailer.capacity_kg + 1e-9
            ):
                raise OperationRejected("TRAILER_CAPACITY_EXCEEDED")
        elif op == "dry_batch":
            if args["dryer_id"] != self.dryer.object_id:
                raise OperationRejected("UNKNOWN_DRYER")
            if self.dryer_observation()["fault"]:
                raise OperationRejected("DRYER_OUT_OF_SERVICE")
            if self.weight(lot) > self.dryer.capacity_kg + 1e-9:
                raise OperationRejected("DRYER_CAPACITY_EXCEEDED")
            target = args["target_moisture"]
            if target > self.warehouse.safe_moisture:
                raise OperationRejected("UNSAFE_DRYING_TARGET")
            if self.moisture(lot) <= target + 1e-12:
                raise OperationRejected("DRYING_NOT_REQUIRED")
            duration = max(
                self.dryer.min_hours,
                self._water_removed(lot, target) / self.dryer.removal_kg_per_hour,
            )
        else:
            if args["warehouse_id"] != self.warehouse.object_id:
                raise OperationRejected("UNKNOWN_WAREHOUSE")
            if self.moisture(lot) > self.warehouse.safe_moisture + 1e-9:
                raise OperationRejected("UNSAFE_STORAGE_MOISTURE")
            if (
                self.weight(self._load(self.warehouse.object_id)) + self.weight(lot)
                > self.warehouse.capacity_kg + 1e-9
            ):
                raise OperationRejected("STORAGE_CAPACITY_EXCEEDED")
        return dict(duration_hours=duration, fuel_l=0.0)

    @staticmethod
    def _water_removed(lot: GrainLot, target: float) -> float:
        return lot["water_kg"] - lot["dry_kg"] * target / (1 - target)

    def execute(
        self,
        op: str,
        args: JsonObject,
        biology: SeasonBiologyBackend,
        weather: WeatherDay,
    ) -> JsonObject:
        if op not in ("unload_batch", "dry_batch", "store_batch"):
            return super().execute(op, args, biology, weather)
        plan = self.plan(op, args, biology, weather)
        lot = self.lots[args["batch_id"]]
        result = {"operation": op, **plan, "before": deepcopy(lot)}
        if op == "unload_batch":
            lot["location_id"] = self.trailer.object_id
        elif op == "store_batch":
            lot["location_id"] = self.warehouse.object_id
            dryer = self.state["dryer"]
            self._count_stored_lot(dryer, lot)
            result["warehouse_remaining_kg"] = self.inventory()["warehouse_facility"][
                "remaining_kg"
            ]
        else:
            removed = self._water_removed(lot, args["target_moisture"])
            lot["water_kg"] -= removed
            lot["dried_by"] = self.dryer.object_id
            energy = removed * self.dryer.energy_kwh_per_removed_kg
            self.state["removed_water_kg"] += removed
            self.state["electricity_kwh"] += energy
            result.update(
                dryer_id=self.dryer.object_id,
                removed_water_kg=removed,
                electricity_kwh=energy,
            )
        return {**result, "batch": deepcopy(lot)}

    def _count_stored_lot(self, dryer: DryerState | None, lot: GrainLot) -> None:
        if dryer is not None and lot["dried_by"] == self.dryer.object_id:
            dryer["dried_batches_stored"] += 1
            if (
                dryer["dried_batches_stored"]
                == self.config.dryer_failure_after_dried_batches
            ):
                dryer["fault_active"] = True

    def audit(self) -> JsonObject:
        projected = self._projected_grain()
        return {
            **super().audit(),
            **projected,
            "grain_batches": deepcopy(self.lots),
            "dry_mass_balance_error_kg": self.state["harvested_dry_kg"]
            - sum(b["dry_kg"] for b in self.lots.values()),
            "water_mass_balance_error_kg": self.state["harvested_water_kg"]
            - self.state["removed_water_kg"]
            - sum(b["water_kg"] for b in self.lots.values()),
        }

    def export_state(self) -> JsonObject:
        base = super().export_state()
        for key in ("combine", "trailer", "stored_batches"):
            del base["state"][key]
        return {
            **base,
            "schema": 3,
            "grain_batches": deepcopy(list(self.lots.values())),
        }

    def load_state(self, payload: JsonObject) -> None:
        if payload.get("schema") != 3 or "grain_batches" not in payload:
            raise ValueError("Incompatible batch physical state")
        lots: dict[str, GrainLot] = {}
        used_ridges: set[int] = set()
        last_seq = 0
        locations = {self.combine_id, self.trailer.object_id, self.warehouse.object_id}
        raw_lots = payload["grain_batches"]
        if not isinstance(raw_lots, list) or set(payload["state"]) != set(
            self.state
        ) - {"combine", "trailer", "stored_batches"}:
            raise ValueError("Invalid batch ledger or duplicate aggregate state")
        for row in deepcopy(raw_lots):
            if not isinstance(row, dict) or set(row) != GrainLot.__required_keys__:
                raise ValueError("Invalid grain batch fields")
            lot = cast(GrainLot, row)
            seq = lot["harvest_request_seq"]
            ridges = lot["ridges"]
            if (
                type(seq) is not int
                or seq <= last_seq
                or lot["batch_id"] != f"grain-{seq}"
                or lot["location_id"] not in locations
                or lot["plot_id"] != self.farm.plot_id
                or lot["crop_id"] != "soybean"
                or lot["cultivar"] != self.config.cultivar
                or lot["dried_by"] not in (None, self.dryer.object_id)
                or (
                    lot["location_id"] == self.combine_id
                    and lot["dried_by"] is not None
                )
                or not isinstance(ridges, list)
                or not ridges
                or any(
                    type(i) is not int or not 0 <= i < self.config.ridge_count
                    for i in ridges
                )
                or ridges != list(range(ridges[0], ridges[-1] + 1))
                or used_ridges.intersection(ridges)
            ):
                raise ValueError("Invalid grain batch identity, provenance or custody")
            finite(lot["dry_kg"], "batch dry mass", 0)
            finite(lot["water_kg"], "batch water mass", 0)
            if self.weight(lot) <= 0:
                raise ValueError("Empty grain batch")
            lots[lot["batch_id"]] = lot
            last_seq = seq
            used_ridges.update(ridges)
        previous = self.lots
        self.lots = lots
        try:
            super().load_state(
                {
                    **payload,
                    "schema": 2,
                    "state": {**payload["state"], **self._projected_grain()},
                }
            )
        except Exception:
            self.lots = previous
            raise
        # The superclass restores aggregate slots only for its legacy mode; here they remain empty.
        self.state["combine"] = dict(dry_kg=0.0, water_kg=0.0)
        self.state["trailer"] = dict(dry_kg=0.0, water_kg=0.0)
        self.state["stored_batches"] = []

    def validate_grain_history(self, operations: Iterable[JsonObject]) -> None:
        lots: dict[str, GrainLot] = {}
        dryer = self._initial_dryer()
        total_removed = 0.0
        for event in operations:
            op, receipt, args, seq = (
                event[k] for k in ("operation", "receipt", "arguments", "request_seq")
            )
            if op == "harvest":
                lot = receipt["batch"]
                key = f"grain-{seq}"
                if (
                    lot["batch_id"] != key
                    or lot["harvest_request_seq"] != seq
                    or lot["location_id"] != self.combine_id
                    or lot["dried_by"] is not None
                    or lot["ridges"]
                    != list(range(args["start_ridge"], args["end_ridge"] + 1))
                    or any(lot[k] != receipt[k] for k in ("dry_kg", "water_kg"))
                ):
                    raise ValueError("Harvest batch disagrees with its source event")
                lots[key] = deepcopy(lot)
            elif op in ("unload_batch", "dry_batch", "store_batch"):
                key = args["batch_id"]
                expected = deepcopy(lots.get(key))
                if expected is None or expected != receipt["before"]:
                    raise ValueError("Batch operation disagrees with preceding custody")
                source = (
                    self.combine_id if op == "unload_batch" else self.trailer.object_id
                )
                if expected["location_id"] != source:
                    raise ValueError("Batch operation has the wrong source")
                if op == "unload_batch":
                    expected["location_id"] = self.trailer.object_id
                elif op == "store_batch":
                    expected["location_id"] = self.warehouse.object_id
                    self._count_stored_lot(dryer, expected)
                else:
                    if dryer is not None and dryer["fault_active"]:
                        raise ValueError("Batch dried while dryer was locked out")
                    removed = self._water_removed(expected, args["target_moisture"])
                    expected["water_kg"] -= removed
                    expected["dried_by"] = self.dryer.object_id
                    total_removed += removed
                if expected != receipt["batch"]:
                    raise ValueError(
                        "Batch operation disagrees with expected transition"
                    )
                lots[key] = expected
            elif op == "repair_dryer" and dryer is not None:
                self._update_dryer(dryer, op)
        if (
            lots != self.lots
            or dryer != self.state["dryer"]
            or not isclose(total_removed, self.state["removed_water_kg"], abs_tol=1e-8)
        ):
            raise ValueError(
                "Physical grain state disagrees with completed batch events"
            )
