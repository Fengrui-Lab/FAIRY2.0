"""Reduced single-bucket water balance; no legacy engine objects are reused."""

from __future__ import annotations
from typing import Final, Literal, TYPE_CHECKING, TypedDict, cast

if TYPE_CHECKING:
    from fairy.acps.backends.biology.contracts import WaterFlux, WeatherDay
    from fairy.acps.ontology.contracts import JsonObject
    from fairy.acps.ontology.crops.lib.soybean import SoybeanSpec
    from fairy.acps.ontology.farm.spec import FarmSpec
from fairy.acps.backends.biology.contracts import BiologyBackend
from fairy.acps.ontology.contracts import JsonObject
from dataclasses import asdict
from datetime import date, timedelta
import json
from math import isclose
from fairy.acps.backends.biology.contracts import BackendCapabilities, SoilObservation
from fairy.acps.ontology.farm.spec import finite


class NativeWaterState(TypedDict):
    """L1水桶恢复状态；日期为下一待结算日，储水/通量统一mm，gdd为°C·day。"""

    next_day: str
    storage_mm: float
    gdd: float
    rain_mm: float
    irrigation_mm: float
    application_loss_mm: float
    runoff_mm: float
    drainage_mm: float
    evaporation_mm: float
    transpiration_mm: float


WaterCounter = Literal[
    "rain_mm",
    "irrigation_mm",
    "application_loss_mm",
    "runoff_mm",
    "drainage_mm",
    "evaporation_mm",
    "transpiration_mm",
]
WATER_COUNTERS: Final[tuple[WaterCounter, ...]] = (
    "rain_mm",
    "irrigation_mm",
    "application_loss_mm",
    "runoff_mm",
    "drainage_mm",
    "evaporation_mm",
    "transpiration_mm",
)


class NativeBackend(BiologyBackend):
    """L1固定冠层的简化根区水桶。

    state中的储水和累计通量为mm，gdd为°C·day；不计算全季籽粒或施肥响应。"""

    state: NativeWaterState  #: 根区水桶与累计通量，只有本后端负责日更新。
    farm: FarmSpec  #: 固定面积、根深和水力边界，m/m²/m³·m⁻³。
    crop: SoybeanSpec  #: 简化冠层、温度阈值与PCSE品种选择。

    capabilities = BackendCapabilities("native", "single_bucket_gdd", "1")

    def __init__(self, farm: FarmSpec, crop: SoybeanSpec, sowing_date: str) -> None:
        """保存显式依赖与初态；模型服务调用发生在决策阶段而非构造阶段。"""
        self.farm, self.crop = farm, crop
        self.sowing_date = sowing_date
        self.state: NativeWaterState = {
            "next_day": sowing_date,
            "storage_mm": farm.initial_vwc * farm.root_depth_m * 1000,
            "gdd": 0.0,
            "rain_mm": 0.0,
            "irrigation_mm": 0.0,
            "application_loss_mm": 0.0,
            "runoff_mm": 0.0,
            "drainage_mm": 0.0,
            "evaporation_mm": 0.0,
            "transpiration_mm": 0.0,
        }

    def observe(self) -> SoilObservation:
        """读取公开观测；不推进业务时钟，不结算pending，也不更新生长。"""
        return SoilObservation(self.farm.plot_id, round(self.audit()["root_vwc"], 3))

    def advance_day(self, weather: WeatherDay, irrigation_mm: float) -> WaterFlux:
        """消费恰好一个连续业务日，结算水量与适用的生长过程，返回mm通量/守恒残差。"""
        if weather.day != self.state["next_day"]:
            raise ValueError("Non-contiguous daily forcing")
        finite(irrigation_mm, "irrigation_mm", 0)
        s, f, c = self.state, self.farm, self.crop
        before = s["storage_mm"]
        depth = f.root_depth_m * 1000
        effective = irrigation_mm * f.irrigation_efficiency
        water = before + weather.rain_mm + effective
        runoff = max(0.0, water - f.saturation_vwc * depth)
        water -= runoff
        drainage = max(0.0, water - f.field_capacity_vwc * depth)
        water -= drainage  # freely draining daily bucket
        demand = weather.et0_mm * c.crop_coefficient
        evaporation = min(
            demand * (1 - c.canopy_cover), max(0.0, water - f.wilting_point_vwc * depth)
        )
        water -= evaporation
        stress = min(
            1.0,
            max(
                0.0,
                (water / depth - f.wilting_point_vwc)
                / (f.field_capacity_vwc - f.wilting_point_vwc),
            ),
        )
        transpiration = min(
            demand * c.canopy_cover * stress, max(0.0, water - f.wilting_point_vwc * depth)
        )
        water -= transpiration
        loss = irrigation_mm - effective
        residual = (
            before
            + weather.rain_mm
            + irrigation_mm
            - water
            - runoff
            - drainage
            - evaporation
            - transpiration
            - loss
        )
        flux = dict(
            rain_mm=weather.rain_mm,
            irrigation_mm=irrigation_mm,
            application_loss_mm=loss,
            runoff_mm=runoff,
            drainage_mm=drainage,
            evaporation_mm=evaporation,
            transpiration_mm=transpiration,
        )
        for key in WATER_COUNTERS:
            s[key] += flux[key]
        s["storage_mm"] = water
        s["gdd"] += max(0.0, (weather.tmin_c + weather.tmax_c) / 2 - c.base_temperature_c)
        s["next_day"] = (date.fromisoformat(weather.day) + timedelta(days=1)).isoformat()
        return {**flux, "balance_error_mm": residual}

    def audit(self) -> JsonObject:
        """返回可信审计投影，用于守恒和恢复检查，不交给普通Agent。"""
        s, c = self.state, self.crop
        gdd = s["gdd"]
        return {
            **s,
            "root_vwc": s["storage_mm"] / (self.farm.root_depth_m * 1000),
            "root_depth_m": self.farm.root_depth_m,
            "stage": "pre_emergence" if gdd < c.emergence_gdd else "vegetative_or_later",
            "thermal_progress": min(1.0, gdd / c.maturity_gdd),
            "progress_semantics": "GDD fraction, not WOFOST DVS",
        }

    def export_state(self) -> JsonObject:
        """导出可信恢复快照；此数据不是LLM公开观测。"""
        return {"schema": 1, "capabilities": asdict(self.capabilities), "state": dict(self.state)}

    def load_state(self, payload: JsonObject) -> None:
        """验证版本与守恒后恢复内部状态；调用方必须提供匹配的场景和新实例。"""
        if payload.get("schema") != 1 or json.dumps(
            payload.get("capabilities"), sort_keys=True
        ) != json.dumps(asdict(self.capabilities), sort_keys=True):
            raise ValueError("Incompatible Native state")
        state = payload["state"]
        if set(state) != set(self.state):
            raise ValueError("Incomplete Native state")
        next_day = date.fromisoformat(state["next_day"])
        if next_day < date.fromisoformat(self.sowing_date):
            raise ValueError("Native clock precedes sowing")
        for key, value in state.items():
            if key != "next_day":
                finite(value, key, 0)
        depth = self.farm.root_depth_m * 1000
        initial = self.farm.initial_vwc * depth
        lower = self.farm.wilting_point_vwc * depth
        upper = (
            self.farm.initial_vwc
            if next_day == date.fromisoformat(self.sowing_date)
            else self.farm.field_capacity_vwc
        ) * depth
        if not lower - 1e-8 <= state["storage_mm"] <= upper + 1e-8:
            raise ValueError("Native storage outside modeled bounds")
        expected = (
            initial
            + state["rain_mm"]
            + state["irrigation_mm"]
            - sum(
                state[k]
                for k in (
                    "application_loss_mm",
                    "runoff_mm",
                    "drainage_mm",
                    "evaporation_mm",
                    "transpiration_mm",
                )
            )
        )
        if not isclose(state["storage_mm"], expected, rel_tol=1e-10, abs_tol=1e-8):
            raise ValueError("Native water balance mismatch")
        if not isclose(
            state["application_loss_mm"],
            state["irrigation_mm"] * (1 - self.farm.irrigation_efficiency),
            rel_tol=1e-10,
            abs_tol=1e-8,
        ):
            raise ValueError("Native irrigation efficiency mismatch")
        if next_day == date.fromisoformat(self.sowing_date):
            if any(state[k] != 0 for k in state if k not in ("next_day", "storage_mm")):
                raise ValueError("Nonzero Native totals before first day")
        self.state = cast(NativeWaterState, dict(state))
