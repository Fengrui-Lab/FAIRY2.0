"""Acquire explicitly selected ERA5 through PCSE's Open-Meteo integration."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
from importlib.metadata import version
from pathlib import Path
from time import sleep
from typing import Sequence

from evaluate.scenarios.historical.weather import OPEN_METEO_DATASET, POINT, archive_from_hours
from fairy.acps.ontology.contracts import JsonObject
from fairy.acps.ontology.farm.spec import finite
from fairy.events.artifacts import load_json, write_json

UNITS = {
    "temperature_2m": "°C",
    "dew_point_2m": "°C",
    "surface_pressure": "hPa",
    "wind_speed_10m": "m/s",
    "precipitation": "mm",
    "shortwave_radiation": "W/m²",
}


def request_options(start: str, end: str) -> JsonObject:
    return {
        "latitude": POINT[0],
        "longitude": POINT[1],
        "start_date": start,
        "end_date": end,
        "hourly": ",".join(UNITS),
        "timezone": "UTC",
        "models": "era5",
        "wind_speed_unit": "ms",
        "elevation": "nan",
        "cell_selection": "nearest",
    }


def _fetch(params: JsonObject) -> JsonObject:
    """Use PCSE's provider dispatch and HTTP handling, preserving hourly response provenance."""
    from pcse.input import OpenMeteoWeatherDataProvider

    class HourlyArchiveProvider(OpenMeteoWeatherDataProvider):
        def _check_cache(self, force_update: bool = False) -> None:
            self.endpoint = self._get_url()
            self.hourly_response = self._get_response(self.endpoint, params)

    for retry in range(3):
        try:
            provider = HourlyArchiveProvider(
                *POINT,
                timezone="UTC",
                openmeteo_model="era5",
                start_date=params["start_date"],
                forecast=False,
                force_update=True,
            )
            response = provider.hourly_response
            if response:
                return {
                    "request": params,
                    "endpoint": provider.endpoint,
                    "response": response,
                    "pcse_version": version("pcse"),
                }
        except Exception as exc:
            if retry == 2:
                raise ValueError("PCSE Open-Meteo hourly request failed") from exc
        sleep(2**retry)
    raise ValueError("PCSE Open-Meteo returned no hourly response")


def fetch_openmeteo(folder: Path) -> list[Path]:
    """Fetch bounded chunks once; reruns use validated local API responses."""
    folder.mkdir(parents=True, exist_ok=True)
    groups = [
        (f"{year}-01-01" if year != 1991 else "1990-12-31", f"{year + 4}-12-31")
        for year in range(1991, 2021, 5)
    ]

    def fetch(group: tuple[str, str]) -> Path:
        start, end = group
        path = folder / f"era5_{start}_{end}.json"
        params = request_options(start, end)
        if path.exists():
            if load_json(path).get("request") != params:
                raise ValueError(f"Cached request identity differs: {path}")
            return path
        write_json(path, _fetch(params))
        return path

    with ThreadPoolExecutor(max_workers=3) as executor:
        return list(executor.map(fetch, groups))


def import_openmeteo(paths: Sequence[Path]) -> JsonObject:
    """Normalize explicitly identified, UTC, non-downscaled ERA5 hourly responses."""
    hours: dict[datetime, dict[str, float]] = {}
    files = []
    location = None
    for path in paths:
        raw = load_json(path)
        params, response = raw["request"], raw["response"]
        if (
            params != request_options(params["start_date"], params["end_date"])
            or raw["endpoint"] != "https://archive-api.open-meteo.com/v1/archive"
            or response["utc_offset_seconds"] != 0
            or any(response["hourly_units"].get(key) != unit for key, unit in UNITS.items())
        ):
            raise ValueError("Unexpected source, time basis or units in the Open-Meteo response")
        this_location = (
            finite(response["latitude"], "grid latitude", POINT[0] - 0.2, POINT[0] + 0.2),
            finite(response["longitude"], "grid longitude", POINT[1] - 0.2, POINT[1] + 0.2),
            finite(response["elevation"], "grid elevation"),
        )
        if location is not None and location != this_location:
            raise ValueError("Open-Meteo responses use different grid points")
        location = this_location
        body = response["hourly"]
        files.append(
            {
                "name": path.name,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "request": params,
                "endpoint": raw["endpoint"],
                "pcse_version": raw["pcse_version"],
            }
        )
        for stamp, temp, dew, pressure, wind, rain, solar in zip(
            body["time"], *(body[key] for key in UNITS), strict=True
        ):
            utc = datetime.fromisoformat(stamp).replace(tzinfo=timezone.utc)
            if (
                utc != utc.replace(minute=0, second=0, microsecond=0)
                or not params["start_date"] <= utc.date().isoformat() <= params["end_date"]
                or utc in hours
            ):
                raise ValueError(f"Invalid or duplicate hourly timestamp: {stamp}")
            values = {
                "t2m": (temp, 1, 273.15),
                "d2m": (dew, 1, 273.15),
                "sp": (pressure, 100, 0),
                "u10": (wind, 1, 0),
                "v10": (0, 1, 0),
                "tp": (rain, 0.001, 0),
                "ssrd": (solar, 3600, 0),
            }
            hours[utc] = {
                k: finite(v, k) * s + o for k, (v, s, o) in values.items() if v is not None
            }
    if location is None or not hours:
        raise ValueError("No ERA5 API records supplied")
    return archive_from_hours(hours, files, OPEN_METEO_DATASET, location[:2], location[2])
