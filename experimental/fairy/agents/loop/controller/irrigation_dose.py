"""Translate a model's irrigation intent into one auditable runtime request."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Mapping, Sequence

from fairy.acps.ontology.contracts import JsonObject, OperationRequest, OperationResult
from fairy.acps.ontology.infrastructure.irrigation import IRRIGATE_RANGE
from fairy.agents.loop.context.public import PublicContext
from fairy.agents.loop.context.water import zone_irrigation
from fairy.agents.models.numeric.dose import (
    CompletedWeather,
    IrrigationDoseInput,
    IrrigationDosePolicy,
    RidgeEvidence,
)
from fairy.events.records import HistoryTurn, detached, fingerprint

INTENT = "request_irrigation"
OPERATION = IRRIGATE_RANGE.name
DIRECT_WATER = {OPERATION, "irrigate"}
TARGET_PARAMETERS = {"start_ridge", "end_ridge"}
MAX_DEPTH_MM = next(
    p.maximum
    for p in IRRIGATE_RANGE.parameters
    if p.name == "depth_mm" and p.maximum is not None
)
FEEDBACK = {
    "IRRIGATION_INTENT_REQUIRED": "Use request_irrigation with the target ridge range.",
    "INVALID_IRRIGATION_TARGET": "Provide exactly start_ridge and end_ridge as valid integer ridge IDs.",
    "IRRIGATION_PENDING": "Water is already pending on at least one target ridge. Reassess after settlement.",
    "FRESH_IRRIGATION_EVIDENCE_REQUIRED": "Inspect crop and observe soil on the full target range today, after any planting or harvest.",
    "CROP_NOT_ACTIVE": "Irrigation dosing requires a living, emerged crop throughout the target range.",
    "IRRIGATION_WATER_UNAVAILABLE": "The scenario has no available irrigation water for the target.",
    "IRRIGATION_POLICY_ABSTAINED": "The dose policy abstained. Reassess current evidence or choose another operation.",
    "IRRIGATION_INVALID_POLICY_OUTPUT": "The dose service returned an invalid result. Choose another operation.",
}


class IrrigationDosing:
    """Execution service for public intents. Policy input comes from public receipts."""

    def __init__(
        self, policy: IrrigationDosePolicy, profiles: Mapping[int, tuple[float, float]]
    ) -> None:
        self.policy = policy
        self.profiles = dict(profiles)
        self.attempts: list[JsonObject] = []
        self.feedback: str | None = None

    def model_context(self, base: PublicContext) -> PublicContext:
        """Expose the intent schema and keep requests in chat history schema-compatible."""
        context = detached(base)
        tools = []
        for tool in context["tools"]:
            function = tool["function"]
            if function["name"] == OPERATION:
                function["name"] = INTENT
                function["description"] = (
                    "Request irrigation now for these ridges. Choose target and timing; "
                    "a separate policy selects the depth. Obtain today's crop and soil "
                    "observations covering the entire target first."
                )
                parameters = function["parameters"]
                parameters["properties"].pop("depth_mm")
                parameters["required"].remove("depth_mm")
            elif function["name"] in DIRECT_WATER:
                continue
            tools.append(tool)
        context["tools"] = tools
        for turn in [
            *context["history"],
            *context["public_memory"]["unresolved_rejections"],
        ]:
            request = turn["request"]
            if request["tool"] == OPERATION:
                request["tool"] = INTENT
                request["arguments"].pop("depth_mm", None)
        if self.feedback:
            context["dose_feedback"] = {
                "error": self.feedback,
                "instruction": FEEDBACK[self.feedback],
                "operation_executed": False,
            }
        return context

    def resolve(
        self,
        request: OperationRequest,
        context: PublicContext,
        history: Sequence[HistoryTurn],
    ) -> OperationRequest | None:
        """Return a resolved operation, or record a rejection without any farm write."""
        self.feedback = None
        if request.tool not in DIRECT_WATER | {INTENT}:
            return request
        if request.tool in DIRECT_WATER:
            return self._reject("IRRIGATION_INTENT_REQUIRED")
        args = request.arguments
        if set(args) != TARGET_PARAMETERS:
            return self._reject("INVALID_IRRIGATION_TARGET")
        first, last = args["start_ridge"], args["end_ridge"]
        if (
            type(first) is not int
            or type(last) is not int
            or not 0 <= first <= last < len(self.profiles)
        ):
            return self._reject("INVALID_IRRIGATION_TARGET")
        target = set(range(first, last + 1))
        status = context["status"]
        now = datetime.fromisoformat(status["time"])
        ledger = zone_irrigation(history, now, target)
        if ledger["pending_ridges"]:
            return self._reject("IRRIGATION_PENDING", args)
        maximum = min(
            MAX_DEPTH_MM,
            status["irrigation_remaining_mm"] * len(self.profiles) / len(target),
        )
        if maximum <= 1e-9:
            return self._reject("IRRIGATION_WATER_UNAVAILABLE", args)

        phases: dict[int, str] = {}
        soil: dict[int, float] = {}
        changed: set[int] = set()
        weather = None
        for turn in reversed(history):
            stamp = datetime.fromisoformat(turn["time"]).astimezone(now.tzinfo)
            if stamp.date() < now.date():
                break
            if stamp > now or not turn["result"]["ok"]:
                continue
            name = turn["request"]["tool"]
            data = turn["result"]["data"]
            receipt = data.get("receipt", {})
            if name in ("plant", "harvest", "replant_gaps"):
                span = turn["request"]["arguments"]
                if TARGET_PARAMETERS <= span.keys():
                    changed.update(range(span["start_ridge"], span["end_ridge"] + 1))
            elif name in ("observe_soil_range", "inspect_crop"):
                for row in receipt.get("observations", ()):
                    ridge = row["ridge_id"]
                    if ridge in target and ridge not in changed:
                        if name == "observe_soil_range":
                            soil.setdefault(ridge, row["root_vwc"])
                        else:
                            phases.setdefault(ridge, row["phase"])
            elif name == "get_current_weather" and weather is None:
                report = data["weather"]
                if report["day"] == (now.date() - timedelta(days=1)).isoformat():
                    weather = CompletedWeather(
                        report["day"], report["rain_mm"], report["et0_mm"]
                    )
        if phases.keys() != target or soil.keys() != target:
            return self._reject("FRESH_IRRIGATION_EVIDENCE_REQUIRED", args)
        if any(
            phase not in ("vegetative", "reproductive") for phase in phases.values()
        ):
            return self._reject("CROP_NOT_ACTIVE", args)
        evidence = IrrigationDoseInput(
            now,
            tuple(
                RidgeEvidence(i, phases[i], soil[i], *self.profiles[i])
                for i in sorted(target)
            ),
            maximum,
            ledger["last_local_midnight_settled_zone_mean_mm"],
            weather,
        )
        choice = self.policy.choose(evidence)
        depth = choice.depth_mm
        if depth is None:
            return self._reject("IRRIGATION_POLICY_ABSTAINED", args)
        if type(depth) not in (int, float) or not 0 < depth <= maximum:
            return self._reject("IRRIGATION_INVALID_POLICY_OUTPUT", args)
        self.attempts.append(
            dict(
                target=dict(args),
                time=now.isoformat(),
                selected_depth_mm=depth,
                maximum_depth_mm=maximum,
                evidence_fingerprint=fingerprint(
                    asdict(evidence) | {"time": now.isoformat()}
                ),
            )
        )
        return OperationRequest(OPERATION, {**args, "depth_mm": depth})

    def _reject(
        self, code: str, target: JsonObject | None = None
    ) -> OperationRequest | None:
        self.feedback = code
        self.attempts.append(dict(target=target, rejected=code))
        return None

    def record_result(self, result: OperationResult, event_seq: int) -> None:
        """Associate the numeric choice with the actual public runtime result."""
        self.attempts[-1].update(event_seq=event_seq, ok=result.ok, error=result.error)

    def snapshot(self) -> JsonObject:
        return {
            "policy": self.policy.identity,
            "attempts": detached(self.attempts),
            "feedback": self.feedback,
        }
