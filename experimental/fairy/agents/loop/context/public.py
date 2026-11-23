"""Allowlisted model input. No runtime instance, snapshot, or audit state."""

from __future__ import annotations
from typing import Sequence, TYPE_CHECKING, TypedDict

if TYPE_CHECKING:
    from fairy.events.session import Runtime
from dataclasses import asdict
from fairy.acps.ontology.contracts import JsonObject
from fairy.events.records import PublicStatus, HistoryTurn


class _RequiredContext(TypedDict):
    task: str
    status: PublicStatus
    deadline: str
    objects: list[JsonObject]
    tools: list[JsonObject]
    history: list[HistoryTurn]
    context_policy: JsonObject
    public_memory: JsonObject
    progress_diagnostic: JsonObject


class PublicContext(_RequiredContext, total=False):
    """LLM可读白名单；不存在Runtime、审计状态或未来扰动配方字段。"""

    target_vwc: list[float]
    progress: JsonObject
    constraints: JsonObject
    weather: list[JsonObject]
    protocol_feedback: JsonObject
    oracle_guidance: JsonObject
    water_diagnostics: JsonObject
    dose_feedback: JsonObject


from fairy.events.records import detached, canonical
from fairy.agents.loop.context.history import public_memory, progress_diagnostic

_CAPACITY_FEEDBACK = {
    "COMBINE_CAPACITY_EXCEEDED": "Reduce the ridge range or unload the combine. In named-batch scenarios inspect_crop shows harvest_batch_limit.",
    "HARVEST_TRAILER_CAPACITY_EXCEEDED": "Reduce the ridge range. The proposed lot exceeds an empty trailer's capacity.",
    "HARVEST_DRYER_CAPACITY_EXCEEDED": "Reduce the ridge range using inspect_crop.harvest_batch_limit. Waiting is not a remedy for this capacity rejection.",
    "DRYER_CAPACITY_EXCEEDED": "This whole lot cannot fit in the dryer. The lot does not get smaller in the trailer. Do not wait or retry this lot.",
}

_INVENTORY_CHANGES = {
    "prepare_seedbed", "plant", "harvest", "unload", "dry_grain", "store_grain",
    "unload_batch", "dry_batch", "store_batch", "repair_dryer",
    "mount_implement", "unmount_implement", "load_seeds", "return_seeds",
    "refuel_equipment", "return_fuel", "charge_equipment",
    "survey_multispectral", "survey_thermal", "ground_inspect",
}


def build_context(
    runtime: Runtime,
    history: Sequence[HistoryTurn],
    max_chars: int = 12000,
    max_turns: int = 8,
    protocol_feedback: str | None = None,
) -> PublicContext:
    """构建受字符预算约束的公开白名单上下文。

    进度和容器仅从成功回执推导；不会把Runtime、恢复快照或完整forcing传给模型。"""
    if any(type(value) is not int or value < 1 for value in (max_chars, max_turns)):
        raise ValueError("Context budgets must be positive integers")
    spec = runtime.scenario
    context: PublicContext = dict(
        task=spec.task,
        status=runtime.public_status(),
        deadline=spec.deadline,
        objects=runtime.public_objects(),
        tools=runtime.operations.tool_definitions(),
        history=detached(list(history[-max_turns:])),
        context_policy={},
        public_memory={},
        progress_diagnostic={},
    )
    if spec.task_type == "irrigation":
        context["target_vwc"] = [spec.target_vwc_min, spec.target_vwc_max]
    if spec.season:
        planted, harvested, prepared = set(), set(), set()
        inventory_change_seq = 0
        planting_change_seq = 0
        stored = 0
        containers: dict[str, JsonObject | None] = {"combine": None, "trailer": None}
        capacities = {}
        latest_harvest = None
        lots: dict[str, JsonObject] = {}
        locations = {}
        for event in runtime.events:
            if event.kind != "operation_result" or not event.payload["ok"]:
                continue
            receipt = event.payload.get("data", {}).get("receipt", {})
            operation = receipt.get("operation")
            if "grain_batches" in receipt:
                lots = {lot["batch_id"]: lot for lot in receipt["grain_batches"]}
                locations = {
                    "trailer": receipt["trailer_facility"]["object_id"],
                    "warehouse": receipt["warehouse_facility"]["object_id"],
                }
            if "combine" in receipt and "trailer" in receipt:
                containers = {
                    name: {key: receipt[name][key] for key in ("dry_kg", "water_kg")}
                    for name in containers
                }
                capacities = {
                    name: receipt[name + "_capacity_kg"]
                    for name in ("combine", "trailer", "storage")
                }
                if "dryer_facility" in receipt:
                    capacities["dryer"] = receipt["dryer_facility"]["capacity_kg"]
            if operation in ("harvest", "unload_batch", "dry_batch", "store_batch"):
                lot = receipt.get("batch")
                if lot:
                    lots[lot["batch_id"]] = lot
                    if operation == "harvest":
                        locations["combine"] = lot["location_id"]
                    elif operation == "unload_batch":
                        locations["trailer"] = lot["location_id"]
                    elif operation == "store_batch":
                        locations["warehouse"] = lot["location_id"]
            if operation == "harvest":
                batch = {key: receipt[key] for key in ("dry_kg", "water_kg")}
                latest_harvest = {
                    **batch,
                    "completed_ridges": receipt["completed_ridges"],
                    "event_seq": event.seq,
                }
                if containers["combine"] is not None:
                    containers["combine"] = {
                        key: containers["combine"][key] + batch[key] for key in batch
                    }
            elif operation == "unload":
                if containers["combine"] is not None and containers["trailer"] is not None:
                    containers["trailer"] = {
                        key: containers["trailer"][key] + containers["combine"][key]
                        for key in ("dry_kg", "water_kg")
                    }
                else:
                    containers["trailer"] = None
                containers["combine"] = {"dry_kg": 0.0, "water_kg": 0.0}
            elif operation == "dry_grain" and containers["trailer"] is not None:
                containers["trailer"]["water_kg"] -= receipt["removed_water_kg"]
            elif operation == "store_grain":
                containers["trailer"] = {"dry_kg": 0.0, "water_kg": 0.0}
            if operation in ("prepare_seedbed", "plant"):
                prepared.update(receipt["completed_ridges"])
            if operation == "plant":
                planting_change_seq = event.seq
            if operation in _INVENTORY_CHANGES:
                inventory_change_seq = event.seq
            prepared.update(receipt.get("prepared_ridges", ()))
            if operation == "plant":
                planted.update(receipt["completed_ridges"])
            elif operation == "harvest":
                harvested.update(receipt["completed_ridges"])
            elif operation == "store_grain":
                stored += 1
        batch_progress = {}
        if lots:
            for name in containers:
                at_location = [lot for lot in lots.values() if lot["location_id"] == locations.get(name)]
                containers[name] = {
                    key: sum((lot[key] for lot in at_location), 0.0)
                    for key in ("dry_kg", "water_kg")
                }
            stored = sum(lot["location_id"] == locations.get("warehouse") for lot in lots.values())
            batch_progress["pending_grain_batches"] = [
                {
                    "batch_id": lot["batch_id"],
                    "location_id": lot["location_id"],
                    "drying_completed": lot["dried_by"] is not None,
                }
                for lot in lots.values()
                if lot["location_id"] != locations.get("warehouse")
            ]
        capacity_feedback = {}
        for turn in history:
            request, result = turn["request"], turn["result"]
            tool = request["tool"]
            if result["ok"]:
                capacity_feedback.pop(tool, None)
            elif result["error"] in _CAPACITY_FEEDBACK:
                capacity_feedback[tool] = {
                    "request": request,
                    "error": result["error"],
                    "response_guidance": _CAPACITY_FEEDBACK[result["error"]],
                }
        if capacity_feedback:
            batch_progress["capacity_feedback"] = list(capacity_feedback.values())
        targets = set(range(spec.season.ridge_count))
        context["progress"] = {
            "source": "successful public tool receipts",
            "prepared_ridges": sorted(prepared),
            "last_inventory_change_seq": inventory_change_seq,
            "last_planting_change_seq": planting_change_seq,
            "planted_ridges": sorted(planted),
            "harvested_ridges": sorted(harvested),
            "remaining_to_plant": sorted(targets - planted),
            "remaining_to_harvest": sorted(targets - harvested),
            "successful_storage_transfers": stored,
            "public_containers": {
                "source": "derived only from successful public receipts; null means unknown",
                "amounts": containers,
                "capacities_kg": capacities,
            },
            "latest_harvest_receipt": latest_harvest,
            **batch_progress,
        }
        context["constraints"] = dict(
            ridge_count=spec.season.ridge_count,
            backend=runtime.backend_name,
            cultivar=spec.season.cultivar,
            spacing_m=spec.season.seed_spacing_cm / 100,
            depth_mm=spec.season.sowing_depth_cm * 10,
            safe_storage_moisture=spec.season.safe_moisture,
        )
    if spec.weather_in_context:
        context["weather"] = [
            asdict(w) for w in spec.public_weather if w.day >= runtime.now.date().isoformat()
        ]
    context["progress_diagnostic"] = progress_diagnostic(history, runtime.now)
    if protocol_feedback:
        context["protocol_feedback"] = {
            "error": protocol_feedback,
            "instruction": "No farm operation was executed. Return one complete tool call or finish with text.",
        }
    context["context_policy"] = {
        "version": "public_history_v2",
        "history_available": len(history),
        "history_kept": len(context["history"]),
        "history_omitted": 0,
    }
    if spec.season and spec.season.cultivar == "Soybean_901_proxy" and runtime.backend_name == "wofost":
        from fairy.agents.loop.context.water import current_water_context

        rows = current_water_context(spec, history, runtime.now)
        if rows:
            context["water_diagnostics"] = {
                "source": "public observations, accepted irrigation receipts, declared soil",
                "scope": "current root-water availability and separately dated past stress; no irrigation recommendation",
                "zones": rows,
            }
    context["public_memory"] = public_memory(history, context["history"])
    while True:
        # Counts are part of the budget, including when their digit lengths change.
        context["context_policy"].update(
            history_kept=len(context["history"]),
            history_omitted=len(history) - len(context["history"]),
        )
        if len(canonical(context)) <= max_chars:
            break
        if context["history"]:
            context["history"].pop(0)
            context["public_memory"] = public_memory(history, context["history"])
        elif context["public_memory"]["observations"]:
            context["public_memory"]["observations"].pop(0)
            context["public_memory"]["omitted_observation_targets"] += 1
        elif context["public_memory"]["unresolved_rejections"]:
            context["public_memory"]["unresolved_rejections"].pop(0)
            context["public_memory"]["omitted_rejections"] += 1
        elif "water_diagnostics" in context:
            zones = context["water_diagnostics"]["zones"]
            zones.pop()
            if not zones:
                context.pop("water_diagnostics")
        else:
            raise ValueError("Public task context exceeds character budget")
    return detached(context)
