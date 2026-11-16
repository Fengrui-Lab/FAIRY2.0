# Information services

`weather.py` implements WeatherService over explicit ScenarioSpec.public_weather reports, independently of the private weather forcing consumed by the biology engine. Both may coincide in a synthetic fixture but are different declarations.

`get_current_weather()` returns today's declared daily report, not an instantaneous station measurement. `get_forecast(days)` requires an integer 1–7, excludes today and returns the available declared dates up to that horizon. It reports requested/available counts and an issue timestamp at local midnight. A shortened or empty forecast is explicit; current-report absence returns WEATHER_UNAVAILABLE. No live network data or forecast-error model is implemented.

Reports use WeatherDay units: daily temperatures in °C, rain and ET0 in mm, radiation in MJ/m²/day, wind in m/s and vapor pressure in hPa. No humidity or momentary-temperature values are fabricated. Repeated calls are deterministic and do not advance time or backend state.

The new weather fixture uses perfect synthetic reports and exposes them through operations only. The original L1 continues to provide its explicitly declared public synthetic weather in context. Future services must define data coverage, provenance and issue-time semantics before integration.
