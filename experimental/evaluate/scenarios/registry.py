"""One scenario catalog for all difficulty levels; no filesystem discovery."""

from .lib.l1.soybean_irrigation import soybean_irrigation
from .lib.l1.soybean_weather_irrigation import soybean_weather_irrigation
from .lib.l3.soybean_full_season import soybean_full_season, soybean_full_season_legacy
from .lib.l3.soybean_normal import (
    soybean_normal,
    soybean_drought_r1,
    soybean_drought_r5,
    soybean_drought_r1_r5,
)

SCENARIOS = {
    "soybean_l1_irrigation_v1": soybean_irrigation,
    "soybean_l1_weather_irrigation_v1": soybean_weather_irrigation,
    "soybean_l3_hn58_waterlimit_v1": soybean_full_season_legacy,
    "soybean_l3_hn58_waterlimit_v2": soybean_full_season,
    "soybean_l3_hn58_normal_v1": soybean_normal,
    "soybean_l3_hn58_drought_r1_v1": soybean_drought_r1,
    "soybean_l3_hn58_drought_r5_v1": soybean_drought_r5,
    "soybean_l3_hn58_drought_r1_r5_v1": soybean_drought_r1_r5,
}

from .lib.l3.soybean_wofost import soybean_wofost_normal, soybean_wofost_calendar_drought

SCENARIOS.update(
    soybean_l3_901_proxy_normal_v1=soybean_wofost_normal,
    soybean_l3_901_proxy_calendar_drought_v1=soybean_wofost_calendar_drought,
)

from .lib.l3.soybean_wofost import soybean_wofost_reproductive_drought

SCENARIOS["soybean_l3_901_proxy_reproductive_drought_v1"] = soybean_wofost_reproductive_drought
