"""Cold-spring planting with an observed decision window and a matched refused action."""

from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable

from evaluate.scenarios.historical_scenarios import (
    FULL,
    Invoke,
    close_harvest,
    compile_season,
    grow_to_harvest,
    make_regional_document,
    require,
    verify_season,
)
from evaluate.scenarios.historical_slices import advance_public
from evaluate.scenarios.historical_weather import POINT, TIMEZONE
from evaluate.scenarios.season import SeasonSpec
from fairy.acps.ontology.contracts import JsonObject, OperationRequest
from fairy.acps.ontology.farm.spec import FarmSpec
from fairy.agents.loop.context.history import public_history
from fairy.events.artifacts import artifact, assert_equivalent, replay, write_json
from fairy.events.records import PublicStatus, fingerprint
from fairy.events.session import Runtime

TOOLS = (
    "observe_seedbed_range",
    "prepare_seedbed",
    "plant",
    "inspect_crop",
    "inspect_inventory",
    "advance_time",
)


def invoke_for(runtime: Runtime) -> Invoke:
    def invoke(tool: str, **args: object):
        return runtime.invoke(OperationRequest(tool, args), actor="public_seedbed_rule")

    return invoke


def _seedbed(invoke: Invoke) -> JsonObject:
    return require(invoke("observe_seedbed_range", **FULL))["receipt"]


def _ready(reading: JsonObject) -> bool:
    return reading["settled"] and all(
        row["temperature_ready"] for row in reading["observations"]
    )


def plant_and_check(
    invoke: Invoke, status: Callable[[], PublicStatus], *, last_plant_day: date
) -> str:
    """Wait on public seedbed observations, then check actual emergence."""
    while date.fromisoformat(status()["time"][:10]) <= last_plant_day:
        bed = _seedbed(invoke)
        if not _ready(bed):
            advance_public(invoke, status())
            continue
        inventory = require(invoke("inspect_inventory"))["receipt"]
        if len(inventory["prepared_ridges"]) != 64:
            work = invoke("prepare_seedbed", **FULL)
            if work.error == "FIELD_NOT_TRAFFICABLE":
                advance_public(invoke, status())
                continue
            require(work)
        work = invoke("plant", **FULL, spacing_m=0.079, depth_mm=40)
        if work.error in ("FIELD_NOT_TRAFFICABLE", "SEEDBED_TOO_COLD"):
            advance_public(invoke, status())
            continue
        require(work)
        planted = date.fromisoformat(status()["time"][:10])
        break
    else:
        raise ValueError("No usable seedbed in the planting window")

    for _ in range(22):
        crop = require(invoke("inspect_crop", **FULL))["receipt"]["observations"]
        if all(row["stage"] not in ("unplanted", "pre_emergence") for row in crop):
            return status()["time"][:10]
        if date.fromisoformat(status()["time"][:10]) >= planted + timedelta(days=21):
            break
        advance_public(invoke, status())
    raise ValueError("Public observations did not confirm emergence within 21 days")


def _prefix(parent: JsonObject, request_seq: int) -> Runtime:
    spec, _ = replay(parent)
    runtime = Runtime(spec.scenario, spec.backend_name)
    for event in spec.events[: request_seq - 1]:
        if event.kind == "operation_requested":
            body = event.payload
            runtime.invoke(
                OperationRequest(body["tool"], body["arguments"]), actor=body["actor"]
            )
    assert_equivalent(
        parent["state"]["events"][: request_seq - 1], runtime.export_state()["events"]
    )
    return runtime


def _slice(parent: JsonObject, state: Runtime, level: str, last_plant_day: date):
    """Run the planting task from the same observed history and verify both artifacts."""
    checkpoint = artifact(
        state, {"kind": "planting_prefix", "parent": fingerprint(parent)}
    )
    restored, _ = replay(checkpoint)
    day = plant_and_check(
        invoke_for(restored), restored.public_status, last_plant_day=last_plant_day
    )
    restored.finish("emergence_confirmed")
    result = artifact(restored, {"kind": "public_seedbed_rule"})
    verified, _ = replay(result)
    prefix = checkpoint["state"]["events"]
    assert_equivalent(prefix, result["state"]["events"][: len(prefix)])
    turns = [t for t in public_history(verified) if t["event_seq"] > len(prefix)]
    if any(t["request"]["tool"] not in TOOLS for t in turns):
        raise ValueError("Planting slice used an out-of-scope operation")
    plant = next(
        t for t in turns if t["request"]["tool"] == "plant" and t["result"]["ok"]
    )
    if not any(
        t["request"]["tool"] == "observe_seedbed_range"
        and t["result"]["ok"]
        and t["time"][:10] == plant["time"][:10]
        and t["event_seq"] < plant["event_seq"]
        and _ready(t["result"]["data"]["receipt"])
        for t in turns
    ):
        raise ValueError(
            "Successful planting lacked a same-day public soil observation"
        )
    final = turns[-1]["result"]["data"]["receipt"]["observations"]
    if len(final) != 64 or any(
        r["stage"] in ("unplanted", "pre_emergence") for r in final
    ):
        raise ValueError("Slice did not confirm the required emergence")
    doc = {
        "level": level,
        "tools": list(TOOLS),
        "source_trace_fingerprint": fingerprint(parent),
        "through_event_seq": len(prefix),
        "checkpoint_fingerprint": fingerprint(checkpoint["state"]),
        "start": state.now.isoformat(),
        "last_plant_day": last_plant_day.isoformat(),
        "max_days_to_emergence": 21,
        "task": (
            "Plant in the confirmed suitable seedbed window and confirm emergence."
            if level == "L1"
            else "Use seedbed readings to select a planting window, then confirm emergence."
        ),
    }
    return (
        doc,
        checkpoint,
        {
            "run": result,
            "slice_fingerprint": fingerprint(doc),
            "task_success": True,
            "emergence_date": day,
            "planted_date": plant["time"][:10],
        },
    )


def build_cold_planting(
    archive: JsonObject, out: Path, *, backend: str = "native"
) -> None:
    """Qualify a historical cold wait, a refused premature action, and actual emergence."""
    if archive.get("requested_point") != list(POINT) or archive["timezone"] != TIMEZONE:
        raise ValueError("The cold-spring fixture requires the Harbin weather archive")
    start, deadline = date(1995, 5, 1), date(1995, 10, 22)
    last_plant = date(1995, 5, 22)
    document = make_regional_document(
        archive,
        region="harbin",
        start=start,
        end=deadline,
        kind="cold_planting",
        farm=FarmSpec(length_m=268, width_m=64 * 1.1, initial_vwc=0.30),
        season=SeasonSpec(),
        quota_mm=0,
        exposure=(date(1995, 5, 2), date(1995, 5, 8)),
    )
    spec = compile_season(document, archive, backend=backend)
    season = Runtime(spec, backend)
    emerged = plant_and_check(
        invoke_for(season), season.public_status, last_plant_day=last_plant
    )
    season, _ = grow_to_harvest(spec, planted_runtime=season)
    close_harvest(season)
    parent = artifact(season, {"kind": "cold_planting_full_season"})
    verify_season(document, archive, parent)
    if not parent["metrics"]["task_success"]:
        raise ValueError("The cold-spring crop did not finish the season")
    turns = public_history(season)
    cold = next(
        t
        for t in turns
        if t["request"]["tool"] == "observe_seedbed_range"
        and t["result"]["data"]["receipt"]["settled"]
        and not _ready(t["result"]["data"]["receipt"])
    )
    planting = next(
        t for t in turns if t["request"]["tool"] == "plant" and t["result"]["ok"]
    )
    causes = {e.seq: e.cause for e in season.events}

    cold_cause = causes[cold["event_seq"]]
    plant_cause = causes[planting["event_seq"]]
    if cold_cause is None or plant_cause is None:
        raise ValueError("Planting slice lacks causal request provenance")
    early_checkpoint = _prefix(parent, cold_cause)
    early = Runtime.from_state(early_checkpoint.export_state())
    invoke = invoke_for(early)
    cold_bed = _seedbed(invoke)
    require(invoke("prepare_seedbed", **FULL))
    before = early.export_state()
    refusal = invoke("plant", **FULL, spacing_m=0.079, depth_mm=40)
    if refusal.error != "SEEDBED_TOO_COLD":
        raise ValueError("Premature planting did not reach the cold seedbed gate")
    after = early.export_state()
    assert_equivalent(
        {
            key: val
            for key, val in before.items()
            if key not in ("events", "tool_errors")
        },
        {
            key: val
            for key, val in after.items()
            if key not in ("events", "tool_errors")
        },
    )
    early.finish("cold_planting_rejected")
    negative = artifact(early)
    verify_season(document, archive, negative)
    ready_checkpoint = _prefix(parent, plant_cause)
    warm_bed = _seedbed(invoke_for(Runtime.from_state(ready_checkpoint.export_state())))
    files = {"l3": document, "run": parent, "early-planting-control": negative}
    for level, checkpoint in (("L1", ready_checkpoint), ("L2", early_checkpoint)):
        cut, state, result = _slice(parent, checkpoint, level, last_plant)
        stem = level.lower()
        files.update({stem: cut, f"{stem}-start": state, f"{stem}-public": result})
    files["qualification"] = {
        "cold_date": cold["time"][:10],
        "cold_seedbed_c": cold_bed["observations"][0]["seedbed_temperature_c"],
        "warm_seedbed_c": warm_bed["observations"][0]["seedbed_temperature_c"],
        "plant_date": planting["time"][:10],
        "emergence_date": emerged,
        "premature_planting_error": refusal.error,
        "stored_dry_kg": parent["metrics"]["stored_dry_grain_kg"],
    }
    out.mkdir(parents=True, exist_ok=False)
    for name, payload in files.items():
        write_json(out / f"{name}.json", payload)
