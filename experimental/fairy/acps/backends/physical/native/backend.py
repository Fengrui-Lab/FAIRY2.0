"""Transactional equipment and dry-grain/water ledger, driven by Runtime time."""

from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from evaluate.scenarios.season import SeasonSpec
    from fairy.acps.backends.biology.contracts import SeasonBiologyBackend, WeatherDay
    from fairy.acps.backends.physical.contracts import GrainBatch, WorkPlan
    from fairy.acps.ontology.contracts import JsonObject
    from fairy.acps.ontology.farm.spec import FarmSpec
from fairy.acps.backends.physical.contracts import (
    PhysicalBackend,
    PhysicalState,
    COMPONENTS,
    CONTAINERS,
)
from fairy.acps.ontology.contracts import JsonObject
from dataclasses import asdict
import copy
from math import ceil
from fairy.acps.backends.physical.contracts import PhysicalCapabilities
from fairy.acps.ontology.contracts import OperationRejected


class NativePhysicalBackend(PhysicalBackend):
    """串行装备作业与唯一物料账本。

    plan不改状态，execute在Runtime推进至完成时刻后复检并提交；
    粮食干质量与水质量分别保存，机械回收不改变生物引擎的生长方程。"""

    state: PhysicalState  #: 唯一物料/装备真值，干粮kg与水kg分开。
    config: SeasonSpec  #: 作业容量、初始库存和机械回收工程假设。
    farm: FarmSpec  #: 种子数和作业覆盖所依赖的固定几何。

    capabilities = PhysicalCapabilities(
        "native",
        "serial_equipment_material_ledger",
        "2",
        (
            "prepare_seedbed",
            "plant",
            "harvest",
            "unload",
            "dry_grain",
            "store_grain",
            "survey_multispectral",
            "survey_thermal",
            "ground_inspect",
            "charge_equipment",
        ),
    )

    def __init__(self, config: SeasonSpec, farm: FarmSpec) -> None:
        """保存显式依赖与初态；模型服务调用发生在决策阶段而非构造阶段。"""
        self.config, self.farm = config, farm
        self.state: PhysicalState = dict(
            prepared=[False] * config.ridge_count,
            seeds=config.initial_seed_count,
            seeds_used=0,
            fuel_l=config.initial_fuel_l,
            fuel_used_l=0.0,
            electricity_kwh=0.0,
            battery={"multispectral": 100.0, "thermal": 100.0, "robot": 100.0},
            combine=dict(dry_kg=0.0, water_kg=0.0),
            trailer=dict(dry_kg=0.0, water_kg=0.0),
            stored_batches=[],
            harvested_dry_kg=0.0,
            harvested_water_kg=0.0,
            harvest_loss_dry_kg=0.0,
            harvest_loss_water_kg=0.0,
            removed_water_kg=0.0,
        )

    @staticmethod
    def weight(batch: GrainBatch) -> float:
        """计算批次总湿质量kg，即干粮质量加水质量。"""
        return batch["dry_kg"] + batch["water_kg"]

    @classmethod
    def moisture(cls, batch: GrainBatch) -> float:
        """计算湿基含水率water/(dry+water)；空容器返回0。"""
        return batch["water_kg"] / cls.weight(batch) if cls.weight(batch) > 0 else 0.0

    def inventory(self) -> JsonObject:
        """返回公开库存/容器回执，不执行作业或推进时间。"""
        s = self.state
        return dict(
            seeds=s["seeds"],
            fuel_l=round(s["fuel_l"], 3),
            battery=copy.deepcopy(s["battery"]),
            combine={**s["combine"], "moisture": self.moisture(s["combine"])},
            trailer={**s["trailer"], "moisture": self.moisture(s["trailer"])},
            stored_batches=copy.deepcopy(s["stored_batches"]),
            electricity_kwh=s["electricity_kwh"],
            prepared_ridges=[i for i, v in enumerate(s["prepared"]) if v],
            combine_capacity_kg=self.config.combine_capacity_kg,
            trailer_capacity_kg=self.config.trailer_capacity_kg,
            storage_capacity_kg=self.config.storage_capacity_kg,
        )

    def plan(
        self, op: str, args: JsonObject, biology: SeasonBiologyBackend, weather: WeatherDay
    ) -> WorkPlan:
        """Pure preflight. The runtime advances the clock, then rechecks at completion."""
        s, c = self.state, self.config
        ids = list(biology.indices(args)) if "start_ridge" in args else []
        n = len(ids)
        duration, fuel = 0.0, 0.0
        if op in ("prepare_seedbed", "plant", "harvest"):
            if weather.rain_mm > 10 or any(
                biology.management_state()[i].root_vwc > 0.36 for i in ids
            ):
                raise OperationRejected("FIELD_NOT_TRAFFICABLE")
            duration = n * ({"prepare_seedbed": 0.08, "plant": 0.06, "harvest": 0.10}[op])
            fuel = n * ({"prepare_seedbed": 0.6, "plant": 0.4, "harvest": 0.9}[op])
            if s["fuel_l"] + 1e-9 < fuel:
                raise OperationRejected("INSUFFICIENT_FUEL")
        if op == "prepare_seedbed":
            if any(s["prepared"][i] or biology.management_state()[i].planted for i in ids):
                raise OperationRejected("RIDGE_ALREADY_PREPARED_OR_PLANTED")
        elif op == "plant":
            if (
                abs(args["spacing_m"] - c.seed_spacing_cm / 100) > 1e-9
                or abs(args["depth_mm"] - c.sowing_depth_cm * 10) > 1e-9
            ):
                raise OperationRejected("PLANTING_CONTRACT_MISMATCH")
            if any(biology.management_state()[i].planted for i in ids):
                raise OperationRejected("RIDGE_ALREADY_PLANTED")
            if any(not s["prepared"][i] for i in ids):
                raise OperationRejected("SEEDBED_NOT_PREPARED")
            needed = n * ceil(2 * self.farm.length_m / args["spacing_m"])
            if needed > s["seeds"]:
                raise OperationRejected("INSUFFICIENT_SEEDS")
        elif op == "harvest":
            if any(not biology.management_state()[i].harvestable for i in ids):
                raise OperationRejected("CROP_NOT_HARVESTABLE")
            gross = biology.project_harvest(ids)
            incoming = (
                gross["gross_dry_kg"] + gross["gross_water_kg"]
            ) * c.harvest_recovery_fraction
            if incoming <= 0:
                raise OperationRejected("NO_RECOVERABLE_GRAIN")
            if self.weight(s["combine"]) + incoming > c.combine_capacity_kg + 1e-9:
                raise OperationRejected("COMBINE_CAPACITY_EXCEEDED")
        elif op == "unload":
            if self.weight(s["combine"]) <= 0:
                raise OperationRejected("COMBINE_EMPTY")
            if self.weight(s["combine"]) + self.weight(s["trailer"]) > c.trailer_capacity_kg + 1e-9:
                raise OperationRejected("TRAILER_CAPACITY_EXCEEDED")
            duration = 0.25
        elif op == "dry_grain":
            if self.weight(s["trailer"]) <= 0:
                raise OperationRejected("TRAILER_EMPTY")
            if self.moisture(s["trailer"]) <= args["target_moisture"]:
                raise OperationRejected("DRYING_NOT_REQUIRED")
            duration = max(
                0.25,
                (
                    s["trailer"]["water_kg"]
                    - s["trailer"]["dry_kg"]
                    * args["target_moisture"]
                    / (1 - args["target_moisture"])
                )
                / 100,
            )
        elif op == "store_grain":
            if self.weight(s["trailer"]) <= 0:
                raise OperationRejected("TRAILER_EMPTY")
            if self.moisture(s["trailer"]) > c.safe_moisture + 1e-9:
                raise OperationRejected("UNSAFE_STORAGE_MOISTURE")
            if (
                sum(self.weight(b) for b in s["stored_batches"]) + self.weight(s["trailer"])
                > c.storage_capacity_kg
            ):
                raise OperationRejected("STORAGE_CAPACITY_EXCEEDED")
            duration = 0.25
        elif op in ("survey_multispectral", "survey_thermal", "ground_inspect"):
            device = {
                "survey_multispectral": "multispectral",
                "survey_thermal": "thermal",
                "ground_inspect": "robot",
            }[op]
            cost = n * (1.0 if device != "robot" else 1.2)
            if s["battery"][device] + 1e-9 < cost:
                raise OperationRejected("INSUFFICIENT_BATTERY")
            if device != "robot" and (weather.rain_mm > 10 or weather.wind_ms > 8):
                raise OperationRejected("UNSAFE_FLIGHT_WEATHER")
            duration = n * (0.01 if device != "robot" else 0.04)
        elif op == "charge_equipment":
            if args["equipment"] not in s["battery"]:
                raise OperationRejected("UNKNOWN_EQUIPMENT")
            if s["battery"][args["equipment"]] >= 100:
                raise OperationRejected("BATTERY_FULL")
            duration = 0.75
        else:
            raise OperationRejected("UNSUPPORTED_PHYSICAL_OPERATION")
        return dict(duration_hours=duration, fuel_l=fuel)

    def execute(
        self, op: str, args: JsonObject, biology: SeasonBiologyBackend, weather: WeatherDay
    ) -> JsonObject:
        """在完成时复检并提交物理/物料变化；时钟推进和失败回滚归Runtime。"""
        plan = self.plan(op, args, biology, weather)
        s, c = self.state, self.config
        ids = list(biology.indices(args)) if "start_ridge" in args else []
        # Preflight tolerates floating-point roundoff at an exhausted budget.
        # Do not persist a tiny negative quantity that restore must then reject.
        s["fuel_l"] = max(0.0, s["fuel_l"] - plan["fuel_l"])
        s["fuel_used_l"] = c.initial_fuel_l - s["fuel_l"]
        result = {"operation": op, **plan}
        if op == "prepare_seedbed":
            for i in ids:
                s["prepared"][i] = True
        elif op == "plant":
            used = len(ids) * ceil(2 * self.farm.length_m / args["spacing_m"])
            s["seeds"] -= used
            s["seeds_used"] += used
            biology.plant(ids, 2 / (self.farm.width_m / c.ridge_count) / args["spacing_m"])
            result["seeds_used"] = used
        elif op == "harvest":
            gross = biology.harvest(ids)
            batch: GrainBatch = {
                "dry_kg": gross["gross_dry_kg"] * c.harvest_recovery_fraction,
                "water_kg": gross["gross_water_kg"] * c.harvest_recovery_fraction,
            }
            for key in COMPONENTS:
                s["combine"][key] += batch[key]
            s["harvested_dry_kg"] += batch["dry_kg"]
            s["harvested_water_kg"] += batch["water_kg"]
            s["harvest_loss_dry_kg"] += gross["gross_dry_kg"] - batch["dry_kg"]
            s["harvest_loss_water_kg"] += gross["gross_water_kg"] - batch["water_kg"]
            result.update({k: batch[k] for k in COMPONENTS})
        elif op == "unload":
            for key in COMPONENTS:
                s["trailer"][key] += s["combine"][key]
                s["combine"][key] = 0.0
        elif op == "dry_grain":
            final = s["trailer"]["dry_kg"] * args["target_moisture"] / (1 - args["target_moisture"])
            removed = s["trailer"]["water_kg"] - final
            s["removed_water_kg"] += removed
            s["electricity_kwh"] += removed * 0.8
            s["trailer"]["water_kg"] = final
            result["removed_water_kg"] = removed
        elif op == "store_grain":
            s["stored_batches"].append({**s["trailer"], "moisture": self.moisture(s["trailer"])})
            s["trailer"] = {"dry_kg": 0.0, "water_kg": 0.0}
        elif op in ("survey_multispectral", "survey_thermal", "ground_inspect"):
            device = {
                "survey_multispectral": "multispectral",
                "survey_thermal": "thermal",
                "ground_inspect": "robot",
            }[op]
            s["battery"][device] = max(
                0.0, s["battery"][device] - len(ids) * (1.0 if device != "robot" else 1.2)
            )
            result["observations"] = (
                biology.field_observation(ids)
                if device == "robot"
                else biology.survey(ids, device == "thermal")
            )
            if device == "robot":
                result["soil"] = biology.soil_observation(ids)
            result["sensor_semantics"] = (
                "deterministic synthetic observation; not calibrated instrument output"
            )
        elif op == "charge_equipment":
            device = args["equipment"]
            s["electricity_kwh"] += (100 - s["battery"][device]) * 0.01
            s["battery"][device] = 100.0
        if ids:
            result["completed_ridges"] = ids
        return result

    def export_state(self) -> JsonObject:
        """导出可信恢复快照；此数据不是LLM公开观测。"""
        return dict(
            schema=1, capabilities=asdict(self.capabilities), state=copy.deepcopy(self.state)
        )

    def load_state(self, payload: JsonObject) -> None:
        """验证版本与守恒后恢复内部状态；调用方必须提供匹配的场景和新实例。"""
        import json

        if payload.get("schema") != 1 or json.dumps(
            payload.get("capabilities"), sort_keys=True
        ) != json.dumps(asdict(self.capabilities), sort_keys=True):
            raise ValueError("Incompatible physical backend")
        if set(payload["state"]) != set(self.state):
            raise ValueError("Incomplete physical state")
        from math import isclose
        from fairy.acps.ontology.farm.spec import finite

        candidate = copy.deepcopy(payload["state"])
        c = self.config
        for k, v in candidate.items():
            if k not in ("prepared", "battery", "combine", "trailer", "stored_batches"):
                finite(v, k, 0)
        if any(type(candidate[k]) is not int for k in ("seeds", "seeds_used")):
            raise ValueError("Invalid seed count")
        if candidate["seeds"] + candidate["seeds_used"] != c.initial_seed_count:
            raise ValueError("Seed conservation mismatch")
        if not isclose(
            candidate["fuel_l"] + candidate["fuel_used_l"], c.initial_fuel_l, abs_tol=1e-8
        ):
            raise ValueError("Fuel conservation mismatch")
        if len(candidate["prepared"]) != c.ridge_count or any(
            type(v) is not bool for v in candidate["prepared"]
        ):
            raise ValueError("Invalid prepared coverage")
        if set(candidate["battery"]) != {"multispectral", "thermal", "robot"}:
            raise ValueError("Invalid equipment identities")
        for v in candidate["battery"].values():
            finite(v, "battery", 0, 100)
        for name, cap in (("combine", c.combine_capacity_kg), ("trailer", c.trailer_capacity_kg)):
            batch = candidate[name]
            if set(batch) != {"dry_kg", "water_kg"}:
                raise ValueError("Invalid grain container")
            for v in batch.values():
                finite(v, "grain mass", 0)
            finite(self.weight(batch), "container mass", 0, cap + 1e-8)
        for batch in candidate["stored_batches"]:
            if set(batch) != {"dry_kg", "water_kg", "moisture"}:
                raise ValueError("Invalid stored batch")
            for v in batch.values():
                finite(v, "stored batch", 0)
            if (
                not isclose(self.moisture(batch), batch["moisture"], abs_tol=1e-9)
                or batch["moisture"] > c.safe_moisture + 1e-9
            ):
                raise ValueError("Unsafe or inconsistent stored grain")
        if sum(self.weight(b) for b in candidate["stored_batches"]) > c.storage_capacity_kg + 1e-8:
            raise ValueError("Invalid warehouse capacity")
        all_batches = [candidate["combine"], candidate["trailer"], *candidate["stored_batches"]]
        for key, total in (
            ("dry_kg", candidate["harvested_dry_kg"]),
            ("water_kg", candidate["harvested_water_kg"] - candidate["removed_water_kg"]),
        ):
            if not isclose(sum(b[key] for b in all_batches), total, abs_tol=1e-8):
                raise ValueError("Grain material conservation mismatch")
        for component in COMPONENTS:
            recovered = candidate["harvested_" + component]
            loss = candidate["harvest_loss_" + component]
            if not isclose(
                recovered, (recovered + loss) * c.harvest_recovery_fraction, abs_tol=1e-8
            ):
                raise ValueError("Harvest recovery disagrees with machinery parameters")
        self.state = candidate

    def audit(self) -> JsonObject:
        """返回可信审计投影，用于守恒和恢复检查，不交给普通Agent。"""
        s = self.state
        dry = sum(s[k]["dry_kg"] for k in CONTAINERS) + sum(
            b["dry_kg"] for b in s["stored_batches"]
        )
        water = sum(s[k]["water_kg"] for k in CONTAINERS) + sum(
            b["water_kg"] for b in s["stored_batches"]
        )
        return {
            **copy.deepcopy(s),
            "dry_mass_balance_error_kg": s["harvested_dry_kg"] - dry,
            "water_mass_balance_error_kg": s["harvested_water_kg"] - water - s["removed_water_kg"],
        }
