"""A batch-triggered dryer outage and a verified public repair path."""

from datetime import datetime
from pathlib import Path

from evaluate.experiments.historical.cases.wet_harvest import (
    public_batch_harvest,
    loaded_batch_operations,
    store_loaded_batch,
)
from evaluate.experiments.historical.operations import (
    Invoke,
    call,
    grow_to_harvest,
    require,
    verify_season,
)
from evaluate.scenarios.historical.compiler import compile_season
from evaluate.scenarios.historical.source import load_historical_source
from evaluate.scenarios.spec import ScenarioSpec
from fairy.acps.ontology.contracts import JsonObject, OperationRequest, OperationResult
from fairy.agents.loop.context.history import public_history
from fairy.events.artifacts import artifact, assert_equivalent, replay, write_json
from fairy.events.records import fingerprint
from fairy.events.session import Runtime

TOOLS = (
    "inspect_inventory",
    "inspect_dryer",
    "repair_dryer",
    "dry_batch",
    "store_batch",
)


def _invoke(runtime: Runtime) -> Invoke:
    def invoke(name: str, **args: object) -> OperationResult:
        return call(runtime, name, **args)

    return invoke


def _season(spec: ScenarioSpec, *, repair: bool, backend: str = "native") -> JsonObject:
    runtime, _ = grow_to_harvest(spec, backend=backend)
    reason = public_batch_harvest(
        _invoke(runtime),
        runtime.public_status,
        0,
        63,
        runtime.deadline,
        maintain_dryer=repair,
    )
    runtime.finish(reason)
    return artifact(
        runtime, {"kind": "public_dryer_repair_rule" if repair else "no_repair_control"}
    )


def _prefix(parent: JsonObject, anchor: int) -> Runtime:
    state = parent["state"]
    runtime = Runtime(ScenarioSpec.from_dict(state["scenario"]), state["backend"])
    for event in state["events"][: anchor - 1]:
        if event["kind"] == "operation_requested":
            payload = event["payload"]
            runtime.invoke(
                OperationRequest(payload["tool"], payload["arguments"]),
                actor=payload["actor"],
            )
    assert_equivalent(runtime.export_state()["events"], state["events"][: anchor - 1])
    return runtime


def _slice(parent: JsonObject, initial: Runtime, level: str) -> dict[str, JsonObject]:
    if level not in ("L1", "L2"):
        raise ValueError("Unknown equipment task level")
    checkpoint = artifact(
        initial, {"kind": "dryer_task_prefix", "parent": fingerprint(parent)}
    )
    restored = Runtime.from_state(checkpoint["state"])
    initial_inventory = require(_invoke(restored)("inspect_inventory"))["receipt"]
    # Resume an actual wet batch inherited from the parent, without harvesting fresh grain.
    if store_loaded_batch(_invoke(restored), maintain_dryer=True) is not None:
        raise ValueError("Repair slice failed to store its inherited batch")
    restored.finish("dryer_batch_safely_stored")
    result = artifact(restored)
    replay(checkpoint)
    verified, _ = replay(result)
    prefix = checkpoint["state"]["events"]
    assert_equivalent(result["state"]["events"][: len(prefix)], prefix)
    turns = [t for t in public_history(verified) if t["event_seq"] > len(prefix)]
    if any(t["request"]["tool"] not in TOOLS or not t["result"]["ok"] for t in turns):
        raise ValueError("Unexpected equipment slice operation")
    repairs = [t for t in turns if t["request"]["tool"] == "repair_dryer"]
    if len(repairs) != (1 if level == "L2" else 0):
        raise ValueError("Equipment slice used a different recovery path")
    final = turns[-1]["result"]["data"]["receipt"]
    prior = initial_inventory["stored_batches"]
    stored = final["stored_batches"]
    assert_equivalent(stored[: len(prior)], prior)
    if (
        len(stored) != len(prior) + 1
        or stored[-1]["moisture"] > initial.season_config.safe_moisture
        or final["trailer"]["dry_kg"]
        or final["trailer"]["water_kg"]
    ):
        raise ValueError("The inherited batch was not safely stored")
    assert_equivalent(initial_inventory["trailer"]["dry_kg"], stored[-1]["dry_kg"])
    old_lots = {lot["batch_id"]: lot for lot in initial_inventory["grain_batches"]}
    new_lots = {lot["batch_id"]: lot for lot in final["grain_batches"]}
    trailer = initial_inventory["trailer_facility"]["object_id"]
    [lot] = [b for b in old_lots.values() if b["location_id"] == trailer]
    key = lot["batch_id"]
    assert_equivalent(
        {k: b for k, b in old_lots.items() if k != key},
        {k: b for k, b in new_lots.items() if k != key},
    )
    if new_lots[key]["location_id"] != final["warehouse_facility"]["object_id"]:
        raise ValueError("The inherited wet lot did not enter warehouse custody")

    document = {
        "level": level,
        "tools": list(TOOLS),
        "through_event_seq": len(prefix),
        "parent_fingerprint": fingerprint(parent),
        "checkpoint_fingerprint": fingerprint(checkpoint["state"]),
        "task": (
            "Diagnose the equipment fault and safely store the inherited wet batch."
            if level == "L2"
            else "Complete drying and storage for the ready inherited batch."
        ),
    }
    stem = level.lower()
    return {
        stem: document,
        f"{stem}-start": checkpoint,
        f"{stem}-public": {
            "task_success": True,
            "run": result,
            "slice_fingerprint": fingerprint(document),
            "stored_batch": stored[-1],
            "repair_calls": len(repairs),
        },
    }


def build_dryer_failure(
    archive: JsonObject, out: Path, *, backend: str = "native"
) -> None:
    """Generate a one-time lockout after the first dried batch enters storage."""
    doc = load_historical_source("harbin_dryer_failure_2016", archive)
    spec = compile_season(doc, archive, backend=backend)
    repaired = _season(spec, repair=True, backend=backend)
    no_repair = _season(spec, repair=False, backend=backend)
    for record in (repaired, no_repair):
        verify_season(doc, archive, record)
    if not repaired["metrics"]["task_success"] or no_repair["metrics"]["task_success"]:
        raise ValueError(
            "Dryer control failed to separate safe storage from equipment blockage"
        )

    complete, _ = replay(repaired)
    interrupted, _ = replay(no_repair)
    if public_history(interrupted)[-1]["result"]["error"] != "DRYER_OUT_OF_SERVICE":
        raise ValueError("The no-repair branch did not reach the equipment refusal")
    requests = [e for e in complete.events if e.kind == "operation_requested"]
    [service] = [e for e in requests if e.payload["tool"] == "repair_dryer"]
    repair_result = next(
        e
        for e in complete.events
        if e.cause == service.seq and e.kind == "operation_result"
    )
    inspect = next(
        e
        for e in reversed(complete.events[: service.seq - 1])
        if e.kind == "operation_result"
        and e.payload.get("data", {}).get("receipt", {}).get("fault") == "BATCH_LOCKOUT"
    )
    assert inspect.cause is not None
    drying = next(
        e for e in requests if e.seq > service.seq and e.payload["tool"] == "dry_batch"
    )
    broken = _prefix(repaired, inspect.cause)
    ready = _prefix(repaired, drying.seq)
    broken_state = broken.export_state()
    defect = broken.season_physical.dryer_observation()
    if (
        defect["availability"] != "out_of_service"
        or len(broken.season_physical.inventory()["stored_batches"]) != 1
    ):
        raise ValueError(
            "Fault was expected between the first stored batch and the next drying job"
        )
    hours = (
        datetime.fromisoformat(repair_result.time)
        - datetime.fromisoformat(service.time)
    ).total_seconds() / 3600
    if (
        hours != defect["repair_hours"]
        or ready.season_physical.dryer_observation()["availability"] != "ready"
    ):
        raise ValueError("Equipment recovery failed to follow the modelled repair time")
    assert_equivalent(
        broken.season_physical.inventory()["trailer"],
        ready.season_physical.inventory()["trailer"],
    )

    wait = Runtime.from_state(broken_state)
    invoke = _invoke(wait)
    require(invoke("advance_time", hours=hours))
    dry, dry_args, store, store_args = loaded_batch_operations(
        wait.season_physical.inventory()
    )
    if invoke(dry, target_moisture=0.13, **dry_args).error != "DRYER_OUT_OF_SERVICE":
        raise ValueError("Waiting unexpectedly cleared the equipment fault")
    if invoke(store, **store_args).error != "UNSAFE_STORAGE_MOISTURE":
        raise ValueError("Wet grain could bypass required drying")
    assert_equivalent(
        wait.season_physical.inventory()["trailer"],
        broken.season_physical.inventory()["trailer"],
    )
    wait.finish("wait_without_dryer_service")
    wait_control = artifact(wait)
    replay(wait_control)

    files = {
        "l3": doc,
        "repaired": repaired,
        "no-repair-control": no_repair,
        "wait-control": wait_control,
        **_slice(repaired, ready, "L1"),
        **_slice(repaired, broken, "L2"),
    }
    files["qualification"] = {
        "first_stored_batch_kg": broken.season_physical.inventory()["stored_batches"][
            0
        ],
        "observed_fault_at": inspect.time,
        "repair_started": service.time,
        "repair_completed": repair_result.time,
        "repair_hours": hours,
        "no_repair_dry_error": "DRYER_OUT_OF_SERVICE",
        "wet_storage_error": "UNSAFE_STORAGE_MOISTURE",
        "stored_dry_kg_repaired": repaired["metrics"]["stored_dry_grain_kg"],
        "stored_dry_kg_without_repair": no_repair["metrics"]["stored_dry_grain_kg"],
        "repaired_ridges": repaired["metrics"]["harvested_ridges"],
    }
    out.mkdir(parents=True, exist_ok=False)
    for name, value in files.items():
        write_json(out / f"{name}.json", value)
