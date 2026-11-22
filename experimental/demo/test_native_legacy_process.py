"""Hydraulic conservation and causal response of the migrated Native model."""

from datetime import date, timedelta

import pytest

from evaluate.scenarios.season import SeasonSpec
from fairy.acps.backends.biology.contracts import WeatherDay
from fairy.acps.backends.biology.native.season import NativeSeasonBackend
from fairy.acps.ontology.crops.lib.soybean import SoybeanSpec
from fairy.acps.ontology.farm.spec import FarmSpec


def test_equal_weather_distinguishes_irrigation_dose_and_timing():
    start = date(2026, 5, 5)
    schedules = ({}, {48: 10}, {48: 20}, {0: 20})
    grain = []
    for schedule in schedules:
        model = NativeSeasonBackend(
            FarmSpec(initial_vwc=0.26), SoybeanSpec(), start.isoformat(), SeasonSpec()
        )
        model.plant([0], 23.5)
        for offset in range(85):
            model.queue_irrigation([0], schedule.get(offset, 0))
            flux = model.advance_day(
                WeatherDay(
                    (start + timedelta(days=offset)).isoformat(),
                    rain_mm=3.0 if offset < 25 else 0.0,
                    et0_mm=5.0,
                ),
                0.0,
            )
            assert flux["balance_error_mm"] == pytest.approx(0.0, abs=1e-9)
        assert model.field_observation([0])[0]["stage"] == "R8"
        grain.append(model.project_harvest([0])["gross_dry_kg"])
    zero, late_ten, late_twenty, early_twenty = grain
    assert zero < late_ten < late_twenty
    assert early_twenty < late_twenty


def test_zero_et0_does_not_zero_growth_and_two_layers_restore():
    start = date(2026, 5, 5)
    farm, crop, season = FarmSpec(initial_vwc=0.30), SoybeanSpec(), SeasonSpec()
    baseline = NativeSeasonBackend(farm, crop, start.isoformat(), season)
    baseline.plant([0], 23.5)
    for offset in range(20):
        baseline.advance_day(
            WeatherDay((start + timedelta(days=offset)).isoformat(), et0_mm=0.0), 0.0
        )
    assert baseline.audit()["ridges"][0]["dry_matter_g_m2"] > 2
    baseline.queue_irrigation([0], 10)
    restored = NativeSeasonBackend(farm, crop, start.isoformat(), season)
    restored.load_state(baseline.export_state())
    forcing = WeatherDay((start + timedelta(days=20)).isoformat(), et0_mm=0.0)
    assert baseline.advance_day(forcing, 0.0) == restored.advance_day(forcing, 0.0)
    assert baseline.export_state() == restored.export_state()
    assert restored.state["ridges"][0]["pending_mm"] == 0
    corrupt = restored.export_state()
    corrupt["state"]["ridges"][0]["top_storage_mm"] -= 1
    with pytest.raises(ValueError, match="conservation"):
        restored.load_state(corrupt)
