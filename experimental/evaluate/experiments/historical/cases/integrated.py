"""One historical L3 that uses both equipment supply and named grain logistics."""

from datetime import date
from math import isclose
from pathlib import Path

from evaluate.experiments.historical.cases.wet_harvest import public_batch_harvest
from evaluate.experiments.historical.cases.cold_planting import plant_and_check
from evaluate.scenarios.historical.compiler import compile_season
from evaluate.experiments.historical.operations import grow_to_harvest, verify_season
from evaluate.scenarios.historical.source import load_historical_source
from fairy.acps.ontology.contracts import JsonObject, OperationRequest, OperationResult
from fairy.acps.ontology.equipment.supply import SUPPLY_ACTIONS
from fairy.events.artifacts import (
    artifact,
    assert_equivalent,
    load_json,
    replay,
    write_json,
)
from fairy.events.records import fingerprint
from fairy.events.session import Runtime

CHECKPOINTS = ("ready-to-plant", "wet-trailer", "partial-warehouse")
ACTOR = "public_integrated_season_rule"


def _conservation(run: Runtime) -> JsonObject:
    """Check located balances against successful operations and the final ledger."""
    ledger = run.season_physical.audit()
    supply = ledger["equipment_supply"]
    warehouse = supply["warehouse"]
    lots = list(ledger["grain_batches"].values())
    receipts = [
        event.payload["receipt"]
        for event in run.events
        if event.kind == "job_completed"
    ]
    jobs: dict[str, list[JsonObject]] = {}
    for receipt in receipts:
        jobs.setdefault(receipt["operation"], []).append(receipt)
        if receipt["operation"] in SUPPLY_ACTIONS:
            before, after = (receipt[side] for side in ("before", "after"))
            assert_equivalent(
                before["warehouse"]["seeds"] + before["seed_hopper"]["seeds"],
                after["warehouse"]["seeds"] + after["seed_hopper"]["seeds"],
            )
            assert_equivalent(
                before["warehouse"]["fuel_l"]
                + sum(t["fuel_l"] for t in before["fuel_tanks"].values()),
                after["warehouse"]["fuel_l"]
                + sum(t["fuel_l"] for t in after["fuel_tanks"].values()),
            )
    required = {
        "mount_implement",
        "unmount_implement",
        "load_seeds",
        "refuel_equipment",
        "prepare_seedbed",
        "plant",
        "harvest",
        "unload_batch",
        "dry_batch",
        "store_batch",
    }
    if not required <= jobs.keys() or len(lots) <= 1:
        raise ValueError("The L3 did not exercise both complete operation chains")
    if not all(lot["location_id"] == "grain_warehouse-1" for lot in lots):
        raise ValueError("Some harvested lots remain outside the warehouse")
    if sorted(i for lot in lots for i in lot["ridges"]) != list(
        range(run.season_config.ridge_count)
    ):
        raise ValueError("Stored batches do not account for every ridge exactly once")
    planted = sum(r["seeds_used"] for r in jobs["plant"])
    fuel = sum(r["fuel_l"] for r in receipts)
    dry = sum(lot["dry_kg"] for lot in lots)
    water = sum(lot["water_kg"] for lot in lots)
    removed = sum(r["removed_water_kg"] for r in jobs["dry_batch"])
    energy = sum(r["electricity_kwh"] for r in jobs["dry_batch"])
    residuals = {
        "seeds": run.season_config.initial_seed_count
        - warehouse["seeds"]
        - supply["seed_hopper"]["seeds"]
        - planted,
        "fuel_l": run.season_config.initial_fuel_l
        - warehouse["fuel_l"]
        - sum(t["fuel_l"] for t in supply["fuel_tanks"].values())
        - fuel,
        "dry_grain_kg": sum(r["batch"]["dry_kg"] for r in jobs["harvest"]) - dry,
        "grain_water_kg": sum(r["batch"]["water_kg"] for r in jobs["harvest"])
        - water
        - removed,
        "drying_electricity_kwh": ledger["electricity_kwh"] - energy,
    }
    if any(not isclose(value, 0.0, abs_tol=1e-8) for value in residuals.values()):
        raise ValueError(f"Integrated material conservation failed: {residuals}")
    assert_equivalent(ledger["seeds_used"], planted)
    assert_equivalent(ledger["fuel_used_l"], fuel)
    assert_equivalent(ledger["removed_water_kg"], removed)
    return {
        "residuals": residuals,
        "seeds_used": planted,
        "fuel_used_l": fuel,
        "stored_batches": len(lots),
        "dried_batches": len(jobs["dry_batch"]),
        "stored_dry_grain_kg": dry,
        "stored_grain_water_kg": water,
        "water_removed_kg": removed,
        "drying_electricity_kwh": energy,
    }


def _validate_checkpoint(name: str, checkpoint: JsonObject) -> None:
    """Reject a cut that lacks the intended live cross-system state."""
    state = checkpoint["state"]["physical_state"]
    stock = state["equipment_supply"]
    lots = state["grain_batches"]
    if name == "ready-to-plant":
        valid = (
            all(state["state"]["prepared"])
            and stock["mounted"] == "planter_implement-1"
            and stock["onboard_seeds"] > 0
            and stock["onboard_fuel"]["tractor-1"] > 0
            and state["state"]["seeds_used"] == 0
            and not lots
        )
    elif name == "wet-trailer":
        trailer = [lot for lot in lots if lot["location_id"] == "grain_trailer-1"]
        valid = (
            len(trailer) == 1
            and trailer[0]["dried_by"] is None
            and trailer[0]["water_kg"] / (trailer[0]["water_kg"] + trailer[0]["dry_kg"])
            > 0.135
        )
    else:
        valid = len(lots) == 8 and all(
            lot["location_id"] == "grain_warehouse-1" for lot in lots
        )
    if not valid:
        raise ValueError(f"Missing integrated checkpoint conditions: {name}")


def build_integrated_season(archive: JsonObject, out: Path, *, backend: str) -> None:
    """Exercise both sets of operations using one historical scene and public rules."""
    document = load_historical_source("harbin_integrated_1995", archive)
    spec = compile_season(document, archive, backend=backend)
    run = Runtime(spec, backend)
    checkpoints: dict[str, JsonObject] = {}
    stored = 0

    def invoke(tool: str, **args: object) -> OperationResult:
        nonlocal stored
        pending_plant = artifact(run) if tool == "plant" else None
        result = run.invoke(OperationRequest(tool, args), actor=ACTOR)
        if result.ok:
            if pending_plant is not None:
                checkpoints["ready-to-plant"] = pending_plant
            elif tool == "unload_batch" and "wet-trailer" not in checkpoints:
                lot = result.data["receipt"]["batch"]
                if (
                    lot["water_kg"] / (lot["dry_kg"] + lot["water_kg"])
                    > run.season_config.safe_moisture
                ):
                    checkpoints["wet-trailer"] = artifact(run)
            elif tool == "store_batch":
                stored += 1
                if stored == 8:
                    checkpoints["partial-warehouse"] = artifact(run)
        return result

    emergence = plant_and_check(
        invoke, run.public_status, last_plant_day=date(1995, 5, 22)
    )
    grow_to_harvest(spec, planted_runtime=run)
    if (
        public_batch_harvest(invoke, run.public_status, 0, 63, run.deadline)
        != "harvest_batches_complete"
    ):
        raise ValueError("Integrated harvest and storage did not finish")
    run.finish("integrated_season_complete")
    parent = artifact(run)
    if not parent["metrics"]["task_success"] or set(checkpoints) != set(CHECKPOINTS):
        raise ValueError("Integrated season or required checkpoints did not qualify")
    for name, checkpoint in checkpoints.items():
        _validate_checkpoint(name, checkpoint)
    report = dict(
        scenario_id=spec.scenario_id,
        backend=backend,
        emergence_date=emergence,
        archive_fingerprint=archive["fingerprint"],
        **_conservation(run),
    )
    out.mkdir(parents=True, exist_ok=False)
    write_json(out / "l3.json", document)
    write_json(out / "run.json", parent)
    for name, checkpoint in checkpoints.items():
        write_json(out / f"{name}.json", checkpoint)
    write_json(out / "conservation.json", report)


def verify_integrated_season(archive: JsonObject, out: Path) -> None:
    """Replay from initialization and continue each restored cut to the same final state."""
    parent = load_json(out / "run.json")
    verify_season(load_json(out / "l3.json"), archive, parent)
    state = parent["state"]
    expected = fingerprint(state)
    results = {}
    for name in CHECKPOINTS:
        checkpoint = load_json(out / f"{name}.json")
        _validate_checkpoint(name, checkpoint)
        prefix = checkpoint["state"]["events"]
        assert_equivalent(state["events"][: len(prefix)], prefix)
        restored = Runtime.from_state(checkpoint["state"])
        replayed, _ = replay(checkpoint)
        cut_id = fingerprint(checkpoint["state"])
        if (
            fingerprint(restored.export_state()) != cut_id
            or fingerprint(replayed.export_state()) != cut_id
        ):
            raise ValueError(f"Checkpoint restore or clean replay diverged: {name}")
        for event in state["events"][len(prefix) :]:
            body = event["payload"]
            if event["kind"] == "operation_requested":
                restored.invoke(
                    OperationRequest(body["tool"], body["arguments"]),
                    actor=body["actor"],
                )
            elif event["kind"] == "run_terminated":
                restored.finish(body["reason"])
        if fingerprint(restored.export_state()) != expected:
            raise ValueError(f"Continuation from {name} diverged from the full run")
        assert_equivalent(parent["audit"], restored.audit_state())
        conservation = _conservation(restored)
        saved = load_json(out / "conservation.json")
        assert_equivalent({key: saved[key] for key in conservation}, conservation)
        results[name] = {
            "checkpoint_fingerprint": cut_id,
            "continued_state_fingerprint": expected,
        }
    write_json(
        out / "verification.json",
        {
            "strict_full_replay": True,
            "final_state_fingerprint": expected,
            "checkpoints": results,
        },
    )
