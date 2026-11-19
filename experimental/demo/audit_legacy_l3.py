"""Read-only, standard-library audit of legacy L3 artifacts; never runs a model.

Named volatile fields are reported separately, not declared harmless. Passing
necessary outcome gates does not certify the full agronomic decision process.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
import json
import math
from pathlib import Path
from zoneinfo import ZoneInfo


ZONE = ZoneInfo("Asia/Shanghai")
IDS = {"inspection_id", "mission_id"}
TIMES = {"current_timestamp", "charge_complete_at", "effect_ready_at"}


def _number(value):
    return isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value)


def compare_workflows(source: dict, replay: dict) -> dict:
    differences, substantive, mapping_errors, timing = [], [], [], []
    forward, reverse = {}, {}

    def record(path, left, right, category="substantive"):
        item = {"path": path, "source": left, "replay": right, "category": category}
        differences.append(item)
        if category == "substantive":
            substantive.append(item)

    def walk(left, right, path, relative):
        # Only documented result fields are eligible, never arguments or names.
        category = None
        if relative[:1] == ("content",):
            if relative[-1] in IDS:
                category = relative[-1]
            elif relative[-1] in TIMES:
                category = relative[-1]
            elif relative == ("content", "tick_result", "elapsed_s"):
                category = "tick_result.elapsed_s"
            elif relative == ("content", "tick_result", "last_physics_sim_time"):
                category = "tick_result.last_physics_sim_time"
        elif relative == ("time",):
            category = "workflow.time"

        if category in IDS and isinstance(left, str) and isinstance(right, str):
            key, inverse = (category, left), (category, right)
            if (key in forward and forward[key] != right) or (inverse in reverse and reverse[inverse] != left):
                mapping_errors.append({"path": path, "source": left, "replay": right})
            forward[key], reverse[inverse] = right, left
            if left != right:
                record(path, left, right, category)
            return
        if category and category not in IDS:
            try:
                if category == "workflow.time":
                    a = datetime.fromisoformat(left)
                    b = datetime.fromisoformat(right)
                    a = a.replace(tzinfo=ZONE) if a.tzinfo is None else a.astimezone(ZONE)
                    b = b.replace(tzinfo=ZONE) if b.tzinfo is None else b.astimezone(ZONE)
                    delta, date_changed = (b - a).total_seconds(), a.date() != b.date()
                elif _number(left) and _number(right):
                    delta = right - left
                    date_changed = None if category.endswith("elapsed_s") else (
                        datetime.fromtimestamp(left, ZONE).date() != datetime.fromtimestamp(right, ZONE).date()
                    )
                else:
                    raise ValueError("Invalid timing values")
            except (ValueError, TypeError, OverflowError, OSError):
                record(path, left, right)
                return
            timing.append({"path": path, "category": category, "delta_seconds": delta, "different_local_date": date_changed})
            if type(left) is not type(right) or left != right:
                record(path, left, right, category)
            return
        if type(left) is not type(right):
            record(path, left, right)
        elif isinstance(left, dict):
            for key in sorted(left.keys() | right.keys()):
                if key not in left or key not in right:
                    record(path + "." + key, {"present": key in left, "value": left.get(key)}, {"present": key in right, "value": right.get(key)})
                else:
                    walk(left[key], right[key], path + "." + key, relative + (key,))
        elif isinstance(left, list):
            if len(left) != len(right):
                record(path + ".length", len(left), len(right))
            for i, (a, b) in enumerate(zip(left, right)):
                walk(a, b, f"{path}[{i}]", relative + (str(i),))
        elif left != right:
            record(path, left, right)

    if list(source) != list(replay):
        record("workflow.step_order", list(source), list(replay))
    for step in source:
        if step in replay:
            walk(source[step], replay[step], step, ())
    timing_summary = {}
    for category in sorted({row["category"] for row in timing}):
        rows = [row for row in timing if row["category"] == category]
        timing_summary[category] = {
            "compared": len(rows),
            "max_absolute_delta_seconds": max(abs(row["delta_seconds"]) for row in rows),
            "different_local_date_count": sum(row["different_local_date"] is True for row in rows),
        }
    return {
        "exact_equal": not differences,
        "nonvolatile_fields_equal": not substantive,
        "id_mapping_bijective": not mapping_errors,
        "id_mapping_errors": mapping_errors,
        "difference_counts": dict(Counter(row["category"] for row in differences)),
        "differences": differences,
        "timing_summary": timing_summary,
        "timing_differences": [row for row in timing if row["delta_seconds"] != 0],
        "timezone": str(ZONE),
        "limitation": "Time differences remain semantically significant for scheduling, charging and delayed effects; nonvolatile equality is not deterministic replay certification.",
    }


def audit_outcome(workflow: dict, report: dict) -> dict:
    outcome = report.get("outcome", {})
    inventory = outcome.get("inventory", {})
    recovery = outcome.get("yield_recovery", {})
    regime = inventory.get("management_regime", {})
    used, quota = regime.get("irrigation_used_mm_total"), regime.get("irrigation_quota_mm_total")
    errors, stores, dries = [], [], []
    for name, step in workflow.items():
        content = step.get("content")
        if isinstance(content, dict):
            if content.get("error") or content.get("status") in {"error", "failed"}:
                errors.append({"step": name, "tool": step.get("tool_name"), "content": content})
            if step.get("tool_name") == "FarmWorldApp__store_grain" and content.get("status") == "ok":
                stores.append(content)
            if step.get("tool_name") == "FarmWorldApp__dry_grain" and content.get("status") == "ok":
                dries.append(content)
    warehouse, unhandled = inventory.get("warehouse_grain_kg"), inventory.get("harvest_grain_kg")
    gates = {
        "run_completed_without_exception": report.get("status") == "completed" and report.get("error") is None,
        "normal_stop_reason": report.get("stopped_reason", report.get("stop_reason")) in {"completed", "agent_finished"},
        "all_64_ridges_harvested": recovery.get("ridges") == 64 and recovery.get("harvested_ridges") == 64,
        "positive_warehouse_grain": _number(warehouse) and warehouse > 0,
        "harvest_inventory_empty": _number(unhandled) and unhandled == 0,
        "irrigation_within_scenario_4mm_quota": _number(used) and _number(quota) and quota == 4 and 0 <= used <= quota,
        "successful_storage_record": bool(stores),
        "stored_batches_safe_moisture": bool(stores) and all(_number(x.get("storage_grain_moisture_pct")) and 0 <= x["storage_grain_moisture_pct"] <= 13.5 for x in stores),
        "last_storage_trailer_empty": bool(stores) and stores[-1].get("trailer_grain_kg") == 0,
    }
    return {
        "necessary_outcome_gates": gates,
        "necessary_outcome_gates_pass": all(gates.values()),
        "tool_error_count": len(errors), "tool_errors": errors,
        "successful_drying_operations": len(dries),
        "grain_dried_flag": inventory.get("grain_dried"),
        "warehouse_grain_kg": warehouse, "irrigation_used_mm": used,
        "legacy_validation": (report.get("evaluation") or {}).get("validation", {}),
        "limitation": "Necessary outcome checks only: no certification of planting specifications, evidence-before-treatment, targeting, maturity/weather/trafficability checks, or optimality. Recovered tool errors require separate review; drying is conditional on grain moisture.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--replay", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True, help="Run report associated with replay")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--source-report", type=Path, help="Original run report for exact final-outcome comparison")
    parser.add_argument("--require-exact", action="store_true", help="Exit nonzero unless complete workflows, outcomes and necessary gates pass")
    args = parser.parse_args()
    source, replay, report = (json.loads(path.read_text()) for path in (args.source, args.replay, args.report))
    audit = {"schema_version": 1, "inputs": {key: str(getattr(args, key).resolve()) for key in ("source", "replay", "report")}, "comparison": compare_workflows(source, replay), "replay_outcome": audit_outcome(replay, report)}
    if args.source_report:
        source_report = json.loads(args.source_report.read_text())
        audit["inputs"]["source_report"] = str(args.source_report.resolve())
        audit["source_outcome"] = audit_outcome(source, source_report)
        audit["final_outcome_exact_equal"] = source_report.get("outcome") == report.get("outcome")
    audit["strict_acceptance"] = bool(
        audit["comparison"]["exact_equal"]
        and audit.get("final_outcome_exact_equal") is True
        and audit.get("source_outcome", {}).get("necessary_outcome_gates_pass")
        and audit["replay_outcome"]["necessary_outcome_gates_pass"]
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"out": str(args.out), "exact_equal": audit["comparison"]["exact_equal"], "nonvolatile_fields_equal": audit["comparison"]["nonvolatile_fields_equal"], "id_mapping_bijective": audit["comparison"]["id_mapping_bijective"], "necessary_outcome_gates_pass": audit["replay_outcome"]["necessary_outcome_gates_pass"]}))
    if args.require_exact and not audit["strict_acceptance"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
