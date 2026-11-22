"""Offline ERA5-Land point data import and reproducible season ranking."""

from __future__ import annotations

import csv
from dataclasses import asdict
from datetime import date, datetime, time, timedelta, timezone
import hashlib
from math import acos, cos, exp, hypot, pi, radians, sin, tan
from pathlib import Path
from statistics import mean, median
from typing import Iterable
from zoneinfo import ZoneInfo

from fairy.acps.backends.biology.contracts import WeatherDay
from fairy.acps.ontology.contracts import JsonObject
from fairy.acps.ontology.farm.spec import finite
from fairy.events.records import fingerprint

DATASET = "reanalysis-era5-land-timeseries"
OPEN_METEO_DATASET = "open-meteo-era5"
TIMEZONE = "Asia/Shanghai"
POINT = (45.7, 126.6)
ELEVATION_M = 150.0
YEARS = tuple(range(1991, 2021))
HOUR = timedelta(hours=1)
VARIABLES = {
    "2m_temperature": "t2m",
    "2m_dewpoint_temperature": "d2m",
    "surface_pressure": "sp",
    "total_precipitation": "tp",
    "surface_solar_radiation_downwards": "ssrd",
    "10m_u_component_of_wind": "u10",
    "10m_v_component_of_wind": "v10",
}


def _saturation(temp_c: float) -> float:
    return 0.6108 * exp(17.27 * temp_c / (temp_c + 237.3))


def daily_et0(
    day: date,
    tmin: float,
    tmax: float,
    vapor_kpa: float,
    pressure_kpa: float,
    wind_10m: float,
    solar_mj: float,
    *,
    elevation_m: float = ELEVATION_M,
) -> float:
    """FAO-56 daily reference grass ET, with G=0 and 10 m wind adjusted to 2 m."""
    from math import log

    tmean = (tmin + tmax) / 2
    lat = radians(POINT[0])
    orbital_angle = 2 * pi * day.timetuple().tm_yday / 365
    declination = 0.409 * sin(orbital_angle - 1.39)
    sunset = acos(max(-1, min(1, -tan(lat) * tan(declination))))
    ra = (
        (24 * 60 / pi)
        * 0.0820
        * (1 + 0.033 * cos(orbital_angle))
        * (sunset * sin(lat) * sin(declination) + cos(lat) * cos(declination) * sin(sunset))
    )
    clear_sky = (0.75 + 2e-5 * elevation_m) * ra
    cloud = max(0.3, min(1.0, solar_mj / clear_sky))
    longwave = (
        4.903e-9
        * ((tmax + 273.16) ** 4 + (tmin + 273.16) ** 4)
        / 2
        * (0.34 - 0.14 * vapor_kpa**0.5)
        * (1.35 * cloud - 0.35)
    )
    net = 0.77 * solar_mj - longwave
    wind = wind_10m * 4.87 / log(67.8 * 10 - 5.42)
    slope = 4098 * _saturation(tmean) / (tmean + 237.3) ** 2
    gamma = 0.000665 * pressure_kpa
    deficit = max(0.0, (_saturation(tmin) + _saturation(tmax)) / 2 - vapor_kpa)
    return max(
        0.0,
        (0.408 * slope * net + gamma * 900 / (tmean + 273) * wind * deficit)
        / (slope + gamma * (1 + 0.34 * wind)),
    )


def _hourly(paths: Iterable[Path]) -> tuple[dict[datetime, dict[str, float]], list[JsonObject]]:
    hours: dict[datetime, dict[str, float]] = {}
    files = []
    for path in paths:
        files.append({"name": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
        with path.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            cols = set(reader.fieldnames or [])
            time_col = next((name for name in ("time", "valid_time") if name in cols), None)
            selected = {
                short: long if long in cols else short
                for long, short in VARIABLES.items()
                if long in cols or short in cols
            }
            if time_col is None or not selected or not {"latitude", "longitude"} <= cols:
                raise ValueError(
                    f"{path.name}: expected CDS point CSV with UTC time and coordinates"
                )
            for row in reader:
                if any(
                    abs(finite(float(row[k]), k) - value) > 1e-4
                    for k, value in zip(("latitude", "longitude"), POINT, strict=True)
                ):
                    raise ValueError(f"{path.name}: wrong grid point")
                utc = datetime.fromisoformat(row[time_col].replace("Z", "+00:00"))
                if utc.tzinfo is not None and utc.utcoffset() != timedelta(0):
                    raise ValueError("CDS input must be UTC")
                utc = utc.replace(tzinfo=timezone.utc)
                if utc != utc.replace(minute=0, second=0, microsecond=0):
                    raise ValueError("CDS input must use hourly timestamps")
                values = hours.setdefault(utc, {})
                for name, col in selected.items():
                    raw = row[col].strip()
                    if raw.lower() in ("", "nan", "na", "null"):
                        continue
                    value = finite(float(raw), col)
                    if name in values:
                        raise ValueError(f"Duplicate {name} at {utc.isoformat()}")
                    values[name] = value
    if not hours:
        raise ValueError("No ERA5-Land values supplied")
    return hours, files


def _dates(start: date, end: date) -> Iterable[date]:
    return (start + timedelta(days=offset) for offset in range((end - start).days + 1))


def season_dates(year: int) -> tuple[date, date]:
    return date(year, 5, 5), date(year, 10, 22)


def _aggregate(
    day: date,
    hours: dict[datetime, dict[str, float]],
    elevation_m: float = ELEVATION_M,
) -> WeatherDay | None:
    zone = ZoneInfo(TIMEZONE)
    start = datetime.combine(day, time.min, zone).astimezone(timezone.utc)
    stop = datetime.combine(day + timedelta(days=1), time.min, zone).astimezone(timezone.utc)
    instant = [hours.get(start + HOUR * ix, {}) for ix in range((stop - start) // HOUR)]
    preceding = [hours.get(start + HOUR * ix, {}) for ix in range(1, (stop - start) // HOUR + 1)]
    if any(not {"t2m", "d2m", "sp", "u10", "v10"} <= row.keys() for row in instant) or any(
        not {"tp", "ssrd"} <= row.keys() for row in preceding
    ):
        return None
    temps = [row["t2m"] - 273.15 for row in instant]
    vapor = mean(_saturation(finite(row["d2m"] - 273.15, "dewpoint", -100, 60)) for row in instant)
    pressure = (
        mean(finite(row["sp"], "surface pressure", 50_000, 110_000) for row in instant) / 1000
    )
    wind = mean(hypot(row["u10"], row["v10"]) for row in instant)
    rain = (
        sum(max(0.0, finite(row["tp"], "hourly precipitation", -1e-7)) for row in preceding) * 1000
    )
    radiation = (
        sum(max(0.0, finite(row["ssrd"], "hourly shortwave", -1.0)) for row in preceding) / 1e6
    )
    low, high = min(temps), max(temps)
    return WeatherDay(
        day.isoformat(),
        low,
        high,
        rain,
        radiation,
        wind,
        vapor * 10,
        daily_et0(day, low, high, vapor, pressure, wind, radiation, elevation_m=elevation_m),
    )


def import_archive(paths: Iterable[Path]) -> JsonObject:
    """Build a local daily archive from de-accumulated CDS point CSV inputs."""
    hours, files = _hourly(paths)
    return archive_from_hours(hours, files, DATASET, POINT, ELEVATION_M)


def archive_from_hours(
    hours: dict[datetime, dict[str, float]],
    files: list[JsonObject],
    dataset: str,
    point: tuple[float, float],
    elevation_m: float,
) -> JsonObject:
    """Freeze daily weather and provenance for an explicit provider product."""
    daily = {}
    missing = []
    for day in _dates(date(YEARS[0], 1, 1), date(YEARS[-1], 12, 31)):
        result = _aggregate(day, hours, elevation_m)
        if result is None:
            missing.append(day.isoformat())
        else:
            daily[day.isoformat()] = asdict(result)
    weather = dict(
        schema=1,
        dataset=dataset,
        requested_point=list(POINT),
        point=list(point),
        elevation_m=elevation_m,
        timezone=TIMEZONE,
        reference_years=[YEARS[0], YEARS[-1]],
        files=files,
        method="UTC hours; interval-ending precipitation/shortwave; local day; FAO-56 PM daily",
        missing_dates=missing,
        daily=daily,
    )
    return {**weather, "fingerprint": fingerprint(weather)}


def rank_seasons(archive: JsonObject) -> JsonObject:
    """Rank complete growing seasons independently of simulator yield."""
    daily = archive["daily"]
    scores = {}
    excluded = []
    for year in YEARS:
        if any(d.isoformat() not in daily for d in _dates(*season_dates(year))):
            excluded.append(year)
            continue
        rows = [daily[d.isoformat()] for d in _dates(date(year, 7, 1), date(year, 8, 15))]
        scores[year] = sum(row["rain_mm"] - row["et0_mm"] for row in rows)
    ranked = sorted(scores, key=lambda year: (scores[year], year))
    midpoint = median(scores.values()) if scores else 0.0
    count = len(ranked) // 10
    return dict(
        climatic_balance_mm={str(y): scores[y] for y in sorted(scores)},
        excluded_years=excluded,
        normal_order=sorted(
            set(scores) - set(ranked[:count]), key=lambda year: (abs(scores[year] - midpoint), year)
        ),
        drought_order=ranked[:count],
    )
