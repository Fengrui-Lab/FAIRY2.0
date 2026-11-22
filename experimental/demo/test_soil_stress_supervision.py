"""Causal public window and private PCSE supervision signal regressions."""

from dataclasses import replace
from datetime import date, timedelta

import pytest

from fairy.agents.models.numeric.soil_stress import (
    FEATURES,
    StressObservation,
    WINDOW_DAYS,
    window_features,
)


def test_public_stress_observation_uses_matching_settled_day_and_zone():
    timestamp = "2016-07-15T07:00:00+08:00"
    soil = {"time": timestamp, "observations": [{"ridge_id": 22, "root_vwc": 0.17}]}
    crop = {
        "time": timestamp,
        "observations": [{"ridge_id": 22, "phase": "reproductive"}],
    }
    weather = {
        "time": timestamp,
        "weather": {
            "day": "2016-07-14",
            "rain_mm": 0,
            "et0_mm": 5,
            "radiation_mj_m2": 20,
            "tmin_c": 15,
            "tmax_c": 25,
        },
    }
    obs = StressObservation.from_public(soil, crop, weather)
    assert obs.day == date(2016, 7, 14) and obs.ridges == (22,)
    assert obs.features == pytest.approx((0.17, 0, 5, 20, 20, 1))
    with pytest.raises(ValueError, match="weather day settled"):
        StressObservation.from_public(
            soil,
            crop,
            {**weather, "weather": {**weather["weather"], "day": "2016-07-15"}},
        )
    with pytest.raises(ValueError, match="paired measurements"):
        StressObservation.from_public(
            soil,
            {**crop, "observations": [{"ridge_id": 23, "phase": "reproductive"}]},
            weather,
        )


def test_sequence_keeps_relative_order_and_rejects_gaps():
    start = date(2016, 7, 8)
    obs = [
        StressObservation(
            start + timedelta(days=i), (22, 23), (float(i),) * len(FEATURES)
        )
        for i in range(WINDOW_DAYS)
    ]
    assert [values[0] for values in window_features(obs)] == list(range(WINDOW_DAYS))
    wrong_day = replace(obs[-1], day=obs[-1].day + timedelta(days=1))
    with pytest.raises(ValueError, match="consecutive zone"):
        window_features([*obs[:-1], wrong_day])
    with pytest.raises(ValueError, match="consecutive zone"):
        window_features([*obs[:-1], replace(obs[-1], ridges=(24, 25))])


def test_pcse_rate_is_captured_before_integration_and_hidden_from_observe():
    pytest.importorskip("pcse")
    from fairy.acps.backends.biology.contracts import WeatherDay
    from fairy.acps.backends.biology.wofost.backend import WofostBackend
    from fairy.acps.ontology.crops.lib.soybean import SoybeanSpec
    from fairy.acps.ontology.farm.spec import FarmSpec

    start = date(2016, 5, 5)
    backend = WofostBackend(
        FarmSpec(initial_vwc=0.27), SoybeanSpec(), start.isoformat()
    )
    assert backend.last_water_stress is None
    for i in range(75):
        day = (start + timedelta(days=i)).isoformat()
        backend.advance_day(WeatherDay(day, radiation_mj_m2=22, et0_mm=5), 0)
        label = backend.last_water_stress
        if label is not None and label.rfws < 0.9:
            assert label.day == day
            assert 0 <= label.rfws < 0.9
            assert "rfws" not in vars(backend.observe())
            assert backend.last_water_stress == label
            break
    else:
        pytest.fail("Expected a stressed living crop under consecutive dry days")


def test_stress_scores_validate_one_prediction_per_evaluation_label():
    np = pytest.importorskip("numpy")
    pytest.importorskip("torch")
    pytest.importorskip("sb3_contrib")
    from evaluate.oracle.stress_supervision import scores

    assert scores(np.array([0.4, 0.05]), np.array([0.3, 0.11]))["confusion"] == {
        "tp": 1,
        "fp": 1,
        "fn": 0,
        "tn": 0,
    }
    with pytest.raises(ValueError, match="paired"):
        scores(np.array([0.5]), np.array([0.5, 0.3]))
