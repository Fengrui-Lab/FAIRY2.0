#!/usr/bin/env python3
"""Archive PCSE/Open-Meteo ERA5 hours as complete local WeatherDay records."""

import argparse
from datetime import date, datetime, time, timedelta, timezone
from hashlib import sha256
from importlib.metadata import version
import json
from math import acos, cos, exp, isfinite, log, pi, radians, sin, tan
from pathlib import Path
from statistics import mean
from tempfile import NamedTemporaryFile
from time import sleep
from zoneinfo import ZoneInfo

UTC = timezone.utc
HOUR = timedelta(hours=1)
DAY = timedelta(days=1)
ENDPOINT = "https://archive-api.open-meteo.com/v1/archive"
INSTANT = {
    "temperature_2m": "°C",
    "dew_point_2m": "°C",
    "surface_pressure": "hPa",
    "wind_speed_10m": "m/s",
}
PRECEDING = {"precipitation": "mm", "shortwave_radiation": "W/m²"}
UNITS = INSTANT | PRECEDING


def encode(data):
    return json.dumps(
        data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def write_new(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
        tmp = Path(temporary.name)
        try:
            temporary.write(encode(data) + b"\n")
            temporary.close()
            path.hardlink_to(tmp)
        finally:
            tmp.unlink(missing_ok=True)


def fetch(params):
    from pcse.exceptions import PCSEError
    from pcse.input import OpenMeteoWeatherDataProvider

    class HourlyArchiveProvider(OpenMeteoWeatherDataProvider):
        def _check_cache(self, force_update=False):
            endpoint = self._get_url()
            if endpoint != ENDPOINT:
                raise ValueError(f"Unexpected PCSE archive endpoint: {endpoint}")
            self.hourly_response = self._get_response(endpoint, params)

    for attempt in range(3):
        try:
            provider = HourlyArchiveProvider(
                params["latitude"],
                params["longitude"],
                timezone="UTC",
                openmeteo_model="era5",
                start_date=params["start_date"],
                forecast=False,
                force_update=True,
            )
            if not provider.hourly_response:
                raise ValueError("Empty archive response")
            return dict(
                request=params,
                response=provider.hourly_response,
                endpoint=ENDPOINT,
                pcse_version=version("pcse"),
            )
        except (OSError, PCSEError, ValueError) as exc:
            if attempt == 2:
                raise ValueError(
                    f"PCSE archive request failed for {params['start_date']}: {exc}"
                ) from exc
            sleep(2**attempt)
    raise AssertionError("unreachable")


def acquire(args, first_utc, last_utc):
    hours, sources, grid = {}, [], None
    cursor = first_utc.date()
    while cursor <= last_utc.date():
        end = min(cursor + 366 * DAY, last_utc.date())
        params = dict(
            latitude=args.latitude,
            longitude=args.longitude,
            start_date=cursor.isoformat(),
            end_date=end.isoformat(),
            hourly=",".join(UNITS),
            timezone="UTC",
            models="era5",
            wind_speed_unit="ms",
            elevation="nan",
            cell_selection="nearest",
        )
        request_id = sha256(encode(params)).hexdigest()
        path = args.raw_dir / f"era5_{cursor}_{end}_{request_id[:16]}.json"
        if not path.exists():
            if args.offline:
                raise ValueError(f"Missing offline response: {path}")
            write_new(path, fetch(params))
        raw = path.read_bytes()
        saved = json.loads(raw)
        response = saved["response"]
        if (
            saved["request"] != params
            or saved["endpoint"] != ENDPOINT
            or response["utc_offset_seconds"] != 0
            or any(
                response["hourly_units"].get(key) != unit for key, unit in UNITS.items()
            )
        ):
            raise ValueError(f"Wrong request, endpoint, time basis or units: {path}")
        location = tuple(
            response[key] for key in ("latitude", "longitude", "elevation")
        )
        lat, lon, _ = location
        lon_gap = abs((lon - args.longitude + 180) % 360 - 180)
        if (
            not all(map(isfinite, location))
            or abs(lat - args.latitude) > 0.25
            or lon_gap > 0.25
        ):
            raise ValueError(
                f"Unexpected ERA5 grid relative to the requested point: {location}"
            )
        if grid is not None and grid != location:
            raise ValueError(
                f"Response grid changed between chunks: {grid} and {location}"
            )
        grid = location
        body = response["hourly"]
        for stamp, *values in zip(
            body["time"], *(body[key] for key in UNITS), strict=True
        ):
            timestamp = datetime.fromisoformat(stamp)
            if timestamp.utcoffset() not in (None, timedelta(0)):
                raise ValueError(f"Expected a UTC timestamp: {stamp}")
            timestamp = timestamp.replace(tzinfo=UTC)
            if timestamp in hours or timestamp != timestamp.replace(
                minute=0, second=0, microsecond=0
            ):
                raise ValueError(f"Invalid or duplicate ERA5 hour: {stamp}")
            if not cursor <= timestamp.date() <= end:
                raise ValueError(f"Unexpected ERA5 hour outside the request: {stamp}")
            if first_utc <= timestamp <= last_utc:
                hours[timestamp] = dict(zip(UNITS, values, strict=True))
        sources.append(
            dict(
                name=path.name,
                sha256=sha256(raw).hexdigest(),
                request=params,
                endpoint=ENDPOINT,
                pcse_version=saved["pcse_version"],
            )
        )
        cursor = end + DAY
    return hours, sources, grid


def saturation(temp):
    return 0.6108 * exp(17.27 * temp / (temp + 237.3))


def reference_et(day, latitude, elevation, low, high, vapor, pressure, wind, solar):
    """FAO-56 daily Penman–Monteith, with daily soil heat flux set to zero."""
    temp = (low + high) / 2
    lat = radians(latitude)
    angle = 2 * pi * day.timetuple().tm_yday / 365
    declination = 0.409 * sin(angle - 1.39)
    sunset = acos(max(-1, min(1, -tan(lat) * tan(declination))))
    extraterrestrial = (
        1440
        / pi
        * 0.0820
        * (1 + 0.033 * cos(angle))
        * (
            sunset * sin(lat) * sin(declination)
            + cos(lat) * cos(declination) * sin(sunset)
        )
    )
    clear_sky = (0.75 + 2e-5 * elevation) * extraterrestrial
    cloud = max(0.3, min(1, solar / clear_sky)) if clear_sky > 0 else 0.3
    longwave = (
        4.903e-9
        * ((high + 273.16) ** 4 + (low + 273.16) ** 4)
        / 2
        * (0.34 - 0.14 * vapor**0.5)
        * (1.35 * cloud - 0.35)
    )
    wind_2m = wind * 4.87 / log(67.8 * 10 - 5.42)
    slope = 4098 * saturation(temp) / (temp + 237.3) ** 2
    gamma = 0.000665 * pressure
    deficit = max(0, (saturation(low) + saturation(high)) / 2 - vapor)
    energy = 0.408 * slope * (0.77 * solar - longwave)
    aerodynamic = gamma * 900 / (temp + 273) * wind_2m * deficit
    return max(0.0, (energy + aerodynamic) / (slope + gamma * (1 + 0.34 * wind_2m)))


def midnight(day, zone):
    utc = datetime.combine(day, time.min, zone).astimezone(UTC)
    if utc.astimezone(zone).replace(tzinfo=None) != datetime.combine(day, time.min):
        raise ValueError(f"{day} has no local midnight in {zone.key}")
    if utc.minute or utc.second:
        raise ValueError(f"{day} midnight in {zone.key} falls off the UTC hourly grid")
    return utc


def aggregate(hours, day, start, stop, latitude, elevation):
    count = (stop - start) // HOUR
    if count <= 0:
        raise ValueError(f"Invalid local-day length on {day}")

    def values(key, offset, minimum, maximum):
        for index in range(offset, count + offset):
            stamp = start + index * HOUR
            value = hours.get(stamp, {}).get(key)
            if type(value) not in (float, int) or not isfinite(value):
                raise ValueError(
                    f"Incomplete local day {day}: {key} at {stamp.isoformat()}"
                )
            if not minimum <= value <= maximum:
                raise ValueError(f"Out-of-range {key} at {stamp.isoformat()}: {value}")
            yield value

    temps = list(values("temperature_2m", 0, -60, 60))
    low, high = min(temps), max(temps)
    vapor = mean(saturation(t) for t in values("dew_point_2m", 0, -100, 60))
    pressure = mean(values("surface_pressure", 0, 0.01, float("inf"))) / 10
    wind = mean(values("wind_speed_10m", 0, 0, float("inf")))
    rain = sum(
        max(0, value) for value in values("precipitation", 1, -0.0001, float("inf"))
    )
    solar = (
        sum(
            max(0, value)
            for value in values("shortwave_radiation", 1, -1 / 3600, float("inf"))
        )
        * 3600
        / 1e6
    )
    return dict(
        day=day.isoformat(),
        tmin_c=low,
        tmax_c=high,
        rain_mm=rain,
        radiation_mj_m2=solar,
        wind_ms=wind,
        vapor_pressure_hpa=10 * vapor,
        et0_mm=reference_et(
            day, latitude, elevation, low, high, vapor, pressure, wind, solar
        ),
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--latitude", type=float, required=True)
    parser.add_argument("--longitude", type=float, required=True)
    parser.add_argument("--timezone", type=ZoneInfo, required=True)
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--raw-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args()
    if not (-90 <= args.latitude <= 90 and -180 <= args.longitude <= 180):
        parser.error(
            "Coordinates must be WGS84 latitude [-90, 90] and longitude [-180, 180]"
        )
    if args.start > args.end or args.end >= datetime.now(args.timezone).date():
        parser.error("Provide an ordered range of completed local dates")
    if args.out.exists():
        parser.error("Output already exists. Choose a new --out path")
    try:
        dates = [
            args.start + offset * DAY
            for offset in range((args.end - args.start).days + 1)
        ]
        bounds = [midnight(day, args.timezone) for day in [*dates, args.end + DAY]]
        hours, sources, grid = acquire(args, bounds[0], bounds[-1])
        lat, lon, elevation = grid
        daily = {
            day.isoformat(): aggregate(
                hours, day, start, stop, args.latitude, elevation
            )
            for day, start, stop in zip(dates, bounds[:-1], bounds[1:], strict=True)
        }
        archive = dict(
            schema=1,
            dataset="open-meteo-era5",
            requested_point=[args.latitude, args.longitude],
            point=[lat, lon],
            elevation_m=elevation,
            timezone=args.timezone.key,
            start_date=args.start.isoformat(),
            end_date=args.end.isoformat(),
            files=sources,
            method="UTC hours; interval-ending precipitation/shortwave; local day; FAO-56 PM daily",
            missing_dates=[],
            daily=daily,
        )
        archive["fingerprint"] = sha256(encode(archive)).hexdigest()
        write_new(args.out, archive)
    except (ValueError, KeyError, OSError, TypeError, ImportError) as exc:
        parser.exit(1, f"{exc}\n")
    print(
        f"{len(daily)} complete local days, ERA5 grid {lat}, {lon}, {elevation} m, {args.out}"
    )


if __name__ == "__main__":
    main()
