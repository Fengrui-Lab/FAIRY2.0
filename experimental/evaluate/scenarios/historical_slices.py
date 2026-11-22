"""Cut irrigation tasks from executed L3 traces, without mixing task and forcing."""

from __future__ import annotations

from zoneinfo import ZoneInfo
import json
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Sequence, cast

from evaluate.scenarios.historical_scenarios import (
    MIN_RESPONSE,
    REGIONS,
    TARGET,
    Invoke,
    boundary,
    call,
    effective_response,
    readings,
    ready_for_water,
    require,
    window,
)
from evaluate.scenarios.spec import ScenarioSpec
from fairy.acps.ontology.contracts import JsonObject, OperationResult
from fairy.agents.loop.context.history import public_history
from fairy.events.artifacts import artifact, assert_equivalent, replay
from fairy.events.records import HistoryTurn, PublicStatus, fingerprint
from fairy.events.session import Runtime

TOOLS = (
    "observe_soil_range",
    "inspect_crop",
    "get_current_weather",
    "irrigate_range",
    "advance_time",
)


def capture(
    parent: JsonObject, day: str, level: str, checkpoint_name: str
) -> tuple[JsonObject, JsonObject]:
    """Re-execute and capture the exact prefix of the selected historical run."""
    if level not in ("L1", "L2"):
        raise ValueError("Only L1/L2 can be captured")
    state = parent["state"]
    runtime = Runtime(ScenarioSpec.from_dict(state["scenario"]), state["backend"])
    for event in state["events"]:
        if event["kind"] != "operation_requested":
            continue
        payload = event["payload"]
        if event["time"][:10] == day and (
            level == "L2" or payload["tool"] == "irrigate_range"
        ):
            break
        from fairy.acps.ontology.contracts import OperationRequest

        runtime.invoke(
            OperationRequest(payload["tool"], payload["arguments"]),
            actor=payload["actor"],
        )
    else:
        raise ValueError("L3 trace did not reach the requested cut")
    prefix = runtime.export_state()
    assert_equivalent(state["events"][: len(runtime.events)], prefix["events"])
    if runtime.now.date().isoformat() != day or runtime.pending_irrigation_mm != 0:
        raise ValueError("Slice must start on the requested date before irrigation")
    checkpoint = artifact(runtime, {"kind": "captured_l3_prefix"})
    suffix_days = 1 if level == "L1" else 8
    source = json.loads(runtime.scenario.construction_json or "{}").get("document", {})
    dual = source.get("identity", {}).get("role") == "dual_dry_patch"
    if dual:
        assert runtime.scenario.season is not None
        zones = [list(z[:2]) for z in runtime.scenario.season.hydraulic_zones]
        if level == "L1":
            target = payload["arguments"]
            zones = [[target["start_ridge"], target["end_ridge"]]]
    else:
        zones = [list(pair) for pair in ((TARGET,) if level == "L1" else REGIONS)]
    document = {
        "schema": 1,
        "identity": {
            "id": f"{runtime.scenario.scenario_id}_{level.lower()}_irrigation",
            "level": level,
        },
        "initial_state": {
            "checkpoint": checkpoint_name,
            "fingerprint": fingerprint(prefix),
            "time": runtime.now.isoformat(),
            "through_event_seq": len(runtime.events),
        },
        "environment": {
            "parent_id": runtime.scenario.scenario_id,
            "parent_scenario_fingerprint": prefix["scenario_fingerprint"],
            "source_trace_fingerprint": fingerprint(parent),
        },
        "task": {
            "deadline": boundary(
                date.fromisoformat(day) + timedelta(days=suffix_days), zone=runtime.zone
            ).isoformat(),
            "tools": list(TOOLS),
            "candidate_regions": zones,
            **({"compare_all_candidates": True} if dual and level == "L2" else {}),
            "required_root_vwc_response": MIN_RESPONSE,
            "requires_diagnosis": level == "L2",
            **(
                {"recommended_depth_mm": payload["arguments"]["depth_mm"]}
                if state["backend"] == "wofost" and level == "L1"
                else {}
            ),
            "description": (
                "Recheck and irrigate the confirmed ridge zone once, then verify the root moisture."
                if level == "L1"
                else "Use current crop and soil observations to select a water-limited reproductive-stage "
                "zone, irrigate it once, then verify the root moisture."
            ),
        },
    }
    return document, checkpoint


def _check_start(doc: JsonObject, checkpoint: JsonObject) -> Runtime:
    state = checkpoint["state"]
    if (
        doc["schema"] != 1
        or doc["identity"]["level"] not in ("L1", "L2")
        or fingerprint(state) != doc["initial_state"]["fingerprint"]
        or state["scenario_fingerprint"]
        != doc["environment"]["parent_scenario_fingerprint"]
    ):
        raise ValueError("Slice checkpoint identity mismatch")
    runtime = Runtime.from_state(state)
    if (
        runtime.done
        or runtime.scenario.scenario_id != doc["environment"]["parent_id"]
        or runtime.now.isoformat() != doc["initial_state"]["time"]
        or len(runtime.events) != doc["initial_state"]["through_event_seq"]
        or runtime.pending_irrigation_mm != 0
        or not runtime.now
        < datetime.fromisoformat(doc["task"]["deadline"])
        <= runtime.deadline
    ):
        raise ValueError("Inconsistent slice boundary")
    return runtime


class SliceRunner:
    """Own the short-task allowlist while reusing the original Runtime and operation dispatch."""

    def __init__(
        self,
        document: JsonObject,
        checkpoint: JsonObject,
        *,
        resume_state: JsonObject | None = None,
    ) -> None:
        self.document = document
        self.checkpoint = checkpoint
        self.runtime = _check_start(document, checkpoint)
        self.deadline = datetime.fromisoformat(document["task"]["deadline"])
        if resume_state is not None:
            restored = Runtime.from_state(resume_state)
            if restored.done or restored.now > self.deadline:
                raise ValueError("Cannot resume a finished or expired slice")
            score_slice(document, checkpoint, restored)
            self.runtime = restored

    def status(self) -> PublicStatus:
        return self.runtime.public_status()

    def invoke(self, tool: str, **args: object) -> OperationResult:
        if tool not in TOOLS or tool not in self.document["task"]["tools"]:
            raise ValueError("Operation outside the slice allowlist")
        if tool == "advance_time":
            hours = args["hours"]
            if isinstance(hours, (int, float)) and (
                self.runtime.now.astimezone(timezone.utc) + timedelta(hours=hours)
                > self.deadline.astimezone(timezone.utc)
            ):
                raise ValueError("Advance exceeds slice deadline")
        if tool == "irrigate_range" and (
            [args["start_ridge"], args["end_ridge"]]
            not in self.document["task"]["candidate_regions"]
            or self.runtime.irrigation_used_mm
            != self.checkpoint["state"]["irrigation_used_mm"]
        ):
            raise ValueError("Irrigation outside the allowed one-operation slice scope")
        return call(self.runtime, tool, **args)

    def finish(self, kind: str) -> JsonObject:
        self.runtime.finish("historical_slice_complete")
        record = artifact(self.runtime, {"kind": kind})
        return {
            "schema": 1,
            "slice_fingerprint": fingerprint(self.document),
            "run": record,
            "metrics": score_slice(self.document, self.checkpoint, self.runtime),
        }


def advance_public(invoke: Invoke, status: PublicStatus) -> None:
    now = datetime.fromisoformat(status["time"])
    following = boundary(
        now.date() + timedelta(days=1), zone=ZoneInfo(status["timezone"])
    )
    hours = (
        following.astimezone(timezone.utc) - now.astimezone(timezone.utc)
    ).total_seconds() / 3600
    require(invoke("advance_time", hours=hours))


def public_policy(
    invoke: Invoke,
    status: Callable[[], PublicStatus],
    regions: Sequence[tuple[int, int]],
    deadline: datetime,
    *,
    needs_diagnosis: bool,
    backend: str = "native",
    max_depth: float = 30.0,
) -> None:
    """Choose the driest eligible region using no model state, oracle action or future forcing."""
    while datetime.fromisoformat(status()["time"]).date() < deadline.date():
        weather = invoke("get_current_weather")
        if not weather.ok and weather.error != "WEATHER_UNAVAILABLE":
            require(weather)
        options = [(readings(invoke, region), region) for region in regions]
        eligible = [
            (soil["vwc"], region)
            for soil, region in options
            if not needs_diagnosis or ready_for_water(soil, backend)
        ]
        if eligible:
            _, region = min(eligible)
            remaining = status()["irrigation_remaining_mm"]
            depth = min(max_depth, remaining * 64 / (region[1] - region[0] + 1))
            if depth > 0:
                require(invoke("irrigate_range", **window(region), depth_mm=depth))
                advance_public(invoke, status())
                readings(invoke, region)
                return
        advance_public(invoke, status())


def _match(turn: HistoryTurn, tool: str, region: JsonObject, day: str) -> bool:
    args = turn["request"]["arguments"]
    return (
        turn["result"]["ok"]
        and turn["request"]["tool"] == tool
        and turn["time"][:10] == day
        and all(args.get(k) == v for k, v in region.items())
    )


def _vwc(observations: Sequence[JsonObject]) -> float:
    return sum(row["root_vwc"] for row in observations) / len(observations)


def score_slice(
    doc: JsonObject, checkpoint: JsonObject, runtime: Runtime
) -> JsonObject:
    """Judge the recorded short task against a same-start/same-forcing zero-water control."""
    initial = _check_start(doc, checkpoint)
    boundary_seq = len(initial.events)
    if len(runtime.events) < boundary_seq or fingerprint(
        runtime.scenario.to_dict()
    ) != fingerprint(initial.scenario.to_dict()):
        raise ValueError("Slice run does not bind the checkpoint scenario")
    assert_equivalent(
        initial.export_state()["events"],
        runtime.export_state()["events"][:boundary_seq],
    )
    history = [
        turn for turn in public_history(runtime) if turn["event_seq"] > boundary_seq
    ]
    requests = [
        event
        for event in runtime.events[boundary_seq:]
        if event.kind == "operation_requested"
    ]
    if any(
        event.payload["tool"] not in TOOLS
        or event.payload["tool"] not in doc["task"]["tools"]
        for event in requests
    ):
        raise ValueError("Recorded operation falls outside the slice")
    metrics: JsonObject = {
        "task_success": False,
        "tool_errors": runtime.tool_errors - initial.tool_errors,
        "water_used_mm": runtime.irrigation_used_mm - initial.irrigation_used_mm,
        "settled": runtime.pending_irrigation_mm == 0,
        "target_response_vwc": None,
    }
    irrigation = [
        t
        for t in history
        if t["result"]["ok"] and t["request"]["tool"] == "irrigate_range"
    ]
    if (
        len(irrigation) != 1
        or not metrics["settled"]
        or metrics["tool_errors"]
        or runtime.now > datetime.fromisoformat(doc["task"]["deadline"])
    ):
        return metrics
    treatment = irrigation[0]
    reg = {
        k: treatment["request"]["arguments"][k] for k in ("start_ridge", "end_ridge")
    }
    if list(reg.values()) not in doc["task"]["candidate_regions"]:
        return metrics
    day = treatment["time"][:10]
    after = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
    before = [t for t in history if t["event_seq"] < treatment["event_seq"]]
    pre_soil = [t for t in before if _match(t, "observe_soil_range", reg, day)]
    pre_crop = [t for t in before if _match(t, "inspect_crop", reg, day)]
    post = [t for t in history if _match(t, "observe_soil_range", reg, after)]
    if not (pre_soil and pre_crop and post):
        return metrics
    soil = pre_soil[-1]["result"]["data"]["receipt"]["observations"]
    crop = pre_crop[-1]["result"]["data"]["receipt"]["observations"]
    if doc["task"]["requires_diagnosis"] and not ready_for_water(
        {"vwc": _vwc(soil), "stages": [row["stage"] for row in crop]},
        runtime.backend_name,
    ):
        return metrics
    if doc["task"].get("compare_all_candidates") and any(
        not any(_match(t, tool, window(tuple(zone)), day) for t in before)
        for zone in doc["task"]["candidate_regions"]
        for tool in ("observe_soil_range", "inspect_crop")
    ):
        return metrics
    target_seq = post[0]["event_seq"]
    for event in requests:
        if event.seq >= target_seq:
            break
        if event.payload["tool"] == "advance_time":
            require(call(initial, "advance_time", **event.payload["arguments"]))
    if initial.now.isoformat() != post[0]["time"]:
        raise ValueError("Matched control did not reach the same observation time")
    control = require(call(initial, "observe_soil_range", **reg))["receipt"][
        "observations"
    ]
    response = _vwc(post[0]["result"]["data"]["receipt"]["observations"]) - _vwc(
        control
    )
    metrics["target_response_vwc"] = response
    metrics["task_success"] = effective_response(response, 0)
    return metrics


def verify_slice(
    doc: JsonObject,
    checkpoint: JsonObject,
    output: JsonObject,
    parent: JsonObject,
) -> JsonObject:
    """Validate lineage, re-execute the full event log, then recompute slice metrics."""
    if (
        output["schema"] != 1
        or output["slice_fingerprint"] != fingerprint(doc)
        or doc["environment"]["source_trace_fingerprint"] != fingerprint(parent)
    ):
        raise ValueError("Slice output or source trace fingerprint mismatch")
    prefix = checkpoint["state"]
    n = doc["initial_state"]["through_event_seq"]
    assert_equivalent(parent["state"]["events"][:n], prefix["events"])
    if parent["state"]["scenario_fingerprint"] != prefix["scenario_fingerprint"]:
        raise ValueError("Checkpoint and source trace use different inputs")
    canonical_doc, canonical_start = capture(
        parent,
        doc["initial_state"]["time"][:10],
        doc["identity"]["level"],
        doc["initial_state"]["checkpoint"],
    )
    assert_equivalent(canonical_doc, doc)
    assert_equivalent(canonical_start["state"], prefix)
    replay(checkpoint)
    runtime, _ = replay(output["run"])
    metrics = score_slice(doc, checkpoint, runtime)
    assert_equivalent(output["metrics"], metrics)
    return metrics


def run_public_slice(doc: JsonObject, checkpoint: JsonObject) -> JsonObject:
    runner = SliceRunner(doc, checkpoint)
    regions = cast(
        list[tuple[int, int]], [tuple(row) for row in doc["task"]["candidate_regions"]]
    )
    public_policy(
        runner.invoke,
        runner.status,
        regions,
        runner.deadline,
        needs_diagnosis=doc["task"]["requires_diagnosis"],
        backend=checkpoint["state"]["backend"],
        max_depth=doc["task"].get("recommended_depth_mm", 30.0),
    )
    return runner.finish("public_observation_rule")


def run_reference_slice(
    doc: JsonObject,
    checkpoint: JsonObject,
    dose: tuple[str, float],
    *,
    target: tuple[int, int] = TARGET,
) -> JsonObject:
    """Evaluate a separately selected reference intervention under the same rules."""
    runner = SliceRunner(doc, checkpoint)
    while runner.status()["time"][:10] < dose[0]:
        advance_public(runner.invoke, runner.status())
    if doc["task"].get("compare_all_candidates"):
        for zone in doc["task"]["candidate_regions"]:
            readings(runner.invoke, tuple(zone))
    else:
        readings(runner.invoke, target)
    require(runner.invoke("irrigate_range", **window(target), depth_mm=dose[1]))
    advance_public(runner.invoke, runner.status())
    readings(runner.invoke, target)
    return runner.finish("reference_intervention")
