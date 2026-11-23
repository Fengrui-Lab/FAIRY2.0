"""A real wet East lot inherited after completing the West half."""

from datetime import datetime
from math import isclose

from fairy.acps.ontology.contracts import JsonObject
from fairy.events.artifacts import artifact, assert_equivalent, replay
from fairy.events.records import fingerprint
from fairy.events.session import Runtime

from evaluate.experiments.historical.operations import (
    call,
    grow_to_harvest,
    next_day,
    require,
    window,
)
from evaluate.scenarios.historical.compiler import compile_season
from evaluate.experiments.historical.cases.wet_harvest import (
    _invoke,
    loaded_batch_operations,
    public_batch_harvest,
    store_loaded_batch,
    unload_harvest,
)


def _inventory(run: Runtime) -> JsonObject:
    return require(call(run, "inspect_inventory"))["receipt"]


def qualify_east_handoff(
    doc: JsonObject, archive: JsonObject, *, backend: str
) -> dict[str, JsonObject]:
    """Cut decision and execution tasks from the same public L3 handoff."""
    spec = compile_season(doc, archive, backend=backend)
    run, _ = grow_to_harvest(spec, backend=backend)
    invoke = _invoke(run)
    if (
        public_batch_harvest(invoke, run.public_status, 0, 31, run.deadline)
        != "harvest_batches_complete"
    ):
        raise ValueError("West half must finish storage before the East handoff")
    while run.now < run.deadline:
        if run.now.hour < 18:
            harvest = invoke("harvest", **window((32, 35)))
            if harvest.ok:
                unload_harvest(invoke, require(harvest)["receipt"], _inventory(run))
                break
            if harvest.error != "FIELD_NOT_TRAFFICABLE":
                require(harvest)
        next_day(run)
    else:
        raise ValueError("No trafficable wet East harvest window")
    l2_start = artifact(run)
    start = _inventory(run)
    l1_start = artifact(run)
    warehouse = start["warehouse_facility"]
    warehouse_id = warehouse["object_id"]
    west = {
        b["batch_id"]: b
        for b in start["grain_batches"]
        if b["location_id"] == warehouse_id
    }
    [east] = [
        b
        for b in start["grain_batches"]
        if b["location_id"] == start["trailer_facility"]["object_id"]
    ]
    if {i for b in west.values() for i in b["ridges"]} != set(range(32)) or east[
        "ridges"
    ] != list(range(32, 36)):
        raise ValueError(
            "The East checkpoint must inherit exactly the stored West half and one eastern lot"
        )
    if east["moisture"] <= warehouse["safe_moisture"]:
        raise ValueError("The inherited East lot is already safe without drying")
    dry, dry_args, store, store_args = loaded_batch_operations(start)
    if store_loaded_batch(invoke) is not None:
        raise ValueError("The East reference batch did not fit after drying")
    if (
        public_batch_harvest(invoke, run.public_status, 36, 63, run.deadline)
        != "harvest_batches_complete"
    ):
        raise ValueError("The East reference season did not complete")
    run.finish("west_east_grain_handoff_complete")
    parent = artifact(run)
    if not parent["metrics"]["task_success"]:
        raise ValueError("The West/East full season did not safely store all grain")
    replay(parent)

    files = {"east-l3": doc, "east-run": parent}
    for level, checkpoint in (("l1", l1_start), ("l2", l2_start)):
        resumed = Runtime.from_state(checkpoint["state"])
        replay(checkpoint)
        request = _invoke(resumed)
        if level == "l2":
            if store_loaded_batch(request) is not None:
                raise ValueError(
                    "Public East diagnosis failed to store the selected batch"
                )
        else:
            require(
                request(
                    dry,
                    target_moisture=min(0.13, warehouse["safe_moisture"]),
                    **dry_args,
                )
            )
            require(request(store, **store_args))
        final = _inventory(resumed)
        by_id = {lot["batch_id"]: lot for lot in final["grain_batches"]}
        assert_equivalent(west, {key: by_id[key] for key in west})
        closed = by_id[east["batch_id"]]
        if (
            set(by_id) != {*west, east["batch_id"]}
            or closed["location_id"] != warehouse_id
            or not isclose(closed["dry_kg"], east["dry_kg"], abs_tol=1e-9)
            or closed["moisture"] > warehouse["safe_moisture"]
            or closed["water_kg"] >= east["water_kg"]
        ):
            raise ValueError("The East slice failed safe, conserving custody transfer")
        resumed.finish("east_wet_batch_safely_stored")
        result = artifact(resumed)
        replay(result)
        prefix = checkpoint["state"]["events"]
        assert_equivalent(prefix, parent["state"]["events"][: len(prefix)])
        assert_equivalent(prefix, result["state"]["events"][: len(prefix)])
        files[f"east-{level}"] = {
            "level": level.upper(),
            "parent_fingerprint": fingerprint(parent),
            "checkpoint_fingerprint": fingerprint(checkpoint["state"]),
            "through_event_seq": len(prefix),
            "task": (
                "Identify the pending East lot, inspect facility capacity and complete safe storage."
                if level == "l2"
                else "Dry and safely store the confirmed East lot."
            ),
            **({"target_batch_id": east["batch_id"]} if level == "l1" else {}),
        }
        files[f"east-{level}-start"] = checkpoint
        files[f"east-{level}-public"] = {
            "run": result,
            "task_success": True,
            "batch_id": east["batch_id"],
            "removed_water_kg": east["water_kg"] - closed["water_kg"],
        }

    refused = Runtime.from_state(l2_start["state"])
    before = refused.export_state()
    for args, error in (
        ({**store_args, "batch_id": next(iter(west))}, "BATCH_NOT_AT_SOURCE"),
        (store_args, "UNSAFE_STORAGE_MOISTURE"),
    ):
        if call(refused, store, **args).error != error:
            raise ValueError("Invalid East handoff bypass was not rejected")
    assert_equivalent(
        {k: v for k, v in before.items() if k not in ("events", "tool_errors")},
        {
            k: v
            for k, v in refused.export_state().items()
            if k not in ("events", "tool_errors")
        },
    )
    refused.finish("east_handoff_invalid_transfers_rejected")
    files["east-control"] = artifact(refused)
    replay(files["east-control"])
    files["east-qualification"] = {
        "checkpoint_time": datetime.fromisoformat(l2_start["state"]["now"]).isoformat(),
        "west_batches": len(west),
        "west_ridges": 32,
        "east_batch_id": east["batch_id"],
        "east_ridges": east["ridges"],
        "east_initial_moisture": east["moisture"],
        "initial_warehouse_remaining_kg": warehouse["remaining_kg"],
        "warehouse_capacity_kg": warehouse["capacity_kg"],
        "final_stored_wet_kg": parent["metrics"]["stored_grain_kg"],
        "removed_water_kg": files["east-l1-public"]["removed_water_kg"],
    }
    return files
