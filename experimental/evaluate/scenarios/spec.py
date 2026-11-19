"""A scenario is a declaration; it never constructs backend objects."""

from __future__ import annotations
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from fairy.acps.ontology.contracts import JsonObject
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from fairy.acps.ontology.farm.spec import FarmSpec, finite
from fairy.acps.ontology.crops.lib.soybean import SoybeanSpec
from fairy.acps.backends.biology.contracts import WeatherDay
from fairy.acps.ontology.contracts import ObjectSpec, OperationBinding
from evaluate.scenarios.season import SeasonSpec


@dataclass(frozen=True)
class ScenarioSpec:
    """不可变场景输入规格；声明任务、天气、对象、操作与预算，不构建引擎。"""

    scenario_id: str
    task: str
    farm: FarmSpec
    crop: SoybeanSpec
    sowing_date: str
    start_time: str
    deadline: str
    weather: tuple[WeatherDay, ...]
    task_type: str = "irrigation"
    season: SeasonSpec | None = None
    timezone: str = "Asia/Shanghai"
    irrigation_quota_mm: float = 45.0
    target_vwc_min: float = 0.25
    target_vwc_max: float = 0.32
    max_advance_hours: float = 48.0
    seed: int = 0
    prefix_irrigation: tuple[tuple[str, float], ...] = ()
    tools: tuple[str, ...] = ("observe_soil", "irrigate", "advance_time")

    objects: tuple[ObjectSpec, ...] = ()
    operation_bindings: tuple[OperationBinding, ...] = ()
    public_weather: tuple[WeatherDay, ...] = ()
    weather_in_context: bool = True
    required_queries: tuple[str, ...] = ()
    construction_json: str | None = None

    def __post_init__(self) -> None:
        """验证声明的单位范围、身份与结构；非法输入明确报错，不静默修正。"""
        for name in (
            "weather",
            "prefix_irrigation",
            "objects",
            "operation_bindings",
            "public_weather",
            "tools",
            "required_queries",
        ):
            if not isinstance(getattr(self, name), tuple):
                raise ValueError(f"{name} must be an immutable tuple")
        if any(not isinstance(w, WeatherDay) for w in (*self.weather, *self.public_weather)):
            raise ValueError("Weather entries must be WeatherDay declarations")
        if any(not isinstance(entry, tuple) or len(entry) != 2 for entry in self.prefix_irrigation):
            raise ValueError("Prefix entries must be immutable day/depth pairs")
        if any(not isinstance(o, ObjectSpec) for o in self.objects) or any(
            not isinstance(b, OperationBinding) for b in self.operation_bindings
        ):
            raise ValueError("Invalid object or operation declaration")
        if any(
            not isinstance(name, str) or not name.isidentifier()
            for name in (*self.tools, *self.required_queries)
        ):
            raise ValueError("Operation names must be identifiers")
        if type(self.seed) is not int:
            raise ValueError("Scenario seed must be an integer")
        if any(
            not isinstance(value, str) or not value.strip()
            for value in (self.scenario_id, self.task)
        ):
            raise ValueError("Scenario identity and task are required")
        zone = ZoneInfo(self.timezone)
        start, end = datetime.fromisoformat(self.start_time), datetime.fromisoformat(self.deadline)
        if start.tzinfo is None or end.tzinfo is None or end <= start:
            raise ValueError("Scenario needs aware start/deadline timestamps in order")
        if (
            start.utcoffset() != start.astimezone(zone).utcoffset()
            or end.utcoffset() != end.astimezone(zone).utcoffset()
        ):
            raise ValueError("Timestamp offsets must agree with scenario timezone")
        first = date.fromisoformat(self.sowing_date)
        last = end.astimezone(zone).date()
        expected = tuple(
            (first + timedelta(days=i)).isoformat() for i in range((last - first).days + 1)
        )
        if tuple(w.day for w in self.weather) != expected:
            raise ValueError("Weather must cover sowing through deadline, contiguously")
        if self.task_type not in ("irrigation", "full_season"):
            raise ValueError("Unsupported task type")
        if self.task_type == "irrigation":
            if self.season is not None or first >= start.astimezone(zone).date():
                raise ValueError("L1 requires a pre-run from an earlier sowing date")
        elif (
            not isinstance(self.season, SeasonSpec)
            or first != start.astimezone(zone).date()
            or self.prefix_irrigation
        ):
            raise ValueError("Full season starts unplanted, without a management prefix")
        if self.season and any(
            not self.farm.wilting_point_vwc < z[2] < self.farm.saturation_vwc
            for z in self.season.hydraulic_zones
        ):
            raise ValueError("Zone field capacity must be between wilting and saturation")
        if len({day for day, _ in self.prefix_irrigation}) != len(self.prefix_irrigation):
            raise ValueError("Duplicate prefix irrigation day")
        for day, amount in self.prefix_irrigation:
            parsed = date.fromisoformat(day)
            if day != parsed.isoformat() or day not in expected:
                raise ValueError("Prefix management needs a canonical weather date")
            if not self.sowing_date <= day < start.astimezone(zone).date().isoformat():
                raise ValueError("Prefix management must precede the task")
            finite(amount, "prefix irrigation", 0)
        finite(self.max_advance_hours, "max_advance_hours", 0.001, 8784)
        finite(self.irrigation_quota_mm, "quota", 0)
        finite(
            self.target_vwc_min,
            "target_vwc_min",
            self.farm.wilting_point_vwc,
            self.farm.saturation_vwc,
        )
        finite(self.target_vwc_max, "target_vwc_max", self.target_vwc_min, self.farm.saturation_vwc)
        for name in (
            "objects",
            "operation_bindings",
            "public_weather",
            "tools",
            "required_queries",
        ):
            if not isinstance(getattr(self, name), tuple):
                raise ValueError(f"{name} must be an immutable tuple")
        if len(set(self.tools)) != len(self.tools) or not self.tools:
            raise ValueError("Exposed operations must be non-empty and unique")
        if tuple(b.operation for b in self.operation_bindings) != self.tools:
            raise ValueError("Operation bindings must match the exposed operations")
        if type(self.weather_in_context) is not bool:
            raise ValueError("weather_in_context must be boolean")
        if not set(self.required_queries) <= {"get_current_weather", "get_forecast"} & set(
            self.tools
        ):
            raise ValueError("Required weather queries must be exposed")
        days = tuple(w.day for w in self.public_weather)
        if days != tuple(sorted(set(days))):
            raise ValueError("Public weather reports must have unique ordered dates")
        if self.construction_json is not None:
            from evaluate.scenarios.compose import validate_construction

            try:
                validate_construction(self)
            except (KeyError, TypeError, AttributeError) as exc:
                raise ValueError("Malformed scenario construction metadata") from exc

    def to_dict(self) -> JsonObject:
        """返回脱离内部对象的序列化声明，供来源指纹或可信恢复使用。"""
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Any) -> ScenarioSpec:
        """从可信JSON字段重建声明/事件并验证结构，不执行模型或农事。"""
        value = dict(value)
        if value.get("season") is not None:
            value["season"] = dict(value["season"])
            value["season"]["hydraulic_zones"] = tuple(
                tuple(z) for z in value["season"].get("hydraulic_zones", ())
            )
            value["season"] = SeasonSpec(**value["season"])
        value["farm"] = FarmSpec(**value["farm"])
        value["crop"] = SoybeanSpec(**value["crop"])
        value["weather"] = tuple(WeatherDay(**day) for day in value["weather"])
        value["prefix_irrigation"] = tuple(
            tuple(entry) for entry in value.get("prefix_irrigation", ())
        )
        value["tools"] = tuple(value["tools"])
        value["required_queries"] = tuple(value["required_queries"])
        value["objects"] = tuple(ObjectSpec(**item) for item in value["objects"])
        value["operation_bindings"] = tuple(
            OperationBinding(**item) for item in value["operation_bindings"]
        )
        value["public_weather"] = tuple(WeatherDay(**item) for item in value["public_weather"])
        return cls(**value)
