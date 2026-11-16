"""Trusted built-in strategy types; model metadata cannot grant privilege."""
from .oracle import IrrigationOracle
from .season_oracle import SeasonOracle


def is_privileged(agent):
    return type(agent) in (IrrigationOracle,SeasonOracle)


def oracle_for(scenario):
    if scenario.task_type=="irrigation": return IrrigationOracle()
    if scenario.task_type=="full_season": return SeasonOracle()
    raise ValueError("No reference strategy for this task")
