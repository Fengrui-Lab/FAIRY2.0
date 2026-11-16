"""Review regressions: lightweight recovery validation and numeric water budgets."""
from dataclasses import replace
import pytest

from evaluate.scenarios.lib.l1.soybean_irrigation import soybean_irrigation
from evaluate.scenarios.lib.l3.soybean_full_season import soybean_full_season
from fairy.acps.ontology.contracts import OperationRequest
from fairy.acps.backends.biology.native.backend import NativeBackend
from fairy.events.session import Runtime
from fairy.events.artifacts import assert_equivalent


def call(runtime, name, **arguments):
    return runtime.invoke(OperationRequest(name, arguments))


def completed(backend="native"):
    runtime = Runtime(soybean_irrigation(), backend)
    call(runtime, "observe_soil")
    call(runtime, "irrigate", depth_mm=15)
    call(runtime, "advance_time", hours=24)
    call(runtime, "observe_soil")
    runtime.finish()
    return runtime


def change_event(state, event_kind, **changes):
    next(e for e in state["events"] if e["kind"] == event_kind).update(changes)


@pytest.mark.parametrize("mutation", [
    "naive_clock", "wrong_offset", "done_string", "done_false", "errors_bool", "errors_negative",
    "errors_mismatch", "observation_value", "observation_seq", "first_seq_bool", "first_seq_wrong",
    "last_irrigation_time", "water_ledger", "pending_water", "missing_start", "start_fingerprint",
    "cause_wrong", "cause_bool", "event_naive", "event_clock", "seq_bool", "missing_result",
    "daily_date", "daily_irrigation", "advanced_from", "advanced_to", "unknown_kind",
])
def test_recovery_rejects_inconsistent_clock_events_and_ledgers(mutation):
    state = completed().export_state()
    if mutation == "naive_clock": state["now"] = state["now"][:-6]
    elif mutation == "wrong_offset": state["now"] = "2026-05-09T23:00:00+00:00"
    elif mutation == "done_string": state["done"] = "false"
    elif mutation == "done_false": state["done"] = False
    elif mutation == "errors_bool": state["tool_errors"] = True
    elif mutation == "errors_negative": state["tool_errors"] = -1
    elif mutation == "errors_mismatch": state["tool_errors"] = 3
    elif mutation == "observation_value": state["observations"][0]["root_vwc"] = .7
    elif mutation == "observation_seq": state["observations"][0]["seq"] = 999
    elif mutation == "first_seq_bool": state["first_irrigation_seq"] = True
    elif mutation == "first_seq_wrong": state["first_irrigation_seq"] = 3
    elif mutation == "last_irrigation_time": state["last_irrigation_time"] = state["now"]
    elif mutation == "water_ledger": state["irrigation_used_mm"] = 0
    elif mutation == "pending_water": state["pending_irrigation_mm"] = 1
    elif mutation == "missing_start": state["events"] = []
    elif mutation == "start_fingerprint": state["events"][0]["payload"]["scenario_fingerprint"] = "other"
    elif mutation == "cause_wrong": change_event(state, "operation_result", cause=1)
    elif mutation == "cause_bool": change_event(state, "operation_result", cause=True)
    elif mutation == "event_naive": change_event(state, "operation_requested", time="2026-05-09T07:00:00")
    elif mutation == "event_clock": change_event(state, "operation_result", time="2026-05-09T08:00:00+08:00")
    elif mutation == "seq_bool": state["events"][0]["seq"] = True
    elif mutation == "missing_result": state["events"] = state["events"][:-2]
    elif mutation == "daily_date": next(e for e in state["events"] if e["kind"] == "environment_day_completed")["payload"]["day"] = "2026-05-08"
    elif mutation == "daily_irrigation": next(e for e in state["events"] if e["kind"] == "environment_day_completed")["payload"]["flux"]["irrigation_mm"] = 0
    elif mutation == "advanced_from": next(e for e in state["events"] if e["kind"] == "time_advanced")["payload"]["from"] = state["now"]
    elif mutation == "advanced_to": next(e for e in state["events"] if e["kind"] == "time_advanced")["payload"]["to"] = state["last_irrigation_time"]
    elif mutation == "unknown_kind": change_event(state, "operation_result", kind="unexpected")
    with pytest.raises(ValueError):
        Runtime.from_state(state)


@pytest.mark.parametrize("mutation", ["storage_balance", "storage_upper", "storage_lower", "application_loss", "initial_totals", "past_clock"])
def test_native_load_rejects_impossible_water_state(mutation):
    spec = soybean_irrigation()
    engine = NativeBackend(spec.farm, spec.crop, spec.sowing_date)
    state = engine.export_state()
    s = state["state"]
    if mutation == "storage_balance": s["storage_mm"] -= 1
    elif mutation == "storage_upper": s["storage_mm"] = 1000
    elif mutation == "storage_lower": s["storage_mm"] = 0
    elif mutation == "application_loss": s.update(irrigation_mm=1, application_loss_mm=1)
    elif mutation == "initial_totals": s["gdd"] = 1
    else: s["next_day"] = "2026-04-30"
    with pytest.raises(ValueError): engine.load_state(state)


def test_native_restore_checks_fixed_weather_even_with_balanced_water():
    state = completed().export_state()
    state["backend_state"]["state"]["gdd"] += 1
    with pytest.raises(ValueError, match="forcing"):
        Runtime.from_state(state)
    state = completed().export_state()
    state["backend_state"]["state"]["rain_mm"] += 1
    state["backend_state"]["state"]["runoff_mm"] += 1
    with pytest.raises(ValueError, match="forcing"):
        Runtime.from_state(state)


@pytest.mark.parametrize("field", ["tmin_c", "irrigation_mm"])
def test_wofost_restore_checks_fixed_input_journal(field):
    pytest.importorskip("pcse")
    state = Runtime(soybean_irrigation(), "wofost").export_state()
    entry = state["backend_state"]["journal"][0]
    if field == "tmin_c": entry["weather"][field] += 1
    else: entry[field] = 1
    with pytest.raises(ValueError): Runtime.from_state(state)


@pytest.mark.parametrize("season", [False, True])
def test_fractional_quota_can_be_restored_without_clamping_either_ledger(season):
    spec = soybean_full_season() if season else replace(soybean_irrigation(), irrigation_quota_mm=4)
    runtime = Runtime(spec)
    for _ in range(40):
        result = (call(runtime, "irrigate_range", start_ridge=0, end_ridge=63, depth_mm=.1)
                  if season else call(runtime, "irrigate", depth_mm=.1))
        assert result.ok
    recovered = Runtime.from_state(runtime.export_state())
    assert_equivalent(runtime.export_state(), recovered.export_state())
    result = (call(recovered, "irrigate_range", start_ridge=0, end_ridge=63, depth_mm=.01)
              if season else call(recovered, "irrigate", depth_mm=.01))
    assert not result.ok
    assert call(recovered, "advance_time", hours=24).ok
    assert_equivalent(recovered.export_state(), Runtime.from_state(recovered.export_state()).export_state())


def test_recovery_keeps_rejections_and_rolled_back_engine_failure(monkeypatch):
    runtime = Runtime(soybean_irrigation())
    assert not call(runtime, "unknown").ok
    assert not call(runtime, "irrigate", depth_mm=-1).ok
    assert call(runtime, "irrigate", depth_mm=10).ok
    original = runtime.backend.advance_day
    def failure(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("injected")
    monkeypatch.setattr(runtime.backend, "advance_day", failure)
    with pytest.raises(RuntimeError): call(runtime, "advance_time", hours=24)
    recovered = Runtime.from_state(runtime.export_state())
    assert_equivalent(runtime.export_state(), recovered.export_state())
    assert call(recovered, "advance_time", hours=24).ok
    assert recovered.audit_state()["irrigation_mm"] == 10
    assert recovered.tool_errors == 2


def test_valid_snapshots_need_no_native_engine_steps(monkeypatch):
    state = completed().export_state()
    def forbidden(*args, **kwargs): raise AssertionError("Restore reran physical simulation")
    monkeypatch.setattr(NativeBackend, "advance_day", forbidden)
    assert_equivalent(state, Runtime.from_state(state).export_state())


def test_recovery_rejects_objects_disagreeing_with_operation_history():
    runtime = Runtime(soybean_full_season())
    assert call(runtime, "prepare_seedbed", start_ridge=0, end_ridge=0).ok
    state = runtime.export_state()
    state["physical_state"]["state"]["prepared"][0] = False
    with pytest.raises(ValueError, match="Field operations"):
        Runtime.from_state(state)


def test_sub_microsecond_advance_is_rejected_without_unrestorable_zero_time_event():
    runtime = Runtime(soybean_irrigation())
    before = runtime.now
    result = call(runtime, "advance_time", hours=1e-12)
    assert not result.ok and result.error == "TIME_STEP_TOO_SMALL"
    assert runtime.now == before
    assert_equivalent(runtime.export_state(), Runtime.from_state(runtime.export_state()).export_state())
