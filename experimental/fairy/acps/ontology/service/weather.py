"""Declared public daily weather reports; never reads private backend forcing."""

from __future__ import annotations
from typing import ClassVar
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime
    from fairy.acps.ontology.contracts import JsonObject
from dataclasses import asdict, dataclass
from datetime import timedelta
from fairy.acps.backends.biology.contracts import WeatherDay
from fairy.acps.ontology.contracts import (
    ObjectSpec,
    OperationDefinition,
    OperationRejected,
    Parameter,
)

CURRENT_WEATHER = OperationDefinition(
    "get_current_weather",
    "Read the declared daily synthetic weather report for the local date, not an instantaneous station reading.",
    (),
    (Parameter("weather", "object"), Parameter("source", "string"), Parameter("time", "string")),
    "read_only",
)
FORECAST = OperationDefinition(
    "get_forecast",
    "Read declared synthetic forecasts for the next 1-7 local dates, excluding today. May return fewer days when public data ends.",
    (Parameter("days", "integer", 1, 7),),
    (
        Parameter("forecast", "array"),
        Parameter("source", "string"),
        Parameter("issued_at", "string"),
        Parameter("requested_days", "integer", 1, 7),
        Parameter("available_days", "integer", 0, 7),
    ),
    "read_only",
)


@dataclass(frozen=True)
class WeatherService:
    """仅发布场景显式声明的天气报告；不读取后端的完整未来forcing。"""

    spec: ObjectSpec
    reports: tuple[WeatherDay, ...]
    operations: ClassVar[tuple[OperationDefinition, ...]] = (CURRENT_WEATHER, FORECAST)

    def current(self, now: datetime) -> JsonObject:
        """读取当前业务日期已声明的天气报告；没有公开数据时拒绝。"""
        report = next((w for w in self.reports if w.day == now.date().isoformat()), None)
        if report is None:
            raise OperationRejected("WEATHER_UNAVAILABLE")
        return {
            "weather": asdict(report),
            "source": "declared_synthetic_daily_report",
            "time": now.isoformat(),
        }

    def forecast(self, now: datetime, days: int) -> JsonObject:
        """读取未来指定天数的已发布报告，排除当天；不补造缺失天气。"""
        dates = {(now.date() + timedelta(days=i)).isoformat() for i in range(1, days + 1)}
        reports = [asdict(w) for w in self.reports if w.day in dates]
        return {
            "forecast": reports,
            "source": "declared_synthetic_forecast",
            "issued_at": now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(),
            "requested_days": days,
            "available_days": len(reports),
        }


@dataclass(frozen=True)
class HistoricalWeatherService(WeatherService):
    """Expose completed historical days only, without treating hindsight as a forecast."""

    operations: ClassVar[tuple[OperationDefinition, ...]] = (
        OperationDefinition(
            "get_current_weather",
            "Read the completed prior local day's historical reanalysis. The current day and future forcing are not published.",
            (),
            CURRENT_WEATHER.results,
            "read_only",
        ),
    )

    def current(self, now: datetime) -> JsonObject:
        completed = (now.date() - timedelta(days=1)).isoformat()
        report = next((w for w in self.reports if w.day == completed), None)
        if report is None:
            raise OperationRejected("WEATHER_UNAVAILABLE")
        return {
            "weather": asdict(report),
            "source": "historical_reanalysis",
            "time": now.isoformat(),
        }
