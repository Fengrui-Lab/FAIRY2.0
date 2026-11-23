"""Simulated biotic pressure with matched weather, scouting and treatment branches."""

from datetime import date, timedelta
from pathlib import Path
from typing import Callable

from evaluate.experiments.historical.operations import (
    FULL,
    Invoke,
    call,
    close_harvest,
    plant_prefix,
    require,
    verify_season,
    window,
)
from evaluate.scenarios.historical.compiler import compile_season, make_regional_document
from evaluate.experiments.historical.slices import advance_public
from evaluate.scenarios.historical.weather import POINT, TIMEZONE
from evaluate.scenarios.season import SeasonSpec
from evaluate.scenarios.spec import ScenarioSpec
from fairy.acps.ontology.contracts import JsonObject, OperationRequest, OperationResult
from fairy.acps.ontology.farm.spec import FarmSpec
from fairy.agents.loop.context.history import public_history
from fairy.events.artifacts import artifact, assert_equivalent, replay, write_json
from fairy.events.records import PublicStatus, fingerprint
from fairy.events.session import Runtime

THRESHOLD = 0.55
TOOLS = ("scout_biotic", "inspect_crop", "treat_biotic", "advance_time")


def _scout(invoke: Invoke, region: tuple[int, int] = (0, 63)) -> JsonObject:
    return require(invoke("scout_biotic", **window(region)))["receipt"]


def choose_treatment(
    invoke: Invoke, region: tuple[int, int] = (0, 63)
) -> tuple[str, tuple[int, int]] | None:
    """Choose from observed local pressure, remaining abstraction units and crop state."""
    observed = _scout(invoke, region)
    for kind in ("insect", "disease"):
        key = f"{kind}_pressure"
        candidates = [
            row["ridge_id"]
            for row in observed["observations"]
            if row[key] is not None
            and row[key] >= THRESHOLD
            and row[f"{kind}_protection_days"] == 0
            and row["ridge_id"] not in observed["pending_ridges"]
        ]
        if not candidates or len(candidates) > observed["protection_remaining_units"]:
            continue
        if candidates != list(range(candidates[0], candidates[-1] + 1)):
            raise ValueError(
                "The declared single-area fixture yielded disjoint scouting targets"
            )
        target = (candidates[0], candidates[-1])
        crop = require(invoke("inspect_crop", **window(target)))["receipt"][
            "observations"
        ]
        if all(
            row["phase"] in ("vegetative", "reproductive") and row["stage"] != "R7"
            for row in crop
        ):
            require(invoke("treat_biotic", **window(target), kind=kind))
            return kind, target
    return None


def _invoke(runtime: Runtime) -> Invoke:
    def invoke(name: str, **args: object) -> OperationResult:
        return call(runtime, name, **args)

    return invoke


def _run(spec: ScenarioSpec, *, interventions: int) -> JsonObject:
    runtime = plant_prefix(spec)
    invoke = _invoke(runtime)
    remaining = interventions
    while runtime.now < runtime.deadline:
        crop = require(invoke("inspect_crop", **FULL))["receipt"]["observations"]
        if all(row["harvestable"] for row in crop):
            break
        if remaining and choose_treatment(invoke) is not None:
            remaining -= 1
        advance_public(invoke, runtime.public_status())
    close_harvest(runtime)
    return artifact(
        runtime, {"kind": "public_biotic_rule", "intervention_limit": interventions}
    )


def _requests(record: JsonObject, tool: str) -> list[JsonObject]:
    return [
        event
        for event in record["state"]["events"]
        if event["kind"] == "operation_requested" and event["payload"]["tool"] == tool
    ]


def _prefix(parent: JsonObject, anchor: int) -> Runtime:
    state = parent["state"]
    runtime = Runtime(ScenarioSpec.from_dict(state["scenario"]), state["backend"])
    for event in state["events"][: anchor - 1]:
        if event["kind"] == "operation_requested":
            body = event["payload"]
            runtime.invoke(
                OperationRequest(body["tool"], body["arguments"]), actor=body["actor"]
            )
    assert_equivalent(state["events"][: anchor - 1], runtime.export_state()["events"])
    return runtime


def _slice(
    parent: JsonObject,
    level: str,
    action: JsonObject,
    region: tuple[int, int],
    kind: str,
) -> tuple[JsonObject, JsonObject, JsonObject]:
    day = action["time"][:10]
    anchor = action["seq"]
    if level == "L2":
        anchor = next(
            x["seq"]
            for x in _requests(parent, "scout_biotic")
            if x["time"][:10] == day and x["seq"] < action["seq"]
        )
    initial = _prefix(parent, anchor)
    checkpoint = artifact(
        initial, {"kind": "biotic_prefix", "parent": fingerprint(parent)}
    )
    target = region if level == "L1" else (0, 63)
    doc = {
        "level": level,
        "parent_fingerprint": fingerprint(parent),
        "checkpoint_fingerprint": fingerprint(checkpoint["state"]),
        "through_event_seq": anchor - 1,
        "tools": list(TOOLS),
        "verify_day": (date.fromisoformat(day) + timedelta(days=1)).isoformat(),
        "known_zone": list(target) if level == "L1" else None,
        "task": "Scout the target, apply appropriate protection and recheck after settlement.",
    }

    treated, wait = Runtime.from_state(initial.export_state()), Runtime.from_state(
        initial.export_state()
    )
    if choose_treatment(_invoke(treated), target) != (kind, region):
        raise ValueError(
            "Public scouting could not isolate the expected area and condition"
        )
    restored = Runtime.from_state(treated.export_state())
    for runtime in (treated, restored, wait):
        invoke = _invoke(runtime)
        advance_public(invoke, runtime.public_status())
        _scout(invoke, region)
        runtime.finish("biotic_slice_followup")
    assert_equivalent(treated.export_state(), restored.export_state())

    result: JsonObject = {
        "slice_fingerprint": fingerprint(doc),
        "treated": artifact(restored),
        "wait": artifact(wait),
    }
    response = []
    for run_key, count in (("treated", 1), ("wait", 0)):
        runtime, _ = replay(result[run_key])
        assert_equivalent(
            checkpoint["state"]["events"],
            runtime.export_state()["events"][: anchor - 1],
        )
        turns = [t for t in public_history(runtime) if t["event_seq"] >= anchor]
        if any(t["request"]["tool"] not in TOOLS for t in turns):
            raise ValueError("Unsupported operation in biotic slice")
        applied = [
            t
            for t in turns
            if t["request"]["tool"] == "treat_biotic" and t["result"]["ok"]
        ]
        if len(applied) != count or any(
            t["request"]["arguments"] != {**window(region), "kind": kind}
            for t in applied
        ):
            raise ValueError("Unexpected protection scope or kind")
        observation = turns[-1]
        if observation["time"][:10] != doc["verify_day"] or observation["request"] != {
            "tool": "scout_biotic",
            "arguments": window(region),
        }:
            raise ValueError("Missing matched next-day scouting evidence")
        measurements = observation["result"]["data"]["receipt"]["observations"]
        response.append(
            sum(row[f"{kind}_pressure"] for row in measurements) / len(measurements)
        )
    if response[1] - response[0] < 0.10:
        raise ValueError(
            "The simulated condition did not respond to targeted protection"
        )
    result.update(
        {
            "task_success": True,
            "treated_pressure": response[0],
            "untreated_pressure": response[1],
        }
    )
    replay(checkpoint)
    return doc, checkpoint, result


def build_biotic(archive: JsonObject, out: Path) -> None:
    """Publish R5 insect feeding and two-wave disease as distinct semantic migrations."""
    if archive.get("requested_point") != list(POINT) or archive["timezone"] != TIMEZONE:
        raise ValueError("These crop protection fixtures require the Harbin archive")
    out.mkdir(parents=True, exist_ok=False)
    for kind, dates, region in (
        ("insect", ("2016-07-14",), (16, 39)),
        ("disease", ("2008-06-21", "2008-07-09"), (20, 43)),
    ):
        stages_at_treatment = []
        year = date.fromisoformat(dates[0]).year
        outbreak_window = date.fromisoformat(dates[0]), date.fromisoformat(dates[-1])
        document = make_regional_document(
            archive,
            region="harbin",
            start=date(year, 5, 5),
            end=date(year, 10, 22),
            kind=f"{kind}_pressure",
            farm=FarmSpec(length_m=268, width_m=64 * 1.1, initial_vwc=0.30),
            season=SeasonSpec(
                biotic_outbreaks=tuple((day, *region, kind, 0.68) for day in dates),
                plant_protection_units=len(dates) * (region[1] - region[0] + 1),
            ),
            quota_mm=0,
            exposure=outbreak_window,
        )
        spec = compile_season(document, archive)
        zero = _run(spec, interventions=0)
        treated = _run(spec, interventions=len(dates))
        treatments = _requests(treated, "treat_biotic")
        if len(treatments) != len(dates) or any(
            e["payload"]["arguments"] != {**window(region), "kind": kind}
            for e in treatments
        ):
            raise ValueError(
                "Public strategy did not address precisely the targeted pressure waves"
            )
        restored, _ = replay(treated)
        turns = public_history(restored)
        for attempt in treatments:
            evidence = [
                t
                for t in turns
                if t["request"] == {"tool": "inspect_crop", "arguments": window(region)}
                and t["event_seq"] < attempt["seq"]
                and t["time"][:10] == attempt["time"][:10]
            ]
            if not evidence:
                raise ValueError("Treatment missing same-day crop-stage evidence")
            stages_at_treatment.append(
                sorted(
                    {
                        row["stage"]
                        for row in evidence[-1]["result"]["data"]["receipt"][
                            "observations"
                        ]
                    }
                )
            )
        files = {"l3": document, "zero": zero, "treated": treated}
        if kind == "disease":
            first_only = _run(spec, interventions=1)
            files["first-treatment-only"] = first_only
            if (
                not zero["metrics"]["stored_dry_grain_kg"]
                < first_only["metrics"]["stored_dry_grain_kg"]
                < treated["metrics"]["stored_dry_grain_kg"]
            ):
                raise ValueError(
                    "Disease recheck did not separate zero, one and two actions"
                )
        elif stages_at_treatment != [["R5"]]:
            raise ValueError("The historical insect case must expose R5")
        for record in files.values():
            if "state" in record:
                verify_season(document, archive, record)
                if not record["metrics"]["task_success"]:
                    raise ValueError("Biotic exposure run failed to close the season")
        slices = {}
        for level in ("L1", "L2"):
            doc, checkpoint, output = _slice(
                treated, level, treatments[-1], region, kind
            )
            stem = level.lower()
            files.update(
                {stem: doc, f"{stem}-start": checkpoint, f"{stem}-public": output}
            )
            slices[level] = {
                key: output[key] for key in ("treated_pressure", "untreated_pressure")
            }
        dry_zero = zero["metrics"]["stored_dry_grain_kg"]
        dry_treated = treated["metrics"]["stored_dry_grain_kg"]
        if dry_treated <= dry_zero:
            raise ValueError("No season-scale treatment contrast")
        files["qualification"] = {
            "synthetic_pressure_dates": list(dates),
            "treatment_dates": [e["time"][:10] for e in treatments],
            "treatment_stages": stages_at_treatment,
            "control_dry_kg": dry_zero,
            "treated_dry_kg": dry_treated,
            "slices": slices,
        }
        if kind == "disease":
            files["qualification"]["first_treatment_only_dry_kg"] = files[
                "first-treatment-only"
            ]["metrics"]["stored_dry_grain_kg"]
        for name, payload in files.items():
            write_json(out / kind / f"{name}.json", payload)
