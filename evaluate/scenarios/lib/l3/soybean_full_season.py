"""Declarative reconstruction of the selected 64-ridge full-season task."""
import hashlib
import json
from pathlib import Path
from evaluate.scenarios.spec import ScenarioSpec
from evaluate.scenarios.season import SeasonSpec
from fairy.acps.ontology.crops.lib.soybean import SoybeanSpec
from fairy.acps.ontology.farm.spec import FarmSpec
from fairy.acps.ontology.contracts import ObjectSpec,OperationBinding
from fairy.acps.ontology.equipment.operations import PROVIDERS
from fairy.acps.backends.biology.contracts import WeatherDay

SCENARIO_ID="soybean_l3_hn58_waterlimit_v1"
WEATHER_SHA256="b9fd613f15ef1e6a216b0c557968ef5f377ca853df1c0050f18d34e3c7d8a1b2"


def soybean_full_season():
    raw=Path(__file__).with_name("weather.json").read_bytes()
    if hashlib.sha256(raw).hexdigest()!=WEATHER_SHA256:
        raise ValueError("Full-season forcing fingerprint mismatch")
    weather=tuple(WeatherDay(**item) for item in json.loads(raw))
    objects=[ObjectSpec("plot-1","plot"),ObjectSpec("weather-1","weather_service","plot-1")]
    bindings=[OperationBinding("get_current_weather","weather-1"),OperationBinding("get_forecast","weather-1")]
    for kind,operations in PROVIDERS.items():
        provider=kind+"-1"
        objects.append(ObjectSpec(provider,kind,"plot-1"))
        bindings.extend(OperationBinding(op.name,provider) for op in operations)
    bindings.append(OperationBinding("advance_time","runtime"))
    return ScenarioSpec(
        scenario_id=SCENARIO_ID,task_type="full_season",season=SeasonSpec(hydraulic_zones=((22,43,.285,.82,.90),)),
        task="Manage the unplanted 64-ridge HEINONG58 soybean field through harvest and safe storage before the deadline. "
             "All ridges must be planted at 7.9 cm spacing and 4 cm depth, harvested, and all recovered grain safely stored "
             "at wet-basis moisture <= 0.135 with empty combine and trailer. The irrigation budget is 4 mm averaged "
             "over the whole field. Stay within seed, fuel, battery and container capacities. "
             "Weather and observations are synthetic. This reduced model is not a validated field-yield predictor.",
        farm=FarmSpec(length_m=268,width_m=64*1.1,initial_vwc=.30),crop=SoybeanSpec(),
        sowing_date="2026-05-05",start_time="2026-05-05T07:00:00+08:00",
        deadline="2026-10-22T07:00:00+08:00",irrigation_quota_mm=4.0,max_advance_hours=720.0,
        weather=weather,public_weather=weather,weather_in_context=False,
        objects=tuple(objects),operation_bindings=tuple(bindings),tools=tuple(b.operation for b in bindings),seed=1202)
