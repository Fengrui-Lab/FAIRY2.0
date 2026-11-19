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


from fairy.events.records import detached, canonical
from fairy.agents.loop.context.history import public_memory, progress_diagnostic


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
        for event in runtime.events:
            if event.kind != "operation_result" or not event.payload["ok"]:
                continue
            receipt = event.payload.get("data", {}).get("receipt", {})
            operation = receipt.get("operation")
            if "combine" in receipt and "trailer" in receipt:
                containers = {
                    name: {key: receipt[name][key] for key in ("dry_kg", "water_kg")}
                    for name in containers
                }
                capacities = {
                    name: receipt[name + "_capacity_kg"]
                    for name in ("combine", "trailer", "storage")
                }
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
            if operation in (
                "prepare_seedbed",
                "plant",
                "harvest",
                "unload",
                "dry_grain",
                "store_grain",
                "charge_equipment",
                "survey_multispectral",
                "survey_thermal",
                "ground_inspect",
            ):
                inventory_change_seq = event.seq
            prepared.update(receipt.get("prepared_ridges", ()))
            if operation == "plant":
                planted.update(receipt["completed_ridges"])
            elif operation == "harvest":
                harvested.update(receipt["completed_ridges"])
            elif operation == "store_grain":
                stored += 1
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
        else:
            raise ValueError("Public task context exceeds character budget")
    return detached(context)
