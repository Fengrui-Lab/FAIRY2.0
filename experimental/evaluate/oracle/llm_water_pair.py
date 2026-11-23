"""Compare real LLM timing guidance, compact diagnosis and expert ranking."""

from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from datetime import date
from multiprocessing import get_context
from pathlib import Path
from typing import Sequence, cast

from evaluate.oracle.stress_supervision import Probe, probes
from evaluate.scenarios.historical_scenarios import (
    TARGET,
    call,
    close_harvest,
    grow_to_harvest,
    next_day,
    plant_prefix,
    require,
    window,
)
from fairy.acps.ontology.contracts import JsonObject
from fairy.agents.loop.context.history import public_history
from fairy.agents.loop.context.public import PublicContext, build_context
from fairy.agents.loop.context.water import soil_profile, root_water
from fairy.agents.models.llm.adapter import ChatClient, LLMAdapter
from fairy.events.artifacts import (
    artifact,
    assert_equivalent,
    load_json,
    replay,
    write_json,
)
from fairy.events.records import HistoryTurn, fingerprint
from fairy.events.session import Runtime

TARGETS = ((0, 21), TARGET)
DATES = {
    "harbin-2001": ("2001-06-22", "2001-06-28"),
    "harbin-2007": ("2007-06-23", "2007-07-17"),
    "harbin-2016": ("2016-07-12", "2016-07-20"),
    "changchun-2016": ("2016-07-07", "2016-07-12"),
}
ZONES = {f"{a}-{b}": set(range(a, b + 1)) for a, b in TARGETS}
ARMS = ("raw", "timing_raw", "timing_diagnosis", "timing_ranked")
KINDS = ("fresh", "severe", "pending", "settled")
MODEL = "Qwen3.6-35B-A3B-FP8"
WINDOW_STEPS = 6


def prepare_case(job: tuple[str, Probe, Path]) -> None:
    name, probe, root = job
    first_dose, decision_day = DATES[name]
    runtime = plant_prefix(probe.spec, backend="wofost")
    profile = soil_profile(probe.spec)
    folder = root / name
    folder.mkdir()

    def read_zones() -> dict[str, float]:
        fractions = {}
        for zone in TARGETS:
            require(call(runtime, "inspect_crop", **window(zone)))
            rows = require(call(runtime, "observe_soil_range", **window(zone)))[
                "receipt"
            ]["observations"]
            assessment = root_water(
                {row["ridge_id"]: row["root_vwc"] for row in rows}, profile
            )
            fractions[f"{zone[0]}-{zone[1]}"] = assessment.available_fraction
        return fractions

    while runtime.now.date().isoformat() < decision_day:
        if runtime.now.date().isoformat() == first_dose:
            read_zones()
            write_json(
                folder / "fresh.json",
                artifact(runtime, {"kind": "shared_fresh_checkpoint"}),
            )
            require(call(runtime, "irrigate_range", **window(TARGET), depth_mm=10))
        next_day(runtime)
    fractions = read_zones()
    if fractions["22-43"] >= min(0.20, fractions["0-21"]):
        raise ValueError(f"{name} did not reproduce the more water-limited target")
    for kind in ("severe", "pending", "settled"):
        record = artifact(runtime, {"kind": f"shared_{kind}_checkpoint"})
        context = build_context(runtime, public_history(runtime), max_chars=60000)
        if not context.get("water_diagnostics", {}).get("zones"):
            raise ValueError("No diagnosis available at shared starting point")
        write_json(folder / f"{kind}.json", record)
        if kind == "severe":
            require(call(runtime, "irrigate_range", **window(TARGET), depth_mm=10))
        if kind == "pending":
            next_day(runtime)
            require(call(runtime, "get_current_weather"))
    write_json(
        folder / "design.json",
        dict(case=name, fractions=fractions, days=[first_dose, decision_day]),
    )


def prepare(root: Path, harbin: Path, changchun: Path) -> None:
    cases = probes(load_json(harbin), load_json(changchun))
    heldout = [
        (
            (
                "changchun-2016"
                if "changchun" in p.spec.scenario_id
                else f"harbin-{p.year}"
            ),
            p,
            root,
        )
        for p in cases
        if p.split == "test" and not p.irrigated
    ]
    if {name for name, _, _ in heldout} != DATES.keys():
        raise ValueError("Held-out case list changed")
    root.mkdir(parents=True, exist_ok=False)
    with ProcessPoolExecutor(max_workers=4, mp_context=get_context("spawn")) as pool:
        list(pool.map(prepare_case, heldout))


def model_context(runtime: Runtime, arm: str) -> PublicContext:
    context = build_context(
        runtime, public_history(runtime), max_chars=60000, max_turns=8
    )
    context["task"] += (
        " Make the immediate water-management decision for ridge ranges 0–21 and 22–43. "
        "The rest of the season will use a common harvest protocol. Use tools for any "
        "observations or operations you choose. End the decision when ready."
    )
    if arm in ("raw", "timing_raw"):
        context.pop("water_diagnostics", None)
    return context


def decide(job: tuple[Path, str, str, str, str]) -> JsonObject:
    root, name, kind, arm, url = job
    folder = root / name
    checkpoint = load_json(folder / f"{kind}.json")
    output = folder / f"{kind}-{arm}-decision.json"
    if output.exists():
        return load_json(output)["agent"]
    progress_path = folder / f"{kind}-{arm}-progress.json"
    tail = load_json(progress_path) if progress_path.exists() else checkpoint
    runtime = Runtime.from_state(tail["state"])
    from openai import OpenAI
    import httpx

    # This service is explicitly selected for the experiment. No unrelated
    # provider credential or environment proxy is used.
    with httpx.Client(trust_env=False, timeout=90.0) as transport:
        agent = LLMAdapter(
            model=MODEL,
            client=cast(
                ChatClient,
                OpenAI(
                    base_url=url,
                    api_key="local",
                    timeout=90.0,
                    max_retries=0,
                    http_client=transport,
                ),
            ),
            thinking=False,
            temperature=0,
            max_output_tokens=768,
            daily_observation_guidance=arm != "raw",
            rank_water_diagnostics=arm == "timing_ranked",
        )
        if progress_path.exists():
            agent.metadata = tail["agent"]
        metadata = agent.metadata
        metadata.update(
            arm=arm, window=kind, case=name, checkpoint=fingerprint(checkpoint["state"])
        )
        metadata.setdefault("decisions", [])
        metadata.setdefault("service_failures", [])
        initial = (
            Runtime.from_state(checkpoint["state"])
            if progress_path.exists()
            else runtime
        )
        initial_context = model_context(initial, "timing_ranked")
        if not initial_context.pop("water_diagnostics", None):
            raise ValueError("Paired checkpoint lacks diagnosis")
        metadata["common_initial_context_sha256"] = fingerprint(initial_context)
        start_day = date.fromisoformat(checkpoint["state"]["now"][:10])
        if runtime.now.date() != start_day:
            metadata["decision_stop"] = "crossed_local_date"
        elif metadata["decisions"] and metadata["decisions"][-1]["kind"] == "finished":
            metadata["decision_stop"] = "model_finished"
        else:
            for _ in range(len(metadata["decisions"]), WINDOW_STEPS):
                try:
                    decision = agent.decide(model_context(runtime, arm))
                except Exception as exc:
                    metadata["service_failures"].append(type(exc).__name__)
                    write_json(
                        progress_path, artifact(runtime, metadata), overwrite=True
                    )
                    raise
                if decision.protocol_error:
                    metadata["decisions"].append(
                        dict(kind="protocol_error", code=decision.protocol_error)
                    )
                elif decision.request is None:
                    metadata["decisions"].append(dict(kind="finished"))
                    metadata["decision_stop"] = "model_finished"
                else:
                    response = runtime.invoke(decision.request, actor="agent")
                    metadata["decisions"].append(
                        dict(
                            kind="tool",
                            day=runtime.now.date().isoformat(),
                            name=decision.request.tool,
                            arguments=decision.request.arguments,
                            success=response.ok,
                            error=response.error,
                        )
                    )
                    if runtime.now.date() != start_day:
                        metadata["decision_stop"] = "crossed_local_date"
                write_json(progress_path, artifact(runtime, metadata), overwrite=True)
                if metadata.get("decision_stop"):
                    break
        metadata.setdefault("decision_stop", "decision_step_limit")
        write_json(output, artifact(runtime, metadata))
        print(
            f"{name}/{kind}/{arm}: {len(metadata['decisions'])} decisions", flush=True
        )
        return metadata


def measures(record: JsonObject) -> JsonObject:
    decisions = [d for d in record["agent"]["decisions"] if d["kind"] == "tool"]
    target = ZONES["22-43"]
    irrigation = [d for d in decisions if d["name"] == "irrigate_range"]
    attempted = [d["arguments"] for d in irrigation]
    actual = [d["arguments"] for d in irrigation if d["success"]]

    def ridges(args: JsonObject) -> set[int]:
        first, last = args.get("start_ridge"), args.get("end_ridge")
        if (
            type(first) is not int
            or type(last) is not int
            or not 0 <= first <= last < 64
        ):
            return set()
        return set(range(first, last + 1))

    rechecked = set()
    for decision in decisions:
        if decision["name"] in ("irrigate_range", "advance_time", "harvest"):
            break
        if decision["name"] == "observe_soil_range" and decision["success"]:
            rechecked.update(ridges(decision["arguments"]))
    return dict(
        first_tool=decisions[0]["name"] if decisions else None,
        overlapping_irrigation_attempt=any(
            bool(ridges(args) & target) for args in attempted
        ),
        overlapping_irrigation_accepted=any(
            bool(ridges(args) & target) for args in actual
        ),
        full_target_rechecked_before_work=target <= rechecked,
        first_accepted_irrigation_in_target=bool(
            actual and ridges(actual[0]) and ridges(actual[0]) <= target
        ),
        first_accepted_irrigation_full_target=bool(
            actual and ridges(actual[0]) == target
        ),
        irrigation_attempts=attempted,
        accepted_irrigation=actual,
        tool_calls=len(decisions),
        protocol_errors=sum(
            d["kind"] == "protocol_error" for d in record["agent"]["decisions"]
        ),
    )


def repeated_observations(history: Sequence[HistoryTurn], anchor: int) -> JsonObject:
    """Count same-day successful reads already covered at their request time."""
    covered: dict[tuple[str, str], set[int]] = {}
    counts = dict(observe_soil_range=0, inspect_crop=0)
    for turn in history:
        operation = turn["request"]["tool"]
        if not turn["result"]["ok"]:
            continue
        day = turn["time"][:10]
        if operation in ("plant", "harvest", "replant_gaps"):
            covered.pop((day, "inspect_crop"), None)
        if operation not in counts:
            continue
        rows = turn["result"]["data"]["receipt"]["observations"]
        ids = {row["ridge_id"] for row in rows}
        existing = covered.setdefault((day, operation), set())
        if turn.get("event_seq", 0) > anchor and ids and ids <= existing:
            counts[operation] += 1
        existing.update(ids)
    return counts


def finish(job: tuple[Path, str, str, str]) -> JsonObject:
    root, name, kind, arm = job
    folder = root / name
    decision = load_json(folder / f"{kind}-{arm}-decision.json")
    checkpoint = load_json(folder / f"{kind}.json")
    if decision["agent"]["checkpoint"] != fingerprint(checkpoint["state"]):
        raise ValueError("Decision does not match the shared checkpoint")
    runtime = Runtime.from_state(decision["state"])
    anchor = len(checkpoint["state"]["events"])
    assert_equivalent(
        checkpoint["state"]["events"], runtime.export_state()["events"][:anchor]
    )
    repeats = repeated_observations(public_history(runtime), anchor)
    result = folder / f"{kind}-{arm}-full.json"
    if result.exists():
        record = load_json(result)
    else:
        grow_to_harvest(runtime.scenario, planted_runtime=runtime)
        close_harvest(runtime)
        record = artifact(runtime, decision["agent"])
        write_json(result, record)
    reconstructed, metrics = replay(record)
    if fingerprint(reconstructed.export_state()) != fingerprint(record["state"]):
        raise ValueError("Full-run state fingerprint differed after replay")
    report = dict(
        case=name,
        window=kind,
        arm=arm,
        checkpoint=decision["agent"]["checkpoint"],
        common_initial_context_sha256=decision["agent"][
            "common_initial_context_sha256"
        ],
        service_model_calls=decision["agent"]["model_calls"],
        decisions=decision["agent"]["decisions"],
        decision={**measures(decision), "redundant_reads": repeats},
        season=metrics,
    )
    write_json(folder / f"{kind}-{arm}-summary.json", report, overwrite=True)
    print(f"{name}/{kind}/{arm}: full season replay verified", flush=True)
    return report


def write_comparison(root: Path, reports: Sequence[JsonObject]) -> None:
    groups: dict[tuple[str, str], dict[str, JsonObject]] = {}
    for report in reports:
        groups.setdefault((report["case"], report["window"]), {})[
            report["arm"]
        ] = report
    paired = []
    for (case, kind), arms in sorted(groups.items()):
        if set(arms) != set(ARMS):
            raise ValueError(f"Incomplete paired results: {case}/{kind}")
        for key in ("checkpoint", "common_initial_context_sha256"):
            if len({r[key] for r in arms.values()}) != 1:
                raise ValueError(f"Paired runs differ at {key}")
        paired.append(dict(case=case, window=kind, arms=arms))
    write_json(root / "comparison.json", paired, overwrite=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="phase", required=True)
    for phase in ("prepare", "decide", "finish"):
        item = sub.add_parser(phase)
        item.add_argument("root", type=Path)
        if phase == "prepare":
            item.add_argument("harbin", type=Path)
            item.add_argument("changchun", type=Path)
        elif phase == "decide":
            item.add_argument("--url", required=True)
            item.add_argument("--workers", type=int, choices=range(1, 9), default=6)
        else:
            item.add_argument("--workers", type=int, choices=range(1, 9), default=6)
    args = parser.parse_args(argv)
    if args.phase == "prepare":
        prepare(args.root, args.harbin, args.changchun)
        return 0
    jobs = [
        (args.root, name, kind, arm) for name in DATES for kind in KINDS for arm in ARMS
    ]
    if args.phase == "decide":
        failures = []
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            submitted = {pool.submit(decide, (*job, args.url)): job for job in jobs}
            from concurrent.futures import as_completed

            for future in as_completed(submitted):
                try:
                    future.result()
                except Exception as exc:
                    failures.append(
                        (submitted[future][1:], type(exc).__name__, str(exc)[:200])
                    )
        if failures:
            for failure in failures:
                print(failure, flush=True)
            return 2
        return 0
    with ProcessPoolExecutor(
        max_workers=args.workers, mp_context=get_context("spawn")
    ) as pool:
        reports = list(pool.map(finish, jobs))
    write_comparison(args.root, reports)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
