"""Regression checks for the experimental historical decision boundary."""

from collections import deque
from copy import copy
from dataclasses import asdict, replace
from datetime import date, timedelta

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("gymnasium")
pytest.importorskip("sb3_contrib")
pytest.importorskip("pcse")

from evaluate.oracle.representation_env import Case, RepresentationEnv, legal, read
from evaluate.scenarios.historical.compiler import (
    compile_season,
    make_regional_document,
    make_season_document,
)
from evaluate.scenarios.historical.weather import DATASET, POINT, TIMEZONE, season_dates
from evaluate.scenarios.season import SeasonSpec
from fairy.acps.backends.biology.contracts import WeatherDay
from fairy.acps.ontology.farm.spec import FarmSpec
from fairy.agents.models.numeric.irrigation_window import (
    HISTORY_FIELDS,
    SNAPSHOT_FIELDS,
)
from fairy.events.records import fingerprint
from fairy.events.session import Runtime


@pytest.fixture
def season():
    start, end = season_dates(1999)
    rows = {}
    for offset in range(-1, (end - start).days + 1):
        day = (start + timedelta(days=offset)).isoformat()
        rows[day] = asdict(
            WeatherDay(
                day,
                tmin_c=17,
                tmax_c=28,
                rain_mm=1,
                radiation_mj_m2=18,
                wind_ms=2,
                vapor_pressure_hpa=12,
                et0_mm=4,
            )
        )
    archive = dict(
        schema=1,
        dataset=DATASET,
        point=list(POINT),
        timezone=TIMEZONE,
        daily=rows,
        files=[{"name": "synthetic test fixture"}],
    )
    archive["fingerprint"] = fingerprint(archive)
    source = make_season_document(archive, 1999, "normal")

    doc = make_regional_document(
        archive,
        region="harbin",
        start=date.fromisoformat(source["initial_state"]["time"][:10]),
        end=date.fromisoformat(source["task"]["deadline"][:10]),
        kind="normal",
        farm=FarmSpec(**source["space"]["farm"]),
        season=SeasonSpec.from_dict(source["initial_state"]["season"]),
        quota_mm=60,
    )
    return Case(
        "harbin", 1999, "train", 0, compile_season(doc, archive, backend="wofost")
    )


def test_public_daily_history_quota_and_restore(season):
    env = RepresentationEnv([season], "C")
    obs, _ = env.reset(seed=42)
    assert (env.runtime.now.hour, env.runtime.now.minute) == (7, 0)
    assert not env.reading.active
    assert env.action_masks().tolist() == [True, False, False, False, False]
    assert not obs[len(SNAPSHOT_FIELDS) :].any()
    initial_event_count = len(env.runtime.events)
    with pytest.raises(ValueError, match="Illegal irrigation dose"):
        env.step(2)
    assert len(env.runtime.events) == initial_event_count

    for _ in range(30):
        if env.reading.active:
            break
        env.step(0)
    assert env.action_masks().all()
    assert legal(replace(env.reading, pending_mm=5)).tolist() == [
        True,
        False,
        False,
        False,
        False,
    ]
    assert legal(replace(env.reading, remaining_mm=7)).tolist() == [
        True,
        True,
        False,
        False,
        False,
    ]
    day = env.reading.day
    obs, reward, done, _, _ = env.step(3)
    assert not done
    assert reward == pytest.approx(-0.2 * 20 / 60)
    assert env.reading.day == day + timedelta(days=1)
    assert env.reading.remaining_mm == 40
    assert env.reading.pending_mm == 0
    last = dict(zip(HISTORY_FIELDS, obs[-len(HISTORY_FIELDS) :], strict=True))
    assert last["settled_water_over_60"] == pytest.approx(20 / 60)
    assert last["day_mask"] == 1

    restored = copy(env)
    restored.history = deque(env.history, maxlen=env.history.maxlen)
    restored.actions = list(env.actions)
    restored.runtime = Runtime.from_state(env.runtime.export_state())
    restored.reading = read(restored.runtime, restored.limits)
    np.testing.assert_array_equal(env.vector(), restored.vector())
    np.testing.assert_array_equal(restored.step(0)[0], env.step(0)[0])
    assert restored.history[-1][HISTORY_FIELDS.index("settled_water_over_60")] == 0


def test_sequence_encoder_uses_the_same_ordered_input(season):
    import torch
    from fairy.agents.models.numeric.modeling import IrrigationWindowEncoder

    env = RepresentationEnv([season], "D")
    raw, _ = env.reset()
    encoder = IrrigationWindowEncoder(env.observation_space)
    vector = torch.tensor(raw[None, :])
    output = encoder(vector)
    assert output.shape == vector.shape
    torch.testing.assert_close(output, vector)
    env.step(0)
    vector = torch.tensor(env.vector()[None, :])
    torch.testing.assert_close(
        encoder(vector)[:, : len(SNAPSHOT_FIELDS)], vector[:, : len(SNAPSHOT_FIELDS)]
    )


def test_public_weather_rejects_decision_day_report(season, monkeypatch):
    from evaluate.oracle import representation_env
    from fairy.acps.ontology.contracts import OperationResult

    env = RepresentationEnv([season], "B")
    env.reset()
    actual = representation_env.call
    report = actual(env.runtime, "get_current_weather")
    assert (
        report.data["weather"]["day"]
        == (env.reading.day - timedelta(days=1)).isoformat()
    )

    def forged(runtime, tool, **arguments):
        if tool == "get_current_weather":
            return OperationResult(
                True, {"weather": {"day": runtime.now.date().isoformat()}}
            )
        return actual(runtime, tool, **arguments)

    monkeypatch.setattr(representation_env, "call", forged)
    with pytest.raises(ValueError, match="last completed local day"):
        read(env.runtime, env.limits)


def test_qualification_requires_both_controls_and_two_paired_seeds():
    from evaluate.oracle.representation_study import ARMS, SEEDS
    from evaluate.oracle.representation_training import _paired_report

    controls = []
    rows = []
    selections = []
    for year in (2001, 2007, 2016):
        case = f"harbin-{year}"
        for name, objective in (("zero", 1.0), ("public_rule", 1.1)):
            controls.append(
                dict(
                    case=case,
                    control=name,
                    climate_balance_mm=-70,
                    objective=objective,
                    metrics=dict(
                        task_success=True,
                        replay_verified=True,
                        stored_dry_grain_kg=3000,
                        water_used_mm=0,
                    ),
                )
            )
    for arm in ARMS:
        for seed in SEEDS:
            policy = dict(arm=arm, seed=seed)
            selections.append(dict(policy=policy, deployable=True))
            for year in (2001, 2007, 2016):
                won = (arm == "D" and seed != 44) or (arm == "C" and seed == 42)
                rows.append(
                    dict(
                        policy=policy,
                        case=f"harbin-{year}",
                        objective=1.2 if won else 1.05,
                        metrics=dict(
                            task_success=True,
                            replay_verified=True,
                            stored_dry_grain_kg=3400,
                            water_used_mm=10,
                        ),
                    )
                )
    result = _paired_report(selections, rows, controls, "zero")
    assert result["winning_seeds"] == {"A": 0, "B": 0, "C": 1, "D": 2}
    assert result["eligible_for_llm"] == ["D"]
    failed = next(r for r in rows if r["policy"] == {"arm": "D", "seed": 42})
    failed["metrics"]["replay_verified"] = False
    assert _paired_report(selections, rows, controls, "zero")["eligible_for_llm"] == []
