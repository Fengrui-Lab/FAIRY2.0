"""One scenario catalog for all difficulty levels; no filesystem discovery."""
from .lib.l1.soybean_irrigation import soybean_irrigation
from .lib.l1.soybean_weather_irrigation import soybean_weather_irrigation
from .lib.l3.soybean_full_season import soybean_full_season
SCENARIOS={"soybean_l1_irrigation_v1":soybean_irrigation,
           "soybean_l1_weather_irrigation_v1":soybean_weather_irrigation,
           "soybean_l3_hn58_waterlimit_v1":soybean_full_season}
