"""Historical flood fixture with an observed decision and matched untreated branch."""

from datetime import date, timedelta
from pathlib import Path
from typing import Callable

from evaluate.scenarios.historical_scenarios import (
    FULL,
    TARGET,
    Invoke,
    call,
    close_harvest,
    compile_season,
    make_regional_document,
    plant_prefix,
    require,
    verify_season,
    window,
)
from evaluate.scenarios.historical_slices import advance_public
from evaluate.scenarios.historical_weather import POINT, TIMEZONE
from evaluate.scenarios.season import SeasonSpec
from evaluate.scenarios.spec import ScenarioSpec
from fairy.acps.ontology.contracts import JsonObject, OperationRequest
from fairy.acps.ontology.farm.spec import FarmSpec
from fairy.agents.loop.context.history import public_history
from fairy.events.artifacts import artifact, assert_equivalent, replay, write_json
from fairy.events.records import PublicStatus, fingerprint
from fairy.events.session import Runtime

FLOOD_OBSERVATIONS = ("observe_surface_water", "inspect_crop")
SLICE_TOOLS = (*FLOOD_OBSERVATIONS, "drain_surface", "advance_time")


def _read(invoke: Invoke, region: tuple[int, int]) -> list[JsonObject]:
    return require(invoke("observe_surface_water", **window(region)))["receipt"][
        "observations"
    ]


def _respond(
    invoke: Invoke, region: tuple[int, int] | None = None
) -> tuple[int, int] | None:
    """Detect a designated or discovered flooded range solely from measured surface water."""
    rows = _read(invoke, region or (0, 63))
    wet = [
        r["ridge_id"]
        for r in rows
        if r["drainage_enabled"] and r["standing_water_mm"] >= 5
    ]
    if not wet:
        return None
    if wet != list(range(wet[0], wet[-1] + 1)):
        raise ValueError("The single-zone flood policy cannot span dry gaps")
    found = (wet[0], wet[-1])
    crop = require(invoke("inspect_crop", **window(found)))["receipt"]["observations"]
    if not all(row["phase"] in ("vegetative", "reproductive") for row in crop):
        return None
    require(invoke("drain_surface", **window(found), max_depth_mm=30))
    return found


def _operator(runtime: Runtime) -> Invoke:
    def invoke(name: str, **args: object):
        return call(runtime, name, **args)

    return invoke


def _full_run(spec: ScenarioSpec, *, drain: bool) -> tuple[JsonObject, JsonObject]:
    runtime = plant_prefix(spec)
    invoke = _operator(runtime)
    daily = []
    while runtime.now < runtime.deadline:
        current = runtime.now.date().isoformat()
        target = _read(invoke, TARGET)
        maturity = require(invoke("inspect_crop", **FULL))["receipt"]["observations"]
        if all(r["harvestable"] for r in maturity):
            break
        stages = sorted({maturity[row["ridge_id"]]["stage"] for row in target})
        depth = max(row["standing_water_mm"] for row in target)
        daily.append(
            {
                "day": current,
                "max_depth_mm": depth,
                "max_waterlogging_days": max(
                    row["crop_waterlogging_days"] for row in target
                ),
                "stages": stages,
            }
        )
        if drain and depth >= 5:
            _respond(invoke)
        advance_public(invoke, runtime.public_status())
    close_harvest(runtime)
    result = artifact(
        runtime, {"kind": "public_flood_rule" if drain else "control_wait"}
    )
    return result, {"daily": daily}


def _cuts(parent: JsonObject, selected_day: str) -> dict[str, JsonObject]:
    state = parent["state"]
    runtime = Runtime(ScenarioSpec.from_dict(state["scenario"]), state["backend"])
    before_action = None
    before_observing = None
    for event in state["events"]:
        if event["kind"] != "operation_requested":
            continue
        body = event["payload"]
        if event["time"][:10] == selected_day:
            if before_observing is None:
                before_observing = artifact(runtime, {"kind": "flood_diagnosis_prefix"})
                assert_equivalent(
                    state["events"][: event["seq"] - 1],
                    before_observing["state"]["events"],
                )
            if body["tool"] == "drain_surface":
                before_action = artifact(runtime, {"kind": "flood_action_prefix"})
                assert_equivalent(
                    state["events"][: event["seq"] - 1],
                    before_action["state"]["events"],
                )
                break
        runtime.invoke(
            OperationRequest(body["tool"], body["arguments"]), actor=body["actor"]
        )
    if before_observing is None or before_action is None:
        raise ValueError("A real treatment event is required to derive the two starts")
    return {"l1": before_action, "l2": before_observing}


def _settle(
    invoke: Invoke, status: Callable[[], PublicStatus], region: tuple[int, int]
) -> float:
    advance_public(invoke, status())
    return max(row["standing_water_mm"] for row in _read(invoke, region))


def _verify_slice(
    doc: JsonObject, checkpoint: JsonObject, output: JsonObject, parent: JsonObject
) -> None:
    if (
        doc["parent_fingerprint"] != fingerprint(parent)
        or doc["checkpoint_fingerprint"] != fingerprint(checkpoint["state"])
        or output["slice_fingerprint"] != fingerprint(doc)
    ):
        raise ValueError("Flood slice identity differs")
    replay(checkpoint)
    runtime, _ = replay(output["run"])
    delay, _ = replay(output["zero"])
    prefix = checkpoint["state"]["events"]
    assert_equivalent(parent["state"]["events"][: len(prefix)], prefix)
    for result in (runtime, delay):
        assert_equivalent(result.export_state()["events"][: len(prefix)], prefix)
        if any(
            e.payload["tool"] not in SLICE_TOOLS
            for e in result.events[len(prefix) :]
            if e.kind == "operation_requested"
        ):
            raise ValueError("Flood slice request outside the task scope")
    if runtime.now != delay.now or runtime.now.date().isoformat() != doc["verify_day"]:
        raise ValueError("Flood slice endpoints have different observation days")
    scope = window(TARGET)
    measured = []
    for branch, expected_drains in ((runtime, 1), (delay, 0)):
        turns = [
            turn for turn in public_history(branch) if turn["event_seq"] > len(prefix)
        ]
        drains = [
            turn
            for turn in turns
            if turn["request"]["tool"] == "drain_surface" and turn["result"]["ok"]
        ]
        if len(drains) != expected_drains or any(
            any(
                turn["request"]["arguments"][key] != value
                for key, value in scope.items()
            )
            for turn in drains
        ):
            raise ValueError("Flood branch drained an unexpected target")
        last = turns[-1]
        if (
            last["request"] != {"tool": "observe_surface_water", "arguments": scope}
            or not last["result"]["ok"]
        ):
            raise ValueError("Flood branch lacks a same-target follow-up observation")
        measured.append(
            max(
                row["standing_water_mm"]
                for row in last["result"]["data"]["receipt"]["observations"]
            )
        )
    drained, control = measured
    if (
        drained != output["next_day_drained_mm"]
        or control != output["next_day_control_mm"]
        or control - drained < 5
        or not output["task_success"]
    ):
        raise ValueError("No verified reduction in standing water")


def build_flood(archive: JsonObject, out: Path) -> None:
    """Qualify reduced crop exposure under one fixed historical flood event."""
    if archive.get("requested_point") != list(POINT) or archive["timezone"] != TIMEZONE:
        raise ValueError("The flood fixture requires the Harbin weather archive")
    document = make_regional_document(
        archive,
        region="harbin",
        start=date(1994, 5, 5),
        end=date(1994, 10, 22),
        kind="flooding",
        farm=FarmSpec(length_m=268, width_m=64 * 1.1, initial_vwc=0.30),
        season=SeasonSpec(ponding_zones=((*TARGET, 30, 4),)),
        quota_mm=0,
        exposure=(date(1994, 7, 13), date(1994, 7, 22)),
    )
    spec = compile_season(document, archive)
    control, control_trace = _full_run(spec, drain=False)
    parent, public_trace = _full_run(spec, drain=True)
    for record in (control, parent):
        verify_season(document, archive, record)
        if not record["metrics"]["task_success"]:
            raise ValueError("Flood comparison did not complete the whole season")
        if any(
            abs(e["payload"]["flux"]["balance_error_mm"]) > 1e-8
            for e in record["state"]["events"]
            if e["kind"] == "environment_day_completed"
        ):
            raise ValueError("Flood run violated the daily water balance")
    first = next(
        e
        for e in parent["state"]["events"]
        if e["kind"] == "operation_requested"
        and e["payload"]["tool"] == "drain_surface"
    )
    start = first["time"][:10]
    end = (date.fromisoformat(start) + timedelta(days=1)).isoformat()
    zero_days, treated_days = (
        max(row["max_waterlogging_days"] for row in trace["daily"])
        for trace in (control_trace, public_trace)
    )
    recovered = (
        parent["metrics"]["stored_dry_grain_kg"]
        - control["metrics"]["stored_dry_grain_kg"]
    )
    event_day = (date.fromisoformat(start) - timedelta(days=1)).isoformat()
    exposure = next(row for row in control_trace["daily"] if row["day"] == event_day)
    rain = archive["daily"][event_day]["rain_mm"]
    if (
        rain <= 50
        or "R5" not in exposure["stages"]
        or treated_days >= zero_days
        or recovered <= 0
    ):
        raise ValueError(
            "Flood scenario did not demonstrate reproductive-stage injury and relief"
        )
    files = {
        "l3": document,
        "control": control,
        "public": parent,
        "control-exposure": control_trace,
        "public-exposure": public_trace,
    }
    evidence = {}
    for level, checkpoint in _cuts(parent, start).items():
        planned = list(TARGET) if level == "l1" else None
        doc = {
            "level": level.upper(),
            "parent_fingerprint": fingerprint(parent),
            "checkpoint_fingerprint": fingerprint(checkpoint["state"]),
            "target": planned,
            "verify_day": end,
            "tools": list(SLICE_TOOLS),
            "task": (
                "Recheck and drain the confirmed flooded zone, then verify at the next daily settlement."
                if planned
                else "Locate the flooded zone using public observations, drain it and recheck."
            ),
        }
        live = Runtime.from_state(checkpoint["state"])
        selected = _respond(_operator(live), TARGET if planned else None)
        if selected != TARGET:
            raise ValueError("Public observations did not isolate the affected zone")
        restored = Runtime.from_state(live.export_state())
        delay = Runtime.from_state(checkpoint["state"])
        drained, compared = [
            _settle(_operator(run), run.public_status, selected)
            for run in (restored, delay)
        ]
        assert drained == _settle(_operator(live), live.public_status, selected)
        assert_equivalent(live.export_state(), restored.export_state())
        restored.finish("flood_slice_drained")
        delay.finish("flood_slice_waited")
        output = {
            "task_success": drained + 5 <= compared,
            "next_day_drained_mm": drained,
            "next_day_control_mm": compared,
            "slice_fingerprint": fingerprint(doc),
            "run": artifact(restored),
            "zero": artifact(delay),
        }
        _verify_slice(doc, checkpoint, output, parent)
        files.update(
            {level: doc, f"{level}-start": checkpoint, f"{level}-public": output}
        )
        evidence[level] = {
            "next_day_drained_mm": drained,
            "next_day_control_mm": compared,
        }
    files["qualification"] = {
        "historical_rain_mm": rain,
        "rain_day": event_day,
        "crop_stages_on_rain_day": exposure["stages"],
        "first_response_date": start,
        "waterlogging_days_control": zero_days,
        "waterlogging_days_drained": treated_days,
        "added_stored_dry_kg": recovered,
        "slices": evidence,
    }
    out.mkdir(parents=True, exist_ok=False)
    for name, payload in files.items():
        write_json(out / f"{name}.json", payload)
