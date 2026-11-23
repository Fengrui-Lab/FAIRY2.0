"""Reusable public-operation probes and replay checks for historical experiments."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Callable

from evaluate.scenarios.historical.compiler import PHASES, boundary, compile_season
from evaluate.scenarios.historical.weather import verify_archive
from evaluate.scenarios.spec import ScenarioSpec
from fairy.acps.ontology.contracts import JsonObject, OperationRequest, OperationResult
from fairy.events.artifacts import artifact, replay
from fairy.events.records import fingerprint
from fairy.events.session import Runtime

REGIONS = ((0, 21), (22, 43), (44, 63))
TARGET = REGIONS[1]
STRESS_VWC = 0.18
MIN_RESPONSE = 0.002
FULL = {"start_ridge": 0, "end_ridge": 63}
Invoke = Callable[..., OperationResult]


def window(region: tuple[int, int]) -> dict[str, int]:
    return {"start_ridge": region[0], "end_ridge": region[1]}


def call(runtime: Runtime, tool: str, **args: object) -> OperationResult:
    return runtime.invoke(OperationRequest(tool, args), actor="historical_driver")


def require(result: OperationResult) -> JsonObject:
    if not result.ok:
        raise ValueError(f"Historical operation rejected: {result.error}")
    return result.data


def move_to(runtime: Runtime, target: datetime) -> None:
    delta = target.astimezone(timezone.utc) - runtime.now.astimezone(timezone.utc)
    if not timedelta(0) < delta <= timedelta(hours=48):
        raise ValueError("Invalid historical probe advance")
    require(call(runtime, "advance_time", hours=delta.total_seconds() / 3600))


def next_day(runtime: Runtime) -> None:
    move_to(
        runtime, boundary(runtime.now.date() + timedelta(days=1), zone=runtime.zone)
    )


def readings(invoke: Invoke, region: tuple[int, int]) -> JsonObject:
    """The observer sees only current public operation results."""
    request = window(region)
    soil = require(invoke("observe_soil_range", **request))["receipt"]["observations"]
    crop = require(invoke("inspect_crop", **request))["receipt"]["observations"]
    return {
        "vwc": sum(row["root_vwc"] for row in soil) / len(soil),
        "stages": sorted({row["stage"] for row in crop}),
        "stage_by_ridge": {str(row["ridge_id"]): row["stage"] for row in crop},
        "harvestable": all(row["harvestable"] for row in crop),
    }


def water_stress_stage(stages: list[str], backend: str = "native") -> bool:
    """Check the intended phase at the resolution of the active backend."""
    allowed = ("reproductive",) if backend == "wofost" else PHASES
    return bool(stages) and set(stages) <= set(allowed)


def ready_for_water(reading: JsonObject, backend: str = "native") -> bool:
    return (
        water_stress_stage(reading["stages"], backend) and reading["vwc"] < STRESS_VWC
    )


def plant_prefix(spec: ScenarioSpec, *, backend: str = "native") -> Runtime:
    """Use public operations; retry days that were not workable without editing forcing."""
    runtime = Runtime(spec, backend)
    assert spec.season is not None
    from evaluate.experiments.historical.supply import provision_work

    def invoke(tool: str, **args: object) -> OperationResult:
        return call(runtime, tool, **args)

    prepared = False
    limit = runtime.now.date() + timedelta(days=21)
    while runtime.now.date() < limit:
        if not prepared:
            if "mount_implement" in spec.tools:
                provision_work(invoke, "prepare_seedbed", FULL)
            result = call(runtime, "prepare_seedbed", **FULL)
            prepared = result.ok
            if not prepared and result.error != "FIELD_NOT_TRAFFICABLE":
                require(result)
        if prepared:
            if "mount_implement" in spec.tools:
                provision_work(invoke, "plant", FULL, fill_hopper=True)
            planted = call(
                runtime,
                "plant",
                **FULL,
                spacing_m=spec.season.seed_spacing_cm / 100,
                depth_mm=spec.season.sowing_depth_cm * 10,
            )
            if planted.ok:
                return runtime
            if planted.error != "FIELD_NOT_TRAFFICABLE":
                require(planted)
        next_day(runtime)
    raise ValueError("No trafficable planting opportunity within 21 days")


def close_harvest(runtime: Runtime) -> None:
    """Finish in capacity-safe batches using only ordinary runtime requests."""
    from evaluate.experiments.historical.supply import provision_work

    def invoke(tool: str, **args: object) -> OperationResult:
        return call(runtime, tool, **args)

    width = 4
    for first in range(0, 64, width):
        while runtime.now < runtime.deadline:
            if runtime.deadline.date() == runtime.now.date():
                raise ValueError("No remaining harvest window before deadline")
            if runtime.now.hour >= 18:
                next_day(runtime)
                continue
            bounds = window((first, first + width - 1))
            if "refuel_equipment" in runtime.scenario.tools:
                provision_work(invoke, "harvest", bounds)
            harvest = call(runtime, "harvest", **bounds)
            if harvest.ok:
                break
            if harvest.error != "FIELD_NOT_TRAFFICABLE":
                require(harvest)
            next_day(runtime)
        else:
            raise ValueError("No harvest window before deadline")
        require(call(runtime, "unload"))
        inventory = require(call(runtime, "inspect_inventory"))["receipt"]
        if inventory["trailer"]["moisture"] > runtime.season_config.safe_moisture:
            require(call(runtime, "dry_grain", target_moisture=0.13))
        require(call(runtime, "store_grain"))
    runtime.finish("historical_harvest_complete")


def grow_to_harvest(
    spec: ScenarioSpec,
    *,
    dose: tuple[str, float] | None = None,
    target: tuple[int, int] = TARGET,
    planted_runtime: Runtime | None = None,
    backend: str = "native",
) -> tuple[Runtime, JsonObject]:
    """Build the harvest-ready prefix through ordinary public operations."""
    runtime = (
        planted_runtime
        if planted_runtime is not None
        else plant_prefix(spec, backend=backend)
    )
    if runtime.scenario != spec:
        raise ValueError("Planting prefix belongs to a different scenario")
    trace = []
    applied = False
    while runtime.now.date() < runtime.deadline.date():
        today = runtime.now.date().isoformat()
        field = readings(lambda name, **args: call(runtime, name, **args), (0, 63))
        soil = readings(lambda name, **args: call(runtime, name, **args), target)
        trace.append(
            {
                "date": today,
                "target_vwc": soil["vwc"],
                "target_stages": soil["stages"],
                "stage_by_ridge": field["stage_by_ridge"],
            }
        )
        if field["harvestable"]:
            break
        if dose is not None and today == dose[0]:
            require(call(runtime, "irrigate_range", **window(target), depth_mm=dose[1]))
            applied = True
        next_day(runtime)
    if dose is not None and not applied:
        raise ValueError("Reference requested a date outside the active growing season")
    return runtime, {
        "daily": trace,
        "stress_days": sum(
            row["target_vwc"] < STRESS_VWC
            and water_stress_stage(row["target_stages"], runtime.backend_name)
            for row in trace
        ),
    }


def run_season(
    spec: ScenarioSpec,
    *,
    dose: tuple[str, float] | None = None,
    target: tuple[int, int] = TARGET,
    backend: str = "native",
) -> tuple[JsonObject, JsonObject]:
    """Close a full-season run and preserve its observed crop and water exposure."""
    runtime, trace = grow_to_harvest(spec, dose=dose, target=target, backend=backend)
    close_harvest(runtime)
    record = artifact(runtime, {"kind": "historical_fixed_driver", "dose": dose})
    if not record["metrics"]["task_success"]:
        raise ValueError("Historical full-season run failed outcome gates")
    return record, trace


def effective_response(treated: float, control: float) -> bool:
    return round(treated - control, 8) >= MIN_RESPONSE


def verify_season(
    document: JsonObject, archive: JsonObject, record: JsonObject
) -> None:
    verify_archive(archive)
    if (
        fingerprint(
            compile_season(
                document, archive, backend=record["state"]["backend"]
            ).to_dict()
        )
        != record["state"]["scenario_fingerprint"]
    ):
        raise ValueError("Historical L3 JSON and artifact refer to different inputs")
    replay(record)
