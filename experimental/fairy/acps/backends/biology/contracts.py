"""Backend contract: one call consumes exactly one completed physical day."""

from __future__ import annotations
from typing import (
    Callable,
    Iterable,
    Literal,
    Mapping,
    Protocol,
    TYPE_CHECKING,
    TypeAlias,
    TypedDict,
    runtime_checkable,
    overload,
)

if TYPE_CHECKING:
    from .native.backend import NativeBackend
    from .native.season import NativeSeasonBackend
    from .wofost.backend import WofostBackend
    from .wofost.season import WofostSeasonBackend
    from evaluate.scenarios.season import SeasonSpec
from dataclasses import dataclass
from datetime import date
from fairy.acps.ontology.contracts import JsonObject
from fairy.acps.ontology.farm.spec import FarmSpec
from fairy.acps.ontology.crops.lib.soybean import SoybeanSpec

from fairy.acps.ontology.farm.spec import finite


@dataclass(frozen=True)
class WeatherDay:
    """一个完整业务日的外生天气。

    温度°C，雨/参考蒸散mm/day，辐射MJ/m²/day，风速m/s，水汽压hPa；
    此对象不表示已向Agent发布的预报。"""

    day: str
    tmin_c: float = 16.0
    tmax_c: float = 28.0
    rain_mm: float = 0.0
    radiation_mj_m2: float = 18.0
    wind_ms: float = 2.0
    vapor_pressure_hpa: float = 12.0
    et0_mm: float = 3.0

    def __post_init__(self) -> None:
        """验证声明的单位范围、身份与结构；非法输入明确报错，不静默修正。"""
        date.fromisoformat(self.day)
        finite(self.tmin_c, "tmin_c", -60, 60)
        finite(self.tmax_c, "tmax_c", self.tmin_c, 60)
        for name in (
            "rain_mm",
            "radiation_mj_m2",
            "wind_ms",
            "vapor_pressure_hpa",
            "et0_mm",
        ):
            finite(getattr(self, name), name, 0)


@dataclass(frozen=True)
class BackendCapabilities:
    """引擎身份、版本、作物与动作能力声明；不同后端不承诺数值相等。"""

    backend_id: str
    model: str
    version: str
    crops: tuple[str, ...] = ("soybean",)
    actions: tuple[str, ...] = ("irrigate",)
    timestep: str = "local_day"


@dataclass(frozen=True)
class SoilObservation:
    """公开根区土壤观测；root_vwc为体积含水率m³/m³，非未测生长状态。"""

    plot_id: str
    root_vwc: float
    measurement: str = "root_zone_vwc_rounded_0.001"
    unit: str = "m3/m3"


@dataclass(frozen=True)
class RidgeStatus:
    """跨后端可信管理投影。

    root_vwc为m³/m³，pending/irrigation为毛水深mm，removed_dry_kg为已移出的每垄干粮；
    phase采用统一粗阶段，harvestable由各引擎确定，不向Agent自动公开全部字段。"""

    ridge_id: int
    planted: bool
    harvested: bool
    root_vwc: float
    pending_mm: float
    irrigation_mm: float
    removed_dry_kg: float
    phase: str
    harvestable: bool


class SoilReading(TypedDict):
    """公开根区观测；root_vwc为m³/m³，time带业务时区。"""

    plot_id: str
    root_vwc: float
    measurement: str
    unit: str
    time: str


class CropObservation(TypedDict):
    """逐垄公开作物状态；stage保留各引擎语义，不暴露生物量。"""

    ridge_id: int
    stage: str
    grain_moisture: float | None


class SoilRidgeObservation(TypedDict):
    """单垄根区体积含水率；不代表某一深度的真实传感器。"""

    ridge_id: int
    root_vwc: float


class SeedbedObservation(TypedDict):
    """Modeled topsoil temperature and moisture at the selected ridge."""

    ridge_id: int
    seedbed_temperature_c: float
    seedbed_vwc: float | None
    minimum_temperature_c: float
    temperature_ready: bool


@runtime_checkable
class SeedbedBiologyBackend(Protocol):
    """Optional topsoil capability used by explicit planting-window scenarios."""

    def seedbed_observation(
        self, indices: Iterable[int]
    ) -> list[SeedbedObservation]: ...


class SurfaceWaterObservation(TypedDict):
    """Public standing water and elapsed crop exposure on one ridge."""

    ridge_id: int
    standing_water_mm: float
    crop_waterlogging_days: int
    drainage_enabled: bool


@runtime_checkable
class SurfaceDrainageBackend(Protocol):
    """Opt-in surface water projection and managed outflow."""

    def surface_water_observation(
        self, indices: Iterable[int]
    ) -> list[SurfaceWaterObservation]: ...
    def drain_surface(
        self, indices: Iterable[int], max_depth_mm: float
    ) -> dict[int, float]: ...
    def managed_surface_drains(self) -> tuple[float, ...]: ...


class BioticObservation(TypedDict):
    """Visible simulated scouting indices and remaining treatment protection time."""

    ridge_id: int
    insect_pressure: float | None
    disease_pressure: float | None
    insect_protection_days: int
    disease_protection_days: int


@runtime_checkable
class BioticManagementBackend(Protocol):
    """Optional field scouting and day-settled crop protection."""

    def observe_biotic(self, indices: Iterable[int]) -> list[BioticObservation]: ...
    def queue_biotic_treatment(self, indices: Iterable[int], kind: str) -> int: ...
    def protection_ledger(self) -> tuple[int, dict[int, str]]: ...


class StandObservation(TypedDict):
    """Visible emerged stand, relative to nominal seed sites."""

    ridge_id: int
    established_fraction: float | None
    original_stage: str
    replant_stage: str | None


@runtime_checkable
class StandRepairBackend(Protocol):
    """Optional stand scouting and repair against a recorded partial placement gap."""

    def observe_stand(self, indices: Iterable[int]) -> list[StandObservation]: ...
    def plan_replant(self, indices: Iterable[int]) -> dict[int, int]: ...
    def replant(self, indices: Iterable[int]) -> dict[int, int]: ...
    def replant_ledger(self) -> dict[int, int]: ...


class SurveyObservation(TypedDict, total=False):
    """合成遥感/热成像；不可用的冠层温差用None而非0表达。"""

    ridge_id: int
    ndvi: float
    canopy_air_delta_c: float | None
    available: bool


class HarvestProjection(TypedDict):
    """生物后端可收获毛粮，单位kg；机械回收与损失由物理后端处理。"""

    gross_dry_kg: float
    gross_water_kg: float


WaterFlux: TypeAlias = dict[str, float]  # 每日通量及守恒残差，统一mm。


class BiologyBackend(Protocol):
    """L1/全季共同接口；观测无副作用，advance_day只消费一个完整本地日。

    audit/export_state是可信侧JSON边界，不能直接交给公开Agent。
    具体实现通过显式工厂懒加载；类型导入不会强制加载PCSE。
    """

    capabilities: BackendCapabilities  # 固定引擎身份、版本与支持动作。

    def observe(self) -> SoilObservation:
        """读取公开根区观测，不更新作物或时钟。"""
        ...

    def advance_day(self, weather: WeatherDay, irrigation_mm: float) -> WaterFlux:
        """结算唯一一天的水量/生长；全季灌溉由逐垄pending提供。"""
        ...

    def audit(self) -> JsonObject:
        """取得可信审计状态；不属于公开观测契约。"""
        ...

    def export_state(self) -> JsonObject:
        """导出版本化恢复数据，不调用Agent。"""
        ...

    def load_state(self, payload: JsonObject) -> None:
        """验证并恢复到新实例；非法或不兼容输入明确报错。"""
        ...


class SeasonBiologyBackend(BiologyBackend, Protocol):
    """全季附加能力；继承所有共同后端方法，避免丢失类型信息。"""

    def management_state(self) -> tuple[RidgeStatus, ...]:
        """可信管理投影；物理作业依赖此接口而非引擎私有字典。"""
        ...

    def indices(self, args: JsonObject) -> range:
        """验证并展开闭区间垄号；拒绝非法范围。"""
        ...

    def plant(self, indices: Iterable[int], density: float) -> None:
        """登记播种，density单位株/m²；具体生长响应受后端能力限制。"""
        ...

    def queue_irrigation(self, indices: Iterable[int], depth: float) -> None:
        """登记目标垄毛水深mm，等待Runtime跨午夜结算。"""
        ...

    def project_harvest(self, indices: Iterable[int]) -> HarvestProjection:
        """只读计算成熟毛粮，不扣除田间粮食。"""
        ...

    def harvest(self, indices: Iterable[int]) -> HarvestProjection:
        """一次性移出成熟毛粮，物理侧负责回收损失和容器转移。"""
        ...

    def field_observation(self, indices: Iterable[int]) -> list[CropObservation]: ...
    def soil_observation(
        self, indices: Iterable[int]
    ) -> list[SoilRidgeObservation]: ...
    def survey(
        self, indices: Iterable[int], thermal: bool = False
    ) -> list[SurveyObservation]: ...


BackendFactory: TypeAlias = Callable[[FarmSpec, SoybeanSpec, str], BiologyBackend]


def _native(farm: FarmSpec, crop: SoybeanSpec, sowing_date: str) -> NativeBackend:
    """按需导入并构建Native L1；具体返回类型供IDE导航与工厂静态检查。"""
    from .native.backend import NativeBackend

    return NativeBackend(farm, crop, sowing_date)


def _wofost(farm: FarmSpec, crop: SoybeanSpec, sowing_date: str) -> WofostBackend:
    """仅在选择WOFOST时加载PCSE适配器，不给Native运行增加可选依赖。"""
    from .wofost.backend import WofostBackend

    return WofostBackend(farm, crop, sowing_date)


class BiologyBackendRegistry:
    """Frozen explicit backend factories. Imports occur only for the selected backend."""

    def __init__(self, entries: Iterable[tuple[str, BackendFactory]]) -> None:
        """保存显式依赖与初态；模型服务调用发生在决策阶段而非构造阶段。"""
        from types import MappingProxyType

        factories: dict[str, BackendFactory] = {}
        for name, factory in entries:
            if name in factories:
                raise ValueError(f"Duplicate backend: {name}")
            if not isinstance(name, str) or not name or not callable(factory):
                raise ValueError("Invalid backend registration")
            factories[name] = factory
        self.factories: Mapping[str, BackendFactory] = MappingProxyType(factories)

    def create(
        self, name: str, farm: FarmSpec, crop: SoybeanSpec, sowing_date: str
    ) -> BiologyBackend:
        """从显式目录构建新后端并验证身份/必要方法，不静默替换引擎。"""
        if name not in self.factories:
            raise ValueError(f"Unsupported backend: {name}")
        backend = self.factories[name](farm, crop, sowing_date)
        if backend.capabilities.backend_id != name:
            raise ValueError("Backend identity disagrees with registration")
        for method in ("observe", "advance_day", "audit", "export_state", "load_state"):
            if not callable(getattr(backend, method, None)):
                raise ValueError(f"Backend missing interface: {method}")
        return backend


BIOLOGY_BACKENDS = BiologyBackendRegistry((("native", _native), ("wofost", _wofost)))


@overload
def make_backend(
    name: Literal["native"],
    farm: FarmSpec,
    crop: SoybeanSpec,
    sowing_date: str,
    season: None = None,
) -> NativeBackend: ...
@overload
def make_backend(
    name: Literal["wofost"],
    farm: FarmSpec,
    crop: SoybeanSpec,
    sowing_date: str,
    season: None = None,
) -> WofostBackend: ...
@overload
def make_backend(
    name: Literal["native"],
    farm: FarmSpec,
    crop: SoybeanSpec,
    sowing_date: str,
    season: SeasonSpec,
) -> NativeSeasonBackend: ...
@overload
def make_backend(
    name: Literal["wofost"],
    farm: FarmSpec,
    crop: SoybeanSpec,
    sowing_date: str,
    season: SeasonSpec,
) -> WofostSeasonBackend: ...
@overload
def make_backend(
    name: str, farm: FarmSpec, crop: SoybeanSpec, sowing_date: str, season: SeasonSpec
) -> SeasonBiologyBackend: ...
@overload
def make_backend(
    name: str,
    farm: FarmSpec,
    crop: SoybeanSpec,
    sowing_date: str,
    season: SeasonSpec | None = None,
) -> BiologyBackend: ...


def make_backend(
    name: str,
    farm: FarmSpec,
    crop: SoybeanSpec,
    sowing_date: str,
    season: SeasonSpec | None = None,
) -> BiologyBackend:
    """按显式名称与场景选择实现；不支持的组合拒绝，不自动降级。"""
    if season is not None:
        if name == "wofost":
            if season.cultivar != "Soybean_901_proxy":
                raise ValueError(
                    "WOFOST full season requires an explicitly declared Soybean_901_proxy scenario; no fallback"
                )
            from .wofost.season import WofostSeasonBackend

            return WofostSeasonBackend(farm, crop, sowing_date, season)
        if name != "native":
            raise ValueError("Unsupported full-season backend; no fallback")
        if season.cultivar != "HEINONG58":
            raise ValueError("Native full season requires HEINONG58 proxy; no fallback")
        from .native.season import NativeSeasonBackend

        return NativeSeasonBackend(farm, crop, sowing_date, season)
    return BIOLOGY_BACKENDS.create(name, farm, crop, sowing_date)


def restore_backend(
    name: str,
    farm: FarmSpec,
    crop: SoybeanSpec,
    sowing_date: str,
    payload: JsonObject,
    season: SeasonSpec | None = None,
) -> BiologyBackend:
    """新建匹配后端再加载恢复包；不会复用其他场景的实例。"""
    backend = make_backend(name, farm, crop, sowing_date, season)
    backend.load_state(payload)
    return backend
