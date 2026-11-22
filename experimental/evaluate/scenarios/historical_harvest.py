"""Build and verify the historical wet-harvest and capacity decisions."""

from datetime import date, datetime, timedelta
from math import isclose
from pathlib import Path
from typing import Callable

from evaluate.scenarios.historical_scenarios import (
    Invoke,
    boundary,
    compile_season,
    grow_to_harvest,
    next_day,
    make_regional_document,
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

TOOLS = (
    "inspect_crop",
    "inspect_inventory",
    "get_current_weather",
    "harvest",
    "unload",
    "dry_grain",
    "store_grain",
    "advance_time",
)
SAFE = 0.135


def weight(batch: JsonObject) -> float:
    return batch["dry_kg"] + batch["water_kg"]


def store_loaded_batch(invoke: Invoke, *, maintain_dryer: bool = False) -> str | None:
    """Close the loaded trailer batch from public moisture and equipment receipts."""
    inventory = require(invoke("inspect_inventory"))["receipt"]
    if inventory["trailer"]["moisture"] > SAFE:
        if maintain_dryer:
            condition = require(invoke("inspect_dryer"))["receipt"]
            if condition["availability"] == "out_of_service":
                require(invoke("repair_dryer"))
        drying = invoke("dry_grain", target_moisture=0.13)
        if drying.error == "DRYER_OUT_OF_SERVICE":
            return "dryer_out_of_service"
        require(drying)
    transfer = invoke("store_grain")
    if transfer.error == "STORAGE_CAPACITY_EXCEEDED":
        return "storage_capacity_exceeded"
    require(transfer)
    require(invoke("inspect_inventory"))
    return None


def public_batch_harvest(
    invoke: Invoke,
    status: Callable[[], PublicStatus],
    first: int,
    last: int,
    deadline: datetime,
    *,
    maintain_dryer: bool = False,
) -> str:
    """Use crop and inventory receipts, with bounded retries for ordinary refusals."""
    width = 4
    while first <= last:
        now = datetime.fromisoformat(status()["time"])
        if now.date() >= deadline.date():
            return "harvest_deadline"
        if now.hour >= 18:
            advance_public(invoke, status())
            continue
        stop = min(last, first + width - 1)
        crop = require(invoke("inspect_crop", **window((first, stop))))["receipt"][
            "observations"
        ]
        if not all(row["harvestable"] for row in crop):
            advance_public(invoke, status())
            continue
        report = invoke("get_current_weather")
        if not report.ok and report.error != "WEATHER_UNAVAILABLE":
            require(report)
        inventory = require(invoke("inspect_inventory"))["receipt"]
        if weight(inventory["combine"]) or weight(inventory["trailer"]):
            return "containers_not_empty_at_batch_start"
        harvested = invoke("harvest", **window((first, stop)))
        if harvested.error == "FIELD_NOT_TRAFFICABLE":
            advance_public(invoke, status())
            continue
        if harvested.error == "COMBINE_CAPACITY_EXCEEDED" and width > 1:
            width = max(1, width // 2)
            continue
        require(harvested)
        require(invoke("unload"))
        failure = store_loaded_batch(invoke, maintain_dryer=maintain_dryer)
        if failure is not None:
            return failure
        first = stop + 1
    return "harvest_batches_complete"


def _invoke(runtime: Runtime) -> Invoke:
    def invoke(tool: str, **args: object):
        return runtime.invoke(OperationRequest(tool, args), actor="public_harvest_rule")

    return invoke


def _full_season(
    spec: ScenarioSpec, *, backend: str = "native", rain_trial_day: date | None = None
) -> JsonObject:
    runtime, _ = grow_to_harvest(spec, backend=backend)
    if rain_trial_day is not None:
        while runtime.now.date() < rain_trial_day:
            next_day(runtime)
    reason = public_batch_harvest(
        _invoke(runtime), runtime.public_status, 0, 63, runtime.deadline
    )
    runtime.finish(reason)
    return artifact(runtime, {"kind": "public_harvest_rule"})


def capture_harvest(parent: JsonObject, level: str) -> tuple[JsonObject, JsonObject]:
    """Find adjacent sides of a real weather refusal and derive verified prefixes."""
    if level not in ("L1", "L2"):
        raise ValueError("Expected L1 or L2")
    state = parent["state"]
    source, _ = replay(parent)
    turns = public_history(source)
    refusal = next(
        t
        for t in turns
        if t["request"]["tool"] == "harvest"
        and t["result"]["error"] == "FIELD_NOT_TRAFFICABLE"
    )
    success = next(
        t
        for t in turns
        if t["event_seq"] > refusal["event_seq"]
        and t["request"]["tool"] == "harvest"
        and t["result"]["ok"]
    )
    anchor = success if level == "L1" else refusal
    cause = next(e["cause"] for e in state["events"] if e["seq"] == anchor["event_seq"])
    request = anchor["request"]["arguments"]
    first = request["start_ridge"]
    last = request["end_ridge"] if level == "L1" else 63
    runtime = Runtime(ScenarioSpec.from_dict(state["scenario"]), state["backend"])
    for event in state["events"]:
        if event["seq"] >= cause:
            break
        if event["kind"] == "operation_requested":
            body = event["payload"]
            runtime.invoke(
                OperationRequest(body["tool"], body["arguments"]), actor=body["actor"]
            )
    prefix = runtime.export_state()
    assert_equivalent(state["events"][: cause - 1], prefix["events"])
    deadline = boundary(runtime.now.date() + timedelta(days=4), zone=runtime.zone)
    checkpoint = artifact(
        runtime, {"kind": "harvest_prefix", "parent": fingerprint(parent)}
    )
    document = {
        "schema": 1,
        "level": level,
        "parent_fingerprint": fingerprint(parent),
        "scenario_fingerprint": prefix["scenario_fingerprint"],
        "checkpoint_fingerprint": fingerprint(prefix),
        "initial_time": runtime.now.isoformat(),
        "through_event_seq": cause - 1,
        "deadline": min(deadline, runtime.deadline).isoformat(),
        "target": [first, last],
        "tools": list(TOOLS),
        "task": (
            "Harvest the ready ridge batch and store it safely."
            if level == "L1"
            else "Manage the remaining mature ridges through weather constraints and safe storage."
        ),
    }
    return document, checkpoint


def score_harvest_slice(
    document: JsonObject, checkpoint: JsonObject, runtime: Runtime
) -> JsonObject:
    """Check spatial scope, cleared containers, mass balance and new safe storage."""
    start = checkpoint["state"]
    stop = runtime.export_state()
    if (
        document["checkpoint_fingerprint"] != fingerprint(start)
        or document["scenario_fingerprint"] != stop["scenario_fingerprint"]
    ):
        raise ValueError("Harvest slice has a different scenario or checkpoint")
    n = document["through_event_seq"]
    assert_equivalent(start["events"], stop["events"][:n])
    if any(
        e.payload["tool"] not in document["tools"]
        for e in runtime.events[n:]
        if e.kind == "operation_requested"
    ):
        raise ValueError("Operation outside the harvest slice scope")
    observed = Runtime.from_state(start)
    initial = observed.season_physical.audit()
    final = runtime.season_physical.audit()
    before = {
        i
        for i, ridge in enumerate(observed.season_backend.management_state())
        if ridge.harvested
    }
    after = {
        i
        for i, ridge in enumerate(runtime.season_backend.management_state())
        if ridge.harvested
    }
    low, high = document["target"]
    target = set(range(low, high + 1))
    prior_batches = initial["stored_batches"]
    assert_equivalent(prior_batches, final["stored_batches"][: len(prior_batches)])
    added_batches = final["stored_batches"][len(prior_batches) :]
    added_dry = sum(b["dry_kg"] for b in added_batches)
    safe = all(b["moisture"] <= SAFE + 1e-9 for b in added_batches)
    empty = not weight(final["combine"]) and not weight(final["trailer"])
    recovered = final["harvested_dry_kg"] - initial["harvested_dry_kg"]
    conserved = isclose(added_dry, recovered, abs_tol=1e-6)
    within_deadline = runtime.now <= datetime.fromisoformat(document["deadline"])
    errors = runtime.tool_errors - observed.tool_errors
    return {
        "task_success": after - before == target
        and added_dry > 0
        and safe
        and empty
        and conserved
        and within_deadline
        and (document["level"] == "L2" or errors == 0),
        "harvested_ridges": sorted(after - before),
        "new_safe_batches": len(added_batches) if safe else 0,
        "new_stored_dry_kg": added_dry,
        "tool_errors": errors,
    }


def verify_harvest_slice(
    document: JsonObject, checkpoint: JsonObject, result: JsonObject, parent: JsonObject
) -> JsonObject:
    """Check that the persisted slice was derived from this parent and replays exactly."""
    expected_doc, expected_checkpoint = capture_harvest(parent, document["level"])
    assert_equivalent(expected_doc, document)
    assert_equivalent(expected_checkpoint["state"], checkpoint["state"])
    if result["slice_fingerprint"] != fingerprint(document):
        raise ValueError("Harvest slice declaration was changed")
    replay(checkpoint)
    runtime, _ = replay(result["run"])
    metrics = score_harvest_slice(document, checkpoint, runtime)
    assert_equivalent(metrics, result["metrics"])
    return metrics


def build_wet_harvest(
    archive: JsonObject, out: Path, *, backend: str = "native"
) -> None:
    """Qualify legal batching, oversized work and insufficient total storage."""
    if archive.get("requested_point") != list(POINT) or archive["timezone"] != TIMEZONE:
        raise ValueError("The wet-harvest fixture requires the Harbin weather archive")

    def document(region: str, capacity: float) -> JsonObject:
        return make_regional_document(
            archive,
            region=region,
            start=date(2016, 5, 5),
            end=date(2016, 10, 22),
            kind="wet_harvest",
            farm=FarmSpec(length_m=268, width_m=64 * 1.1, initial_vwc=0.30),
            season=SeasonSpec(storage_capacity_kg=capacity),
            quota_mm=0,
            exposure=(date(2016, 8, 4), date(2016, 8, 8)),
        )

    capacity, low_capacity_limit = 11000, 9000
    trial_day = None
    maturity = None
    original_window_rain = {}
    normal_doc = document("harbin", capacity)
    if backend == "wofost":
        spec = compile_season(normal_doc, archive, backend=backend)
        ready, _ = grow_to_harvest(spec, backend=backend)
        maturity = ready.now.date()
        trial_day = next(
            (
                date.fromisoformat(day.day)
                for day in spec.weather
                if maturity <= date.fromisoformat(day.day) < ready.deadline.date()
                and day.rain_mm > 10
            ),
            None,
        )
        original_window_rain = {
            day.day: day.rain_mm
            for day in spec.weather
            if "2016-08-04" <= day.day <= "2016-08-08"
        }
        if trial_day is None:
            raise ValueError("No documented trafficability rain in the mature window")
    parent = _full_season(
        compile_season(normal_doc, archive, backend=backend),
        backend=backend,
        rain_trial_day=trial_day,
    )
    if backend == "wofost":
        if not parent["metrics"]["task_success"]:
            raise ValueError(
                "The declared WOFOST warehouse could not hold the wet harvest"
            )
        low_capacity_limit = min(
            low_capacity_limit, int(parent["metrics"]["stored_dry_grain_kg"] * 0.85)
        )
    small_doc = document("harbin", low_capacity_limit)
    control = _full_season(
        compile_season(small_doc, archive, backend=backend),
        backend=backend,
        rain_trial_day=trial_day,
    )
    for doc, record in ((normal_doc, parent), (small_doc, control)):
        verify_season(doc, archive, record)
    run, _ = replay(parent)
    low_capacity, _ = replay(control)
    turns = public_history(run)
    refusals = [
        t["time"]
        for t in turns
        if t["request"]["tool"] == "harvest"
        and t["result"]["error"] == "FIELD_NOT_TRAFFICABLE"
    ]
    rain_at_refusal = {
        stamp: archive["daily"][stamp[:10]]["rain_mm"] for stamp in refusals
    }
    if (
        not any(rain > 10 for rain in rain_at_refusal.values())
        or not parent["metrics"]["task_success"]
        or control["metrics"]["task_success"]
    ):
        raise ValueError("Wet harvest qualification did not separate the two outcomes")
    blocked = public_history(low_capacity)[-1]
    if (
        blocked["request"]["tool"] != "store_grain"
        or blocked["result"]["error"] != "STORAGE_CAPACITY_EXCEEDED"
    ):
        raise ValueError("Low-capacity control encountered a different limit")
    if parent["metrics"]["stored_dry_grain_kg"] <= low_capacity_limit:
        raise ValueError(
            "Control capacity must be below the full season's dry-mass floor"
        )

    files = {
        "l3": normal_doc,
        "run": parent,
        "capacity-control-l3": small_doc,
        "capacity-control": control,
    }
    diagnostics = {}
    for level in ("L1", "L2"):
        cut, checkpoint = capture_harvest(parent, level)
        runtime = Runtime.from_state(checkpoint["state"])
        low, high = cut["target"]
        reason = public_batch_harvest(
            _invoke(runtime),
            runtime.public_status,
            low,
            high,
            datetime.fromisoformat(cut["deadline"]),
        )
        runtime.finish(reason)
        result = {
            "slice_fingerprint": fingerprint(cut),
            "run": artifact(runtime),
            "metrics": score_harvest_slice(cut, checkpoint, runtime),
        }
        if not verify_harvest_slice(cut, checkpoint, result, parent)["task_success"]:
            raise ValueError(f"{level} wet harvest slice did not qualify")
        stem = level.lower()
        files.update({stem: cut, f"{stem}-start": checkpoint, f"{stem}-public": result})
        diagnostics[level] = result["metrics"]

    l1 = Runtime.from_state(files["l1-start"]["state"])
    remaining = [
        i
        for i, row in enumerate(l1.season_backend.management_state())
        if not row.harvested
    ]
    wider = _invoke(l1)("harvest", **window((min(remaining), max(remaining))))
    if wider.error != "COMBINE_CAPACITY_EXCEEDED":
        raise ValueError(
            "Oversized batch control did not reach the combine capacity gate"
        )
    l1.finish("oversized_batch_rejected")
    files["oversized-batch-control"] = artifact(l1)
    replay(files["oversized-batch-control"])
    inventories = [
        t["result"]["data"]["receipt"]
        for t in turns
        if t["request"]["tool"] == "inspect_inventory" and t["result"]["ok"]
    ]
    peaks = {
        "combine_kg": max(
            weight(t["result"]["data"]["receipt"])
            for t in turns
            if t["request"]["tool"] == "harvest" and t["result"]["ok"]
        ),
        "trailer_kg": max(weight(row["trailer"]) for row in inventories),
        "warehouse_kg": max(
            sum(map(weight, row["stored_batches"])) for row in inventories
        ),
    }
    capacities = inventories[0]
    if any(
        value > capacities[field + "_capacity_kg"] + 1e-9
        for field, value in (
            ("combine", peaks["combine_kg"]),
            ("trailer", peaks["trailer_kg"]),
            ("storage", peaks["warehouse_kg"]),
        )
    ):
        raise ValueError("Stored run exceeded a physical capacity")
    files["qualification"] = {
        "rain_rejections_mm": rain_at_refusal,
        "peak_wet_mass_kg": peaks,
        "warehouse_wet_capacity_kg": capacity,
        **(
            {
                "first_harvestable_date": maturity.isoformat(),
                "original_exposure_rain_mm": original_window_rain,
                "original_refusal_date_harvestable": maturity.isoformat()
                <= "2016-08-06",
                "qualified_rain_date": trial_day.isoformat(),
            }
            if maturity is not None and trial_day is not None
            else {}
        ),
        "successful_batched_dry_kg": parent["metrics"]["stored_dry_grain_kg"],
        "capacity_control_wet_limit_kg": low_capacity_limit,
        "capacity_control_stored_dry_kg": control["metrics"]["stored_dry_grain_kg"],
        "slices": diagnostics,
    }
    out.mkdir(parents=True, exist_ok=False)
    for name, data in files.items():
        write_json(out / f"{name}.json", data)
