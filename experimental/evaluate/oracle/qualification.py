"""Independent task/replay qualification; no generated plan is trusted as evidence."""

from __future__ import annotations
from typing import Mapping, TYPE_CHECKING

if TYPE_CHECKING:
    from fairy.acps.ontology.contracts import JsonObject
from fairy.events.artifacts import replay
from fairy.events.records import fingerprint


def qualify(record: JsonObject) -> JsonObject:
    """独立回放并核对任务、决策覆盖、建议绑定和策略来源，分别报告资格与执行质量。"""
    _, metrics = replay(record)
    info = record.get("agent", {}).get("autogen", {})
    trace = info.get("decision_trace", [])
    model_calls = record.get("agent", {}).get("model_calls", [])
    protocol_errors = record.get("agent", {}).get("protocol_errors", 0) + record.get(
        "agent", {}
    ).get("prior_usage", {}).get("protocol_errors", 0)
    physical = [e for e in record["state"]["events"] if e["kind"] == "operation_requested"]
    agent_only = all(e["payload"]["actor"] == "agent" for e in physical)
    advised = [
        r
        for r in trace
        if r["kind"] == "service" and r.get("feedback", {}).get("status") == "ADVISED"
    ]
    events = {e["seq"]: e for e in record["state"]["events"]}
    physical_trace = [r for r in trace if r["kind"] == "physical"]
    result_ids = {e["seq"] for e in events.values() if e["kind"] == "operation_result"}
    audit_errors = []
    if [r.get("seq") for r in trace] != list(range(1, len(trace) + 1)):
        audit_errors.append("decision sequence is not contiguous")
    covered = [r.get("physical_event_seq") for r in physical_trace]
    if set(covered) != result_ids or len(covered) != len(set(covered)):
        audit_errors.append("physical receipt coverage differs")
    known_advice = {}
    consumed = set()
    decision_windows = set()
    for row in trace:
        if row["kind"] == "service" and row.get("feedback", {}).get("status") in (
            "ADVISED",
            "CACHED",
            "REVALIDATED",
        ):
            advice = row["feedback"]["advice"]
            known_advice[advice["advice_id"]] = advice
            if advice["policy_fingerprint"] != info.get("policy_fingerprint"):
                audit_errors.append("advice policy identity differs")
            if row["feedback"]["status"] == "ADVISED":
                if advice["window"] in decision_windows:
                    audit_errors.append("multiple numerical decisions in one daily window")
                decision_windows.add(advice["window"])
        if row["kind"] != "physical":
            continue
        event = events.get(row.get("physical_event_seq"), {})
        if event.get("kind") != "operation_result":
            audit_errors.append("unknown physical receipt")
            continue
        request = events[event["cause"]]["payload"]
        if (
            fingerprint({"tool": request["tool"], "arguments": request["arguments"]})
            != row.get("request_fingerprint")
            or row.get("ok") != event["payload"]["ok"]
            or row.get("error") != event["payload"]["error"]
        ):
            audit_errors.append("decision/physical receipt mismatch")
        if request["tool"] == "irrigate_range" and event["payload"]["ok"]:
            advice = known_advice.get(row.get("advice_id"))
            if (
                not advice
                or advice["parameters"] != request["arguments"]
                or advice["decision_id"] in consumed
            ):
                audit_errors.append("accepted irrigation is not bound to unique recorded advice")
            if advice:
                consumed.add(advice["decision_id"])
    # Advice does not masquerade as a physical operation; zero is a genuine model output.
    numeric_used = bool(advised)
    llm_attested = record.get("agent", {}).get("kind") == "llm" and (
        bool(model_calls) or record["agent"].get("prior_usage", {}).get("calls", 0) > 0
    )
    policy = info.get("numeric_policy_manifest", {})
    trained = (
        fingerprint(policy) == info.get("policy_fingerprint")
        and policy.get("algorithm") == "MaskablePPO"
        and policy.get("actor_l1_change", 0) > 0
        and policy.get("source_fingerprint") == record["state"]["source_fingerprint"]
        and record["state"]["scenario_fingerprint"]
        in policy.get(
            "qualification_scenario_fingerprints",
            [policy.get("qualification_scenario_fingerprint")],
        )
        and policy.get("backend") == record["state"]["backend"]
        and policy.get("crop") == record["state"]["scenario"]["season"]["cultivar"]
        and policy.get("timesteps", 0) >= 128
        and len(policy.get("weights_sha256", "")) == 64
    )
    reference_eligible = (
        metrics["task_success"]
        and metrics["replay_verified"]
        and agent_only
        and llm_attested
        and not audit_errors
        and bool(info.get("knowledge_fingerprint"))
    )
    trained_stack_eligible = reference_eligible and numeric_used and trained
    score = (
        metrics["stored_dry_grain_kg"] / 3500 - 0.2 * metrics["water_used_mm"] / 60
        if metrics["task_success"]
        else None
    )
    return {
        "schema": 1,
        "comparison_domain": {
            "source": record["state"]["source_fingerprint"],
            "backend": record["state"]["backend"],
            "scenario": record["state"]["scenario_fingerprint"],
            "objective": "actual_storage_3500_minus_water_0.2_per_60_v1",
        },
        "task_valid": metrics["task_success"],
        "replay_verified": metrics["replay_verified"],
        "reference_eligible": reference_eligible,
        "clean_execution": metrics["tool_errors"] == 0 and protocol_errors == 0,
        "clean_physical_execution": metrics["tool_errors"] == 0,
        "protocol_errors": protocol_errors,
        "preflight_blocks": sum(row["kind"] == "guard" for row in trace),
        "all_physical_requests_from_public_agent": agent_only,
        "numeric_advice_calls": len(advised),
        "trained_numeric_policy_attested": trained,
        "trained_stack_eligible": trained_stack_eligible,
        "llm_execution_attested": llm_attested,
        "decision_audit_valid": not audit_errors,
        "decision_audit_errors": audit_errors[:10],
        "numeric_schedule_complete": not info.get("numeric_windows_missed", []),
        "numeric_zero_advice_calls": sum(r["feedback"]["advice"]["dose_mm"] == 0 for r in advised),
        "missed_numeric_windows": info.get("numeric_windows_missed", []),
        "metrics": metrics,
        "selection_objective": {
            "stored_dry_mass_scale_kg": 3500,
            "water_cost": 0.2,
            "score": score,
            "scope": "explicit experiment objective, not a global FAIRY score",
        },
        "artifact_fingerprint": fingerprint(record),
        "optimality": "not_proven",
        "model_calls_this_segment": len(model_calls),
        "knowledge_fingerprint": info.get("knowledge_fingerprint"),
        "memory_fingerprint": info.get("memory_fingerprint"),
        "policy_fingerprint": info.get("policy_fingerprint"),
    }


def select(reports: Mapping[str, JsonObject]) -> JsonObject:
    """在相同比较域内选最佳已知合格候选；不生成跨后端/场景总冠军。"""
    domains = {fingerprint(r.get("comparison_domain")) for r in reports.values()}
    if len(domains) > 1:
        groups = {
            domain: select(
                {
                    n: r
                    for n, r in reports.items()
                    if fingerprint(r.get("comparison_domain")) == domain
                }
            )
            for domain in sorted(domains)
        }
        return {
            "candidate_count": len(reports),
            "selected": None,
            "selected_trained_stack": None,
            "groups": groups,
            "selection": "independent comparison domains; no cross-backend/scenario winner",
            "optimality": "not_proven",
        }
    eligible = [(name, r) for name, r in reports.items() if r["reference_eligible"]]
    best = (
        max(
            eligible,
            key=lambda p: (p[1]["selection_objective"]["score"], -p[1]["metrics"]["tool_errors"]),
        )
        if eligible
        else None
    )
    trained = [p for p in eligible if p[1]["trained_stack_eligible"]]
    best_trained = (
        max(
            trained,
            key=lambda p: (p[1]["selection_objective"]["score"], -p[1]["metrics"]["tool_errors"]),
        )
        if trained
        else None
    )
    return {
        "candidate_count": len(reports),
        "eligible_count": len(eligible),
        "selected": best[0] if best else None,
        "trained_stack_count": len(trained),
        "selected_trained_stack": best_trained[0] if best_trained else None,
        "selection": "max declared actual-storage-minus-water objective, then fewer executed tool errors",
        "comparison_scope": "Includes declared non-learning baselines; learned-stack integration is reported separately",
        "optimality": "best known among listed attempts only; not proven globally optimal",
    }
