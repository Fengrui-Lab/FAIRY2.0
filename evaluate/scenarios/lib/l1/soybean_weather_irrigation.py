"""Ontology-loading probe: daily weather queries plus the same soybean physics."""
from dataclasses import replace
from evaluate.scenarios.lib.l1.soybean_irrigation import soybean_irrigation
from fairy.acps.ontology.contracts import ObjectSpec, OperationBinding


def soybean_weather_irrigation():
    base = soybean_irrigation()
    return replace(base, scenario_id="soybean_l1_weather_irrigation_v1",
                   task="Query today's daily synthetic weather report and the next-day forecast, then observe soil, irrigate within 45 mm, advance to the next day and recheck. Target root-zone VWC: 0.25-0.32 m3/m3. Forecasts are declared perfect synthetic inputs for this fixture, not live station measurements.",
                   objects=base.objects + (ObjectSpec("weather-1", "weather_service", base.farm.plot_id),),
                   tools=base.tools + ("get_current_weather", "get_forecast"),
                   operation_bindings=base.operation_bindings + (
                       OperationBinding("get_current_weather", "weather-1"),
                       OperationBinding("get_forecast", "weather-1")), weather_in_context=False,
                   required_queries=("get_current_weather", "get_forecast"))
