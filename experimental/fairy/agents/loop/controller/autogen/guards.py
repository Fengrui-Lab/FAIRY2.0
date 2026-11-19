"""CAPE-style three-valued checks. Unknown facts never come from backend truth."""

from __future__ import annotations
from typing import Literal, TYPE_CHECKING

if TYPE_CHECKING:
    from fairy.acps.ontology.contracts import JsonObject, OperationRequest
    from fairy.agents.loop.context.public import PublicContext
from dataclasses import dataclass, asdict
import math
from .public import PublicView


@dataclass(frozen=True)
class Check:
    """三值预检查结论及依据；PASS只覆盖已检查条件，不能保证隐藏容量或资源均满足。"""

    status: Literal["PASS", "BLOCKED", "NEED_OBSERVATION"]
    rule: str
    reason: str
    source: str = "public_tool_contract"
    required_observations: tuple[str, ...] = ()

    def to_dict(self) -> JsonObject:
        """返回脱离内部对象的序列化声明，供来源指纹或可信恢复使用。"""
        return {
            **asdict(self),
            "executed": False,
            "semantics": "preflight only; runtime remains final authority",
        }


def guard(
    context: PublicContext, request: OperationRequest, *, advisor_required: bool = True
) -> Check:
    """仅根据公开证据返回PASS/BLOCKED/NEED_OBSERVATION；标明物理规则或生成协议来源。"""
    tool = next(
        (t["function"] for t in context["tools"] if t["function"]["name"] == request.tool), None
    )
    if tool is None:
        return Check("BLOCKED", "known_operation", "Unknown operation")
    args = request.arguments
    schema = tool["parameters"]
    props = schema["properties"]
    if (
        not isinstance(args, dict)
        or not set(schema.get("required", ())) <= set(args)
        or not set(args) <= set(props)
    ):
        return Check(
            "BLOCKED", "argument_schema", "Use exactly the declared required parameter names"
        )
    for name, value in args.items():
        p = props[name]
        kind = p["type"]
        if (
            kind == "integer"
            and type(value) is not int
            or kind == "number"
            and (type(value) not in (int, float) or not math.isfinite(value))
            or kind == "string"
            and type(value) is not str
        ):
            return Check("BLOCKED", "argument_schema", "Invalid type for " + name)
        if kind in ("number", "integer") and any(
            (key in p and test(value, p[key]))
            for key, test in [
                ("minimum", lambda x, b: x < b),
                ("maximum", lambda x, b: x > b),
                ("exclusiveMinimum", lambda x, b: x <= b),
            ]
        ):
            return Check("BLOCKED", "argument_bounds", f"{name} must satisfy {p}")
    view = PublicView(context)
    progress = context.get("progress", {})
    indices = (
        list(range(args["start_ridge"], args["end_ridge"] + 1)) if "start_ridge" in args else []
    )
    if indices and args["end_ridge"] < args["start_ridge"]:
        return Check("BLOCKED", "ridge_range", "End ridge precedes start")
    if request.tool == "advance_time":
        if (
            advisor_required
            and view.phase() in ("establishment", "growth_management")
            and args["hours"] > 24
        ):
            return Check(
                "BLOCKED",
                "daily_numeric_window",
                "Use at most 24 hours between management opportunities",
                "generator_protocol",
            )
        return Check("PASS", "clock", "Clock request satisfies declared checks")
    if request.tool in (
        "inspect_crop",
        "observe_soil_range",
        "inspect_inventory",
        "get_current_weather",
        "get_forecast",
        "survey_multispectral",
        "survey_thermal",
        "ground_inspect",
        "charge_equipment",
    ):
        return Check(
            "PASS",
            "observation",
            "Known parameter checks passed; runtime checks remaining resources",
        )
    if request.tool in ("prepare_seedbed", "plant", "harvest"):
        soil = view.field("observe_soil_range", indices)
        weather = view.data("get_current_weather")
        missing = tuple(
            name
            for name, value in [("observe_soil_range", soil), ("get_current_weather", weather)]
            if not value
        )
        if missing:
            return Check(
                "NEED_OBSERVATION",
                "trafficability_evidence",
                "Fresh target soil and current weather required",
                required_observations=missing,
            )
        assert soil is not None and weather is not None
        if any(r["root_vwc"] > 0.36 for r in soil) or weather["weather"]["rain_mm"] > 10:
            return Check(
                "BLOCKED",
                "trafficability",
                "Observed wet-soil or rain limit is exceeded",
                "audited_native_contract",
            )
    if request.tool == "prepare_seedbed":
        if set(indices) & set(progress.get("planted_ridges", ())):
            return Check(
                "BLOCKED", "unplanted", "Cannot prepare planted ridges", "audited_native_contract"
            )
    if request.tool == "plant":
        if not set(indices) <= set(progress.get("prepared_ridges", ())):
            return Check(
                "NEED_OBSERVATION",
                "prepared_evidence",
                "Preparation receipt or inventory evidence required",
                required_observations=("inspect_inventory",),
            )
        if set(indices) & set(progress.get("planted_ridges", ())):
            return Check("BLOCKED", "not_planted", "Targets already planted")
        if (
            args["spacing_m"] != context["constraints"]["spacing_m"]
            or args["depth_mm"] != context["constraints"]["depth_mm"]
        ):
            return Check(
                "BLOCKED",
                "task_planting_geometry",
                "Use the declared task spacing and depth",
                "task_requirement",
            )
    if request.tool == "irrigate_range":
        if (
            args["depth_mm"] * len(indices) / view.count
            > view.status["irrigation_remaining_mm"] + 1e-9
        ):
            return Check(
                "BLOCKED", "water_quota", "Requested field-average depth exceeds remaining quota"
            )
        soil = view.field("observe_soil_range", indices)
        crop = view.field("inspect_crop", indices)
        missing = tuple(
            name
            for name, value in [("observe_soil_range", soil), ("inspect_crop", crop)]
            if not value
        )
        if missing:
            return Check(
                "NEED_OBSERVATION",
                "irrigation_evidence",
                "Fresh crop and root-water observations required",
                required_observations=missing,
            )
        if advisor_required and (
            indices != list(range(view.count)) or view.status["pending_irrigation_mm"] != 0
        ):
            return Check(
                "BLOCKED",
                "advisor_scope",
                "MVP adviser covers the full field once per settled daily window",
                "generator_protocol",
            )
    if request.tool == "harvest":
        if set(indices) & set(progress.get("harvested_ridges", ())):
            return Check("BLOCKED", "not_harvested", "Targets already harvested")
        crop = view.field("inspect_crop", indices)
        if not crop:
            return Check(
                "NEED_OBSERVATION",
                "maturity_evidence",
                "Read current target crop stages",
                required_observations=("inspect_crop",),
            )
        if any(not r.get("harvestable", r["stage"] == "R8") for r in crop):
            return Check(
                "BLOCKED",
                "r8_maturity",
                "Harvest requires observed backend maturity; grain moisture is separate",
                "public_backend_contract",
            )
    if request.tool in ("dry_grain", "store_grain", "unload"):
        inventory = view.inventory()
        if inventory is None:
            return Check(
                "NEED_OBSERVATION",
                "inventory_evidence",
                "Inspect current containers before this transfer",
                required_observations=("inspect_inventory",),
            )
        container = inventory["combine" if request.tool == "unload" else "trailer"]
        dry, water = container["dry_kg"], container["water_kg"]
        weight = dry + water
        if weight <= 0:
            return Check("BLOCKED", "nonempty_container", "The relevant container is empty")
        moisture = water / weight
        if request.tool == "dry_grain" and moisture <= args["target_moisture"] + 1e-12:
            return Check(
                "BLOCKED",
                "drying_needed",
                "Grain is already at or below the requested wet-basis moisture",
            )
        if (
            request.tool == "store_grain"
            and moisture > context["constraints"]["safe_storage_moisture"] + 1e-9
        ):
            return Check("BLOCKED", "safe_storage", "Observed trailer grain is too wet for storage")
    return Check(
        "PASS",
        "known_preconditions",
        "Known public checks passed; unobserved field mass and engine resources remain runtime checks",
    )
