from copy import deepcopy

from demo.audit_legacy_l3 import audit_outcome, compare_workflows


def test_timestamp_and_uuid_do_not_hide_physical_difference():
    source = {"a": {"content": {"mission_id": "old", "current_timestamp": 1000.0, "water_mm": 5.0}}}
    replay = deepcopy(source)
    replay["a"]["content"].update(mission_id="new", current_timestamp=1001.0)
    audit = compare_workflows(source, replay)
    assert not audit["exact_equal"]
    assert audit["nonvolatile_fields_equal"] and audit["id_mapping_bijective"]
    replay["a"]["content"]["water_mm"] = 5.1
    assert not compare_workflows(source, replay)["nonvolatile_fields_equal"]


def test_uuid_mapping_must_be_bijective_even_when_some_values_equal():
    source = {"a": {"content": {"mission_id": "same"}}, "b": {"content": {"mission_id": "other"}}}
    replay = deepcopy(source)
    replay["b"]["content"]["mission_id"] = "same"
    assert not compare_workflows(source, replay)["id_mapping_bijective"]
    assert not compare_workflows(replay, source)["id_mapping_bijective"]


def test_date_crossing_and_argument_changes_are_not_ignored():
    source = {"a": {"time": "2026-05-01 23:59:59", "tool_args": {"mission_id": "old"}}}
    replay = {"a": {"time": "2026-05-02 00:00:01", "tool_args": {"mission_id": "new"}}}
    result = compare_workflows(source, replay)
    assert result["timing_summary"]["workflow.time"]["different_local_date_count"] == 1
    assert not result["nonvolatile_fields_equal"]


def test_legacy_success_alone_is_not_outcome_success():
    result = audit_outcome({}, {"status": "completed", "error": None, "evaluation": {"validation": {"success": True}}})
    assert not result["necessary_outcome_gates_pass"]


def test_call_order_is_semantic():
    source = {"a": {}, "b": {}}
    assert not compare_workflows(source, dict(reversed(list(source.items()))))["nonvolatile_fields_equal"]


def test_strict_cli_fails_on_timestamp_or_final_outcome_drift(tmp_path, monkeypatch):
    import json
    import sys
    import pytest
    from demo.audit_legacy_l3 import main

    workflow = {"store": {"tool_name": "FarmWorldApp__store_grain", "content": {
        "status": "ok", "storage_grain_moisture_pct": 12.0,
        "trailer_grain_kg": 0, "current_timestamp": 1000.0,
    }}}
    report = {"status": "completed", "stopped_reason": "completed", "error": None, "evaluation": None,
              "outcome": {"inventory": {"warehouse_grain_kg": 100,
                  "harvest_grain_kg": 0, "management_regime": {
                      "irrigation_used_mm_total": 2, "irrigation_quota_mm_total": 4}},
                  "yield_recovery": {"ridges": 64, "harvested_ridges": 64}}}
    files = {name: tmp_path / f"{name}.json" for name in ("source", "replay", "report", "source-report", "out")}
    for name, data in (("source", workflow), ("replay", workflow), ("report", report), ("source-report", report)):
        files[name].write_text(json.dumps(data))
    argv = ["audit", "--require-exact"]
    for name, path in files.items():
        argv.extend([f"--{name}", str(path)])
    monkeypatch.setattr(sys, "argv", argv)
    main()
    assert json.loads(files["out"].read_text())["strict_acceptance"]
    workflow["store"]["content"]["current_timestamp"] += 0.001
    files["replay"].write_text(json.dumps(workflow))
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 1
    files["replay"].write_text(files["source"].read_text())
    report["outcome"]["inventory"]["warehouse_grain_kg"] += 1
    files["report"].write_text(json.dumps(report))
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 1
