"""Spatial, reduced soybean season. Explicitly uncalibrated; no legacy imports.

Water is conserved per ridge. Radiation-driven growth supplies dry grain;
no preset yield, maximum-ever yield proxy, or yield-based success score.
"""

from __future__ import annotations
from typing import Iterable, Literal, Mapping, TYPE_CHECKING, TypedDict, cast

if TYPE_CHECKING:
    from evaluate.scenarios.season import SeasonSpec
    from fairy.acps.backends.biology.contracts import (
        CropObservation,
        HarvestProjection,
        RidgeStatus,
        SoilRidgeObservation,
        SurveyObservation,
        WaterFlux,
        WeatherDay,
    )
    from fairy.acps.ontology.contracts import JsonObject
    from fairy.acps.ontology.crops.lib.soybean import SoybeanSpec
    from fairy.acps.ontology.farm.spec import FarmSpec
from fairy.acps.backends.biology.contracts import SeasonBiologyBackend
from fairy.acps.ontology.contracts import JsonObject
from dataclasses import asdict
from datetime import date, timedelta
from math import exp
import copy
from fairy.acps.backends.biology.contracts import BackendCapabilities, SoilObservation
from fairy.acps.ontology.contracts import OperationRejected


class NativeRidgeState(TypedDict):
    """逐垄内部状态；仅生物后端写入，不作为Agent公开观测。

    storage/pending/累计水通量：mm；LAI：m²/m²；干物质/籽粒：g/m²；
    gdd：°C·day；moisture：湿基质量分数；density：株/m²。
    """

    planted: bool
    harvested: bool
    gdd: float
    lai: float
    dry_matter_g_m2: float
    grain_dry_g_m2: float
    grain_removed_g_m2: float
    storage_mm: float
    pending_mm: float
    moisture: float
    density: float
    rain_mm: float
    irrigation_mm: float
    runoff_mm: float
    drainage_mm: float
    evap_mm: float
    transp_mm: float
    application_loss_mm: float


class NativeSeasonState(TypedDict):
    """全季快照；next_day由统一日结驱动，ridges保持场景固定顺序。"""

    next_day: str
    ridges: list[NativeRidgeState]


RidgeWaterCounter = Literal[
    "rain_mm",
    "irrigation_mm",
    "application_loss_mm",
    "runoff_mm",
    "drainage_mm",
    "evap_mm",
    "transp_mm",
]
RIDGE_COUNTERS: Mapping[str, RidgeWaterCounter] = {
    "rain_mm": "rain_mm",
    "irrigation_mm": "irrigation_mm",
    "application_loss_mm": "application_loss_mm",
    "runoff_mm": "runoff_mm",
    "drainage_mm": "drainage_mm",
    "evaporation_mm": "evap_mm",
    "transpiration_mm": "transp_mm",
}


class NativeSeasonBackend(SeasonBiologyBackend):
    """逐垄简化大豆全季模型。

    state由本后端独占写入；水量守恒与RUE生长各日更新一次。密度/播深响应、
    养分与缺氧尚未实现，恢复一致不等同田间校准。"""

    state: NativeSeasonState  #: 下一待结算日及逐垄完整状态，可信恢复时验证守恒。
    farm: FarmSpec
    crop: SoybeanSpec
    config: SeasonSpec  #: 全季参数；密度/播深生物响应限制见过程审计。

    capabilities = BackendCapabilities(
        "native",
        "ridge_water_rue_soybean",
        "2",
        actions=("irrigate", "full_season", "plant", "harvest"),
    )

    def __init__(self, farm: FarmSpec, crop: SoybeanSpec, start: str, season: SeasonSpec) -> None:
        """保存显式依赖与初态；模型服务调用发生在决策阶段而非构造阶段。"""
        self.farm, self.crop, self.config = farm, crop, season
        self.state: NativeSeasonState = {
            "next_day": start,
            "ridges": [
                dict(
                    planted=False,
                    harvested=False,
                    gdd=0.0,
                    lai=0.0,
                    dry_matter_g_m2=0.0,
                    grain_dry_g_m2=0.0,
                    grain_removed_g_m2=0.0,
                    storage_mm=farm.initial_vwc * farm.root_depth_m * 1000,
                    pending_mm=0.0,
                    moisture=0.30,
                    density=0.0,
                    rain_mm=0.0,
                    irrigation_mm=0.0,
                    runoff_mm=0.0,
                    drainage_mm=0.0,
                    evap_mm=0.0,
                    transp_mm=0.0,
                    application_loss_mm=0.0,
                )
                for _ in range(season.ridge_count)
            ],
        }

    @property
    def ridge_area(self) -> float:
        """每垄面积m²，按农场总面积与场景垄数计算。"""
        return self.farm.area_m2 / self.config.ridge_count

    def hydraulic_parameters(self, ridge: int) -> tuple[float, float, float]:
        """取得指定垄的田间持水量、降雨捕获率和灌溉效率，未覆盖分区使用农场默认值。"""
        for first, last, capacity, capture, efficiency in self.config.hydraulic_zones:
            if first <= ridge <= last:
                return capacity, capture, efficiency
        return self.farm.field_capacity_vwc, 1.0, self.farm.irrigation_efficiency

    def indices(self, args: JsonObject) -> range:
        """校验闭区间垄号并展开；非法范围拒绝，不产生状态变化。"""
        start, end = args["start_ridge"], args["end_ridge"]
        if not 0 <= start <= end < self.config.ridge_count:
            raise OperationRejected("INVALID_RIDGE_RANGE")
        return range(start, end + 1)

    def stage(self, r: NativeRidgeState) -> str:
        """按本引擎状态计算细分阶段；不推进积温或生长。"""
        if r["harvested"]:
            return "harvested"
        if not r["planted"]:
            return "unplanted"
        g = r["gdd"]
        if g < self.config.emergence_gdd:
            return "pre_emergence"
        for fraction, stage in (
            (0.35, "vegetative"),
            (0.55, "R1"),
            (0.70, "R3"),
            (0.83, "R5"),
            (0.94, "R6"),
            (1, "R7"),
        ):
            if g < self.config.maturity_gdd * fraction:
                return stage
        return "R8"

    def management_state(self) -> tuple[RidgeStatus, ...]:
        """返回跨引擎管理状态，供可信物理作业/评价使用，不读取其他后端的私有字典。"""
        from fairy.acps.backends.biology.contracts import RidgeStatus

        def phase(r: NativeRidgeState) -> str:
            stage = self.stage(r)
            return (
                stage
                if stage in ("unplanted", "pre_emergence", "vegetative", "harvested")
                else ("mature" if stage == "R8" else "reproductive")
            )

        return tuple(
            RidgeStatus(
                i,
                r["planted"],
                r["harvested"],
                r["storage_mm"] / (self.farm.root_depth_m * 1000),
                r["pending_mm"],
                r["irrigation_mm"],
                r["grain_removed_g_m2"] * self.ridge_area / 1000,
                phase(r),
                self.stage(r) == "R8",
            )
            for i, r in enumerate(self.state["ridges"])
        )

    def observe(self) -> SoilObservation:
        """读取公开观测；不推进业务时钟，不结算pending，也不更新生长。"""
        return SoilObservation(self.farm.plot_id, round(self.audit()["root_vwc"], 3))

    def field_observation(self, indices: Iterable[int]) -> list[CropObservation]:
        """Synthetic visual/moisture inspection, not hidden biomass or potential yield."""
        return [
            dict(
                ridge_id=i,
                stage=self.stage(r),
                grain_moisture=round(r["moisture"], 3) if self.stage(r) in ("R7", "R8") else None,
            )
            for i in indices
            for r in (self.state["ridges"][i],)
        ]

    def soil_observation(self, indices: Iterable[int]) -> list[SoilRidgeObservation]:
        """返回目标垄的根区体积含水率，单位m³/m³，按公开精度取整。"""
        return [
            dict(
                ridge_id=i,
                root_vwc=round(
                    self.state["ridges"][i]["storage_mm"] / (self.farm.root_depth_m * 1000), 3
                ),
            )
            for i in indices
        ]

    def survey(self, indices: Iterable[int], thermal: bool = False) -> list[SurveyObservation]:
        """计算合成NDVI或冠层温差；非真实仪器模拟，不可用冠层温差用None表达。"""
        rows: list[SurveyObservation] = []
        for i in indices:
            r = self.state["ridges"][i]
            if thermal and (not r["planted"] or r["harvested"] or r["lai"] == 0):
                rows.append(dict(ridge_id=i, canopy_air_delta_c=None, available=False))
                continue
            if thermal:
                vwc = r["storage_mm"] / (self.farm.root_depth_m * 1000)
                capacity, _, _ = self.hydraulic_parameters(i)
                value = round(
                    5
                    * (
                        1
                        - min(
                            1,
                            max(
                                0,
                                (vwc - self.farm.wilting_point_vwc)
                                / (capacity - self.farm.wilting_point_vwc),
                            ),
                        )
                    ),
                    2,
                )
                rows.append(dict(ridge_id=i, canopy_air_delta_c=value))
            else:
                rows.append(
                    dict(ridge_id=i, ndvi=round(0.15 + 0.7 * (1 - exp(-0.6 * r["lai"])), 3))
                )
        return rows

    def queue_irrigation(self, indices: Iterable[int], depth: float) -> None:
        """按目标垄登记毛水深mm；本方法不推进时间，实际供水在统一日界结算。"""
        for i in indices:
            self.state["ridges"][i]["pending_mm"] += depth

    def plant(self, indices: Iterable[int], density: float) -> None:
        """将选定垄登记为已播种，density单位株/m²；后端过程范围见类说明。"""
        for i in indices:
            self.state["ridges"][i].update({"planted": True, "density": density})

    def project_harvest(self, indices: Iterable[int]) -> HarvestProjection:
        """Gross mature grain available for removal, without equipment losses."""
        dry = wet = 0.0
        for i in indices:
            r = self.state["ridges"][i]
            if self.stage(r) != "R8":
                raise OperationRejected("CROP_NOT_HARVESTABLE")
            available = r["grain_dry_g_m2"] * self.ridge_area / 1000
            dry += available
            wet += available / (1 - r["moisture"])
        return dict(gross_dry_kg=dry, gross_water_kg=wet - dry)

    def harvest(self, indices: Iterable[int]) -> HarvestProjection:
        """Remove gross grain once; physical machinery owns recovery and loss."""
        indices = tuple(indices)
        batch = self.project_harvest(indices)
        for i in indices:
            r = self.state["ridges"][i]
            r["grain_removed_g_m2"] += r["grain_dry_g_m2"]
            r.update({"grain_dry_g_m2": 0.0, "harvested": True, "lai": 0.0})
        return batch

    def advance_day(self, weather: WeatherDay, irrigation_mm: float) -> WaterFlux:
        """消费恰好一个连续业务日，结算水量与适用的生长过程，返回mm通量/守恒残差。"""
        if weather.day != self.state["next_day"]:
            raise ValueError("Non-contiguous daily forcing")
        if irrigation_mm != 0:
            raise ValueError("Season irrigation must be allocated by ridge")
        f, c = self.farm, self.config
        depth = f.root_depth_m * 1000
        totals = {
            k: 0.0
            for k in (
                "rain_mm",
                "irrigation_mm",
                "application_loss_mm",
                "runoff_mm",
                "drainage_mm",
                "evaporation_mm",
                "transpiration_mm",
                "balance_error_mm",
            )
        }
        for i, r in enumerate(self.state["ridges"]):
            before = r["storage_mm"]
            fc, capture, efficiency = self.hydraulic_parameters(i)
            gross = r["pending_mm"]
            loss = gross * (1 - efficiency)
            water = before + weather.rain_mm * capture + gross - loss
            runoff = weather.rain_mm * (1 - capture) + max(0, water - f.saturation_vwc * depth)
            water = min(water, f.saturation_vwc * depth)
            drainage = max(0, water - fc * depth)
            water -= drainage
            cover = 1 - exp(-0.6 * r["lai"])
            stress = min(
                1, max(0, (water / depth - f.wilting_point_vwc) / (fc - f.wilting_point_vwc))
            )
            evap = min(weather.et0_mm * (1 - cover), max(0, water - f.wilting_point_vwc * depth))
            water -= evap
            transp = min(
                weather.et0_mm * cover * stress, max(0, water - f.wilting_point_vwc * depth)
            )
            water -= transp
            residual = (
                before + weather.rain_mm + gross - water - loss - runoff - drainage - evap - transp
            )
            flux = dict(
                rain_mm=weather.rain_mm,
                irrigation_mm=gross,
                application_loss_mm=loss,
                runoff_mm=runoff,
                drainage_mm=drainage,
                evaporation_mm=evap,
                transpiration_mm=transp,
                balance_error_mm=residual,
            )
            for key, value in flux.items():
                totals[key] += value / c.ridge_count
                state_key = RIDGE_COUNTERS.get(key)
                if state_key is not None:
                    r[state_key] += value
            r.update({"storage_mm": water, "pending_mm": 0.0})
            if r["planted"] and not r["harvested"]:
                r["gdd"] += max(
                    0, (weather.tmin_c + weather.tmax_c) / 2 - self.crop.base_temperature_c
                )
                progress = min(1, r["gdd"] / c.maturity_gdd)
                if self.stage(r) == "R8":
                    # Grain dry mass freezes at maturity; weather alters grain water only.
                    r["moisture"] = min(
                        0.35, max(0.10, r["moisture"] - 0.018 + min(0.04, weather.rain_mm * 0.003))
                    )
                    r["lai"] *= 0.90
                elif r["gdd"] >= c.emergence_gdd:
                    if r["lai"] == 0:
                        r["lai"] = 0.10
                    intercepted = 1 - exp(-0.6 * r["lai"])
                    growth_stress = (
                        transp / (weather.et0_mm * cover) if weather.et0_mm * cover > 0 else 0.0
                    )
                    increment = (
                        weather.radiation_mj_m2
                        * 0.48
                        * intercepted
                        * c.rue_g_mj_par
                        * growth_stress
                    )
                    r["dry_matter_g_m2"] += increment
                    if progress >= 0.55:
                        r["grain_dry_g_m2"] += increment * c.grain_partition
                    if progress < 0.83:
                        r["lai"] = min(
                            c.max_lai,
                            r["lai"] + 0.059 * r["lai"] * (1 - r["lai"] / c.max_lai) * stress,
                        )
                    else:
                        r["lai"] *= 0.965
        self.state["next_day"] = (date.fromisoformat(weather.day) + timedelta(days=1)).isoformat()
        return totals

    def audit(self) -> JsonObject:
        """返回可信审计投影，用于守恒和恢复检查，不交给普通Agent。"""
        return {
            **copy.deepcopy(self.state),
            "root_vwc": sum(r["storage_mm"] for r in self.state["ridges"])
            / (self.config.ridge_count * self.farm.root_depth_m * 1000),
            "model_status": "uncalibrated reduced full-season process",
        }

    def export_state(self) -> JsonObject:
        """导出可信恢复快照；此数据不是LLM公开观测。"""
        return dict(
            schema=1, capabilities=asdict(self.capabilities), state=copy.deepcopy(self.state)
        )

    def load_state(self, payload: JsonObject) -> None:
        """验证版本与守恒后恢复内部状态；调用方必须提供匹配的场景和新实例。"""
        import json
        from fairy.acps.ontology.farm.spec import finite

        if payload.get("schema") != 1 or json.dumps(
            payload.get("capabilities"), sort_keys=True
        ) != json.dumps(asdict(self.capabilities), sort_keys=True):
            raise ValueError("Incompatible season backend")
        state = copy.deepcopy(payload["state"])
        date.fromisoformat(state["next_day"])
        if set(state) != set(self.state) or len(state["ridges"]) != self.config.ridge_count:
            raise ValueError("Incomplete spatial state")
        for r in state["ridges"]:
            if set(r) != set(self.state["ridges"][0]):
                raise ValueError("Incomplete ridge state")
            for k, v in r.items():
                if k in ("planted", "harvested"):
                    if type(v) is not bool:
                        raise ValueError("Invalid crop flags")
                else:
                    finite(v, k, 0)
            if r["harvested"] and not r["planted"]:
                raise ValueError("Invalid crop history")
            if r["storage_mm"] > self.farm.saturation_vwc * self.farm.root_depth_m * 1000 + 1e-8:
                raise ValueError("Invalid soil storage")
            from math import isclose

            expected = (
                self.farm.initial_vwc * self.farm.root_depth_m * 1000
                + r["rain_mm"]
                + r["irrigation_mm"]
                - r["application_loss_mm"]
                - r["runoff_mm"]
                - r["drainage_mm"]
                - r["evap_mm"]
                - r["transp_mm"]
            )
            if not isclose(expected, r["storage_mm"], abs_tol=1e-7):
                raise ValueError("Soil water conservation mismatch")
            if r["harvested"] and r["grain_dry_g_m2"] != 0:
                raise ValueError("Harvested grain still in field")
            if r["grain_dry_g_m2"] + r["grain_removed_g_m2"] > r["dry_matter_g_m2"] + 1e-8:
                raise ValueError("Grain exceeds assimilated dry matter")
            if r["moisture"] >= 1:
                raise ValueError("Invalid moisture")
        self.state = cast(NativeSeasonState, state)
