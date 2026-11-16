"""Synthetic fixed-weather soybean pilot, not a measured field experiment."""
from datetime import date, timedelta
from evaluate.scenarios.spec import ScenarioSpec
from fairy.acps.ontology.farm.spec import FarmSpec
from fairy.acps.ontology.crops.lib.soybean import SoybeanSpec
from fairy.acps.backends.biology.contracts import WeatherDay
from fairy.acps.ontology.contracts import ObjectSpec, OperationBinding


def soybean_irrigation():
    sowing = date(2026, 5, 1)
    return ScenarioSpec(
        scenario_id="soybean_l1_irrigation_v1",
        task="Observe soybean root-zone moisture, irrigate within 45 mm, advance to the next day and observe again. Target VWC: 0.25-0.32 m3/m3. Weather is declared synthetic dry weather, not field measurements. Irrigation settles at the next local midnight.",
        farm=FarmSpec(), crop=SoybeanSpec(), sowing_date=sowing.isoformat(),
        start_time="2026-05-09T07:00:00+08:00", deadline="2026-05-10T07:00:00+08:00",
        objects=(ObjectSpec("plot-1", "plot"), ObjectSpec("soil-1", "soil_sensor", "plot-1"),
                 ObjectSpec("irrigation-1", "irrigation_system", "plot-1")),
        operation_bindings=(OperationBinding("observe_soil", "soil-1"),
                            OperationBinding("irrigate", "irrigation-1"),
                            OperationBinding("advance_time", "runtime")),
        public_weather=tuple(WeatherDay((sowing + timedelta(days=i)).isoformat()) for i in range(8, 10)),
        weather=tuple(WeatherDay((sowing + timedelta(days=i)).isoformat()) for i in range(10)),
    )
