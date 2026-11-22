"""Planter-skip recovery from public scouting under historical weather."""

from datetime import date
from pathlib import Path
from typing import Callable, Iterable

from evaluate.scenarios.historical_harvest import public_batch_harvest
from evaluate.scenarios.historical_scenarios import (
    FULL,
    Invoke,
    call,
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

GAPS = ((12, 15, 0.48), (28, 31, 0.42))
TOOLS = ("observe_stand_range", "replant_gaps", "advance_time")


def _blocks(indices: Iterable[int]) -> list[tuple[int, int]]:
    blocks: list[tuple[int, int]] = []
    for index in indices:
        if blocks and index == blocks[-1][1] + 1:
            blocks[-1] = blocks[-1][0], index
        else:
            blocks.append((index, index))
    return blocks


def _invoke(runtime: Runtime) -> Invoke:
    def invoke(tool: str, **args: object):
        return call(runtime, tool, **args)

    return invoke


def _stand(invoke: Invoke, bounds: tuple[int, int] = (0, 63)) -> list[JsonObject]:
    return require(invoke("observe_stand_range", **window(bounds)))["receipt"][
        "observations"
    ]


def _repair(
    invoke: Invoke,
    status: Callable[[], PublicStatus],
    bounds: tuple[int, int],
    *,
    wait_for_establishment: bool = True,
) -> dict[int, float]:
    """Use only exposed stand readings to locate, restore and recheck missing seed sites."""
    for _ in range(30):
        scouting = _stand(invoke, bounds)
        if all(row["established_fraction"] is not None for row in scouting):
            break
        advance_public(invoke, status())
    else:
        raise ValueError("Initial crop did not emerge within the scouting window")
    selected = {
        row["ridge_id"]: row["established_fraction"]
        for row in scouting
        if row["established_fraction"] < 0.9
        and row["original_stage"] == "vegetative"
        and row["replant_stage"] is None
    }
    if not selected:
        raise ValueError("No repairable gap seen in the public stand observations")
    for low, high in _blocks(selected):
        require(invoke("replant_gaps", **window((low, high))))
    if wait_for_establishment:
        _followup(invoke, status, bounds, set(selected))
    return selected


def _followup(
    invoke: Invoke,
    status: Callable[[], PublicStatus],
    bounds: tuple[int, int],
    selected: set[int],
) -> str:
    for attempt in range(22):
        by_ridge = {row["ridge_id"]: row for row in _stand(invoke, bounds)}
        if all(
            by_ridge[i]["established_fraction"] == 1.0
            and by_ridge[i]["replant_stage"] == "vegetative"
            for i in selected
        ):
            return status()["time"][:10]
        if attempt != 21:
            advance_public(invoke, status())
    raise ValueError("The repaired cohort did not establish within 21 days")


def _season(spec: ScenarioSpec, *, fix: bool) -> JsonObject:
    runtime = plant_prefix(spec)
    invoke = _invoke(runtime)
    if fix:
        _repair(invoke, runtime.public_status, (0, 63))
    while runtime.now < runtime.deadline:
        rows = require(invoke("inspect_crop", **FULL))["receipt"]["observations"]
        if all(row["stage"] == "harvested" for row in rows):
            break
        ready = _blocks(r["ridge_id"] for r in rows if r["harvestable"])
        if not ready:
            advance_public(invoke, runtime.public_status())
        for start, stop in ready:
            if (
                public_batch_harvest(
                    invoke, runtime.public_status, start, stop, runtime.deadline
                )
                != "harvest_batches_complete"
            ):
                raise ValueError(
                    "Replant comparison failed to close a mature cohort's batch"
                )
    runtime.finish("replant_harvest_complete")
    return artifact(
        runtime, {"kind": "public_stand_repair_rule" if fix else "no_replant"}
    )


def _short_tasks(parent: JsonObject) -> dict[str, JsonObject]:
    actual, _ = replay(parent)
    repair = [
        e
        for e in actual.events
        if e.kind == "operation_requested" and e.payload["tool"] == "replant_gaps"
    ]
    if len(repair) != len(GAPS):
        raise ValueError("No full published repair path")
    first = repair[0]
    first_day = first.time[:10]
    diagnoses = [
        e
        for e in actual.events
        if e.kind == "operation_requested"
        and e.payload["tool"] == "observe_stand_range"
        and e.time[:10] == first_day
        and e.seq < first.seq
    ]
    if not diagnoses:
        raise ValueError("No observed stand evidence from which to cut L2")
    artifacts = {}
    for level, anchor, bounds in (
        (
            "L1",
            first.seq,
            (
                first.payload["arguments"]["start_ridge"],
                first.payload["arguments"]["end_ridge"],
            ),
        ),
        ("L2", diagnoses[0].seq, (0, 63)),
    ):
        initial = Runtime(actual.scenario, actual.backend_name)
        for e in actual.events[: anchor - 1]:
            if e.kind == "operation_requested":
                initial.invoke(
                    OperationRequest(e.payload["tool"], e.payload["arguments"]),
                    actor=e.payload["actor"],
                )
        checkpoint = artifact(
            initial, {"kind": "stand_repair_prefix", "parent": fingerprint(parent)}
        )
        assert_equivalent(
            parent["state"]["events"][: anchor - 1], checkpoint["state"]["events"]
        )
        fixed = Runtime.from_state(checkpoint["state"])
        affected = _repair(
            _invoke(fixed), fixed.public_status, bounds, wait_for_establishment=False
        )
        resumed = Runtime.from_state(fixed.export_state())
        emergence = _followup(
            _invoke(resumed), resumed.public_status, bounds, set(affected)
        )
        resumed.finish("stand_repair_verified")
        result = artifact(resumed)
        replay(checkpoint)
        verified, _ = replay(result)
        turns = [row for row in public_history(verified) if row["event_seq"] >= anchor]
        if any(
            t["request"]["tool"] not in TOOLS or not t["result"]["ok"] for t in turns
        ):
            raise ValueError("Repair slice contained an invalid action")
        final = {
            row["ridge_id"]: row["established_fraction"]
            for row in turns[-1]["result"]["data"]["receipt"]["observations"]
        }
        if not all(
            final[i] > initial_fraction for i, initial_fraction in affected.items()
        ):
            raise ValueError("Published follow-up did not verify gap closure")
        control = Runtime.from_state(checkpoint["state"])
        while control.now.date().isoformat() < emergence:
            advance_public(_invoke(control), control.public_status())
        comparison = {
            row["ridge_id"]: row["established_fraction"]
            for row in _stand(_invoke(control), bounds)
        }
        assert_equivalent(affected, {i: comparison[i] for i in affected})
        control.finish("stand_wait_control")
        zero = artifact(control)
        replay(zero)
        doc = {
            "level": level,
            "parent_fingerprint": fingerprint(parent),
            "through_event_seq": anchor - 1,
            "checkpoint_fingerprint": fingerprint(checkpoint["state"]),
            "tools": list(TOOLS),
            "known_zone": list(bounds) if level == "L1" else None,
            "task": "Identify and fill established stand gaps, then confirm emergence.",
        }
        stem = level.lower()
        artifacts.update(
            {
                stem: doc,
                f"{stem}-start": checkpoint,
                f"{stem}-public": {
                    "task_success": True,
                    "slice_fingerprint": fingerprint(doc),
                    "changed_ridges": sorted(affected),
                    "emergence_day": emergence,
                    "run": result,
                    "zero": zero,
                    "before": affected,
                    "after": {i: final[i] for i in affected},
                },
            }
        )
    return artifacts


def build_replant(archive: JsonObject, out: Path) -> None:
    """Verify local planter-skip recovery, seed inventory and asynchronous maturity."""
    if archive.get("requested_point") != list(POINT) or archive["timezone"] != TIMEZONE:
        raise ValueError("The planter-skip case requires the Harbin archive")
    doc = make_regional_document(
        archive,
        region="harbin",
        start=date(2008, 5, 5),
        end=date(2008, 10, 22),
        kind="planter_skip",
        farm=FarmSpec(length_m=268, width_m=64 * 1.1, initial_vwc=0.30),
        season=SeasonSpec(planter_skip_zones=GAPS),
        quota_mm=0,
    )
    spec = compile_season(doc, archive)
    no_replant = _season(spec, fix=False)
    fixed = _season(spec, fix=True)
    for record in (no_replant, fixed):
        verify_season(doc, archive, record)
        if not record["metrics"]["task_success"]:
            raise ValueError(
                "Replant comparison failed to harvest and store all ridges"
            )
    repair_ridges = {i for low, high, _ in GAPS for i in range(low, high + 1)}
    state = fixed["state"]["physical_state"]["state"]
    zero = no_replant["state"]["physical_state"]["state"]
    run, _ = replay(fixed)
    receipt_history = [
        r
        for r in public_history(run)
        if r["request"]["tool"] == "replant_gaps" and r["result"]["ok"]
    ]
    allocations = {
        row["ridge_id"]: row["seeds"]
        for turn in receipt_history
        for row in turn["result"]["data"]["receipt"]["filled_sites"]
    }
    if (
        allocations.keys() != repair_ridges
        or sum(allocations.values()) != state["seeds_used"] - zero["seeds_used"]
    ):
        raise ValueError("Repair scope or seed conservation did not validate")
    crop_turns = [
        r for r in public_history(run) if r["request"]["tool"] == "inspect_crop"
    ]
    mature_dates: dict[int, str] = {}
    for r in crop_turns:
        for item in r["result"]["data"]["receipt"]["observations"]:
            if item["harvestable"]:
                mature_dates.setdefault(item["ridge_id"], r["time"][:10])
    original = min(d for i, d in mature_dates.items() if i not in repair_ridges)
    repaired = min(mature_dates[i] for i in repair_ridges)
    gain = (
        fixed["metrics"]["stored_dry_grain_kg"]
        - no_replant["metrics"]["stored_dry_grain_kg"]
    )
    if repaired <= original or gain <= 0:
        raise ValueError(
            "Replant run did not separate maturity or improve final storage"
        )
    files = {"l3": doc, "control": no_replant, "public": fixed, **_short_tasks(fixed)}
    files["qualification"] = {
        "repair_date": receipt_history[0]["time"][:10],
        "repair_ridges": sorted(repair_ridges),
        "seeds_used_to_fill_gaps": sum(allocations.values()),
        "initial_established_fraction": {"12-15": 0.52, "28-31": 0.58},
        "emergence_dates": {
            level: files[f"{level}-public"]["emergence_day"] for level in ("l1", "l2")
        },
        "unaffected_harvestable_date": original,
        "repaired_harvestable_date": repaired,
        "control_stored_dry_kg": no_replant["metrics"]["stored_dry_grain_kg"],
        "replant_stored_dry_kg": fixed["metrics"]["stored_dry_grain_kg"],
        "added_stored_dry_kg": gain,
    }
    out.mkdir(parents=True, exist_ok=False)
    for name, data in files.items():
        write_json(out / f"{name}.json", data)
