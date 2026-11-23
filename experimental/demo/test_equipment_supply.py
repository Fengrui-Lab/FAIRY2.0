"""Regression for transactional supply recovery with unchanged total stock."""

from copy import deepcopy
from datetime import date

import pytest

from evaluate.experiments.historical.operations import call, require
from evaluate.scenarios.historical.compiler import compile_season, make_regional_document
from evaluate.scenarios.season import SeasonSpec
from fairy.acps.ontology.farm.spec import FarmSpec
from fairy.events.artifacts import artifact, replay
from fairy.events.session import Runtime
from test_historical_scenarios import (
    archive,
)  # noqa: F401 — shared engineering weather fixture


def test_interrupted_refuel_recovers_only_committed_stock(archive, monkeypatch):
    document = make_regional_document(
        archive,
        region="supply_fixture",
        start=date(1992, 5, 5),
        end=date(1992, 10, 22),
        kind="cold_planting",
        farm=FarmSpec(),
        season=SeasonSpec(initial_seed_count=300_000),
        quota_mm=0,
        equipment_supply=True,
    )
    run = Runtime(compile_season(document, archive))
    transfer = dict(
        warehouse_id="supply_warehouse-1", equipment_id="tractor-1", litres=40
    )
    before = run.export_state()
    supply_ids = {key: transfer[key] for key in ("warehouse_id", "equipment_id")}
    assert (
        call(run, "load_seeds", seeds=300_001, **supply_ids).error
        == "INSUFFICIENT_WAREHOUSE_STOCK"
    )
    assert (
        call(run, "refuel_equipment", litres=91, **supply_ids).error
        == "ONBOARD_CAPACITY_EXCEEDED"
    )
    assert (
        call(run, "return_seeds", seeds=1, **supply_ids).error
        == "INSUFFICIENT_ONBOARD_STOCK"
    )
    assert (
        call(
            run, "mount_implement", tractor_id="tractor-1", implement_id="unregistered"
        ).error
        == "UNKNOWN_IMPLEMENT"
    )
    assert run.export_state()["physical_state"] == before["physical_state"]
    assert run.now.isoformat() == before["now"]
    emit = run._emit

    def interrupt(kind, payload, cause=None):
        if kind == "job_completed" and payload["operation"] == "refuel_equipment":
            raise KeyboardInterrupt()
        return emit(kind, payload, cause)

    with monkeypatch.context() as patch:
        patch.setattr(run, "_emit", interrupt)
        with pytest.raises(KeyboardInterrupt):
            call(run, "refuel_equipment", **transfer)
    assert run.export_state()["physical_state"] == before["physical_state"]
    assert run.now.isoformat() == before["now"]
    run = Runtime.from_state(run.export_state())
    receipt = require(call(run, "refuel_equipment", **transfer))["receipt"]
    assert receipt["after"]["warehouse"]["fuel_l"] == 150
    assert receipt["after"]["fuel_tanks"]["tractor-1"]["fuel_l"] == 40
    bad = deepcopy(run.export_state())
    fuel = bad["physical_state"]["equipment_supply"]["onboard_fuel"]
    fuel["tractor-1"], fuel["combine-1"] = 0, 40
    with pytest.raises(ValueError, match="Restored equipment stocks disagree"):
        Runtime.from_state(bad)
    assert replay(artifact(run))[1]["replay_verified"]
