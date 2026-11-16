"""Declared public daily weather reports; never reads private backend forcing."""
from dataclasses import asdict, dataclass
from datetime import timedelta
from fairy.acps.backends.biology.contracts import WeatherDay
from fairy.acps.ontology.contracts import ObjectSpec, OperationDefinition, OperationRejected, Parameter

CURRENT_WEATHER = OperationDefinition(
    "get_current_weather", "Read the declared daily synthetic weather report for the local date, not an instantaneous station reading.", (),
    (Parameter("weather", "object"), Parameter("source", "string"), Parameter("time", "string")), "read_only")
FORECAST = OperationDefinition(
    "get_forecast", "Read declared synthetic forecasts for the next 1-7 local dates, excluding today. May return fewer days when public data ends.",
    (Parameter("days", "integer", 1, 7),),
    (Parameter("forecast", "array"), Parameter("source", "string"), Parameter("issued_at", "string"),
     Parameter("requested_days", "integer", 1, 7), Parameter("available_days", "integer", 0, 7)), "read_only")


@dataclass(frozen=True)
class WeatherService:
    spec: ObjectSpec
    reports: tuple[WeatherDay, ...]
    operations = (CURRENT_WEATHER, FORECAST)

    def current(self, now):
        report = next((w for w in self.reports if w.day == now.date().isoformat()), None)
        if report is None:
            raise OperationRejected("WEATHER_UNAVAILABLE")
        return {"weather": asdict(report), "source": "declared_synthetic_daily_report", "time": now.isoformat()}

    def forecast(self, now, days):
        dates = {(now.date() + timedelta(days=i)).isoformat() for i in range(1, days + 1)}
        reports = [asdict(w) for w in self.reports if w.day in dates]
        return {"forecast": reports, "source": "declared_synthetic_forecast",
                "issued_at": now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(),
                "requested_days": days, "available_days": len(reports)}
