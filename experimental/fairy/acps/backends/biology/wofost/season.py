"""Independent per-ridge PCSE season; no Native water or crop process is used."""

from __future__ import annotations
from typing import Any, Iterable, TYPE_CHECKING, TypedDict

if TYPE_CHECKING:
    from evaluate.scenarios.season import SeasonSpec
    from fairy.acps.backends.biology.contracts import (
        CropObservation,
        HarvestProjection,
        SoilRidgeObservation,
        SurveyObservation,
        WaterFlux,
    )
    from fairy.acps.ontology.contracts import JsonObject
    from fairy.acps.ontology.crops.lib.soybean import SoybeanSpec
    from fairy.acps.ontology.farm.spec import FarmSpec
from fairy.acps.backends.biology.contracts import SeasonBiologyBackend
from fairy.acps.ontology.contracts import JsonObject
from dataclasses import asdict, replace
from datetime import date, timedelta
import copy
from math import exp
from fairy.acps.backends.biology.contracts import (
    BackendCapabilities,
    RidgeStatus,
    SoilObservation,
    WeatherDay,
)
from fairy.acps.backends.biology.wofost.backend import WofostBackend
from fairy.acps.ontology.contracts import OperationRejected
from fairy.acps.ontology.farm.spec import finite


class WofostRidgeState(TypedDict):
    """适配器的管理/收获状态，不复制PCSE的水分或生长过程。

    pending/irrigation为毛水深mm；removed/mature为每垄干粮kg；
    mature_dry_kg为None表示尚未成熟，0表示成熟时确实无粮。
    """

    planted: bool
    harvested: bool
    pending_mm: float
    irrigation_mm: float
    removed_dry_kg: float
    mature_dry_kg: float | None
    moisture: float
    density: float


class WofostSeasonBackend(SeasonBiologyBackend):
    """64根独立PCSE土柱的全季管理适配。

    engines拥有水分/生长真值，rows仅保存播种、pending和成熟粮转移等管理状态；
    不叠加Native生长过程。恢复通过初始化和输入日志重建。"""

    engines: list[WofostBackend]  #: 每垄独立PCSE土柱，不与Native叠加生长。
    rows: list[WofostRidgeState]  #: 适配层管理状态，成熟毛粮单位kg/垄。
    journal: list[JsonObject]  #: 逐日外生输入与全田灌溉摘要，供Runtime核验。
    inputs: list[JsonObject]  #: 播种/逐垄灌溉/日结/收获的有序重建日志。
    next_day: str  #: 下一待结算业务日，必须与Runtime日期一致。

    capabilities = BackendCapabilities(
        "wofost",
        "spatial_Wofost81_WLP_CWB",
        "1+pcse6.0.13",
        actions=("irrigate", "full_season", "plant", "harvest"),
    )

    def __init__(self, farm: FarmSpec, crop: SoybeanSpec, start: str, season: SeasonSpec) -> None:
        """保存显式依赖与初态；模型服务调用发生在决策阶段而非构造阶段。"""
        self.farm, self.crop, self.config, self.start = farm, crop, season, start
        if season.cultivar != "Soybean_901_proxy":
            raise ValueError("Explicit Soybean_901_proxy required")
        self.ridge_area = farm.area_m2 / season.ridge_count
        self.engines: list[WofostBackend] = []
        for i in range(season.ridge_count):
            capacity, capture, efficiency = self.hydraulic_parameters(i)
            local = replace(farm, field_capacity_vwc=capacity, irrigation_efficiency=efficiency)
            self.engines.append(WofostBackend(local, crop, start, bare=True))
        self.rows: list[WofostRidgeState] = [
            dict(
                planted=False,
                harvested=False,
                pending_mm=0.0,
                irrigation_mm=0.0,
                removed_dry_kg=0.0,
                mature_dry_kg=None,
                moisture=0.30,
                density=0.0,
            )
            for _ in self.engines
        ]
        self.next_day = start
        self.journal: list[JsonObject] = []
        self.inputs: list[JsonObject] = []

    def hydraulic_parameters(self, ridge: int) -> tuple[float, float, float]:
        """取得指定垄的田间持水量、降雨捕获率和灌溉效率，未覆盖分区使用农场默认值。"""
        for first, last, capacity, capture, efficiency in self.config.hydraulic_zones:
            if first <= ridge <= last:
                return capacity, capture, efficiency
        return self.farm.field_capacity_vwc, 1.0, self.farm.irrigation_efficiency

    def indices(self, args: JsonObject) -> range:
        """校验闭区间垄号并展开；非法范围拒绝，不产生状态变化。"""
        first, last = args["start_ridge"], args["end_ridge"]
        if (
            type(first) is not int
            or type(last) is not int
            or not 0 <= first <= last < len(self.rows)
        ):
            raise OperationRejected("INVALID_RIDGE_RANGE")
        return range(first, last + 1)

    def _targets(self, indices: Iterable[int]) -> tuple[int, ...]:
        """验证目标垄唯一且在范围内，返回可重复遍历的不可变索引。"""
        ids = tuple(indices)
        if (
            not ids
            or len(ids) != len(set(ids))
            or any(type(i) is not int or not 0 <= i < len(self.rows) for i in ids)
        ):
            raise ValueError("Invalid ridge targets")
        return ids

    def _phase(self, i: int) -> str:
        """将PCSE发育/管理状态映射为统一粗阶段，不把DVS伪装成Native R1—R8。"""
        r, engine = self.rows[i], self.engines[i]
        if r["harvested"]:
            return "harvested"
        if not r["planted"]:
            return "unplanted"
        if r["mature_dry_kg"] is not None:
            return "mature"
        dvs = float(engine.model.get_variable("DVS"))
        if dvs < 0:
            return "pre_emergence"
        return "vegetative" if dvs < 1 else "reproductive"

    def management_state(self) -> tuple[RidgeStatus, ...]:
        """返回跨引擎管理状态，供可信物理作业/评价使用，不读取其他后端的私有字典。"""
        return tuple(
            RidgeStatus(
                i,
                r["planted"],
                r["harvested"],
                float(e.model.get_variable("SM")),
                r["pending_mm"],
                r["irrigation_mm"],
                r["removed_dry_kg"],
                self._phase(i),
                self._phase(i) == "mature",
            )
            for i, (r, e) in enumerate(zip(self.rows, self.engines))
        )

    def observe(self) -> SoilObservation:
        """读取公开观测；不推进业务时钟，不结算pending，也不更新生长。"""
        return SoilObservation(
            self.farm.plot_id,
            round(sum(r.root_vwc for r in self.management_state()) / len(self.rows), 3),
        )

    def field_observation(self, indices: Iterable[int]) -> list[CropObservation]:
        """返回目标垄的公开视觉阶段/适用含水率，不暴露内部生物量或潜在产量。"""
        return [
            dict(
                ridge_id=i,
                stage=self._phase(i),
                grain_moisture=(
                    round(self.rows[i]["moisture"], 3) if self._phase(i) == "mature" else None
                ),
            )
            for i in indices
        ]

    def soil_observation(self, indices: Iterable[int]) -> list[SoilRidgeObservation]:
        """返回目标垄的根区体积含水率，单位m³/m³，按公开精度取整。"""
        return [
            dict(ridge_id=i, root_vwc=round(float(self.engines[i].model.get_variable("SM")), 3))
            for i in indices
        ]

    def survey(self, indices: Iterable[int], thermal: bool = False) -> list[SurveyObservation]:
        """计算合成NDVI或冠层温差；非真实仪器模拟，不可用冠层温差用None表达。"""
        observations: list[SurveyObservation] = []
        for i in indices:
            r, engine = self.rows[i], self.engines[i]
            lai = float(engine.model.get_variable("LAI") or 0.0)
            available = (
                r["planted"] and not r["harvested"] and self._phase(i) != "mature" and lai > 0
            )
            if thermal:
                if not available:
                    observations.append(dict(ridge_id=i, canopy_air_delta_c=None, available=False))
                    continue
                capacity, _, _ = self.hydraulic_parameters(i)
                sm = float(engine.model.get_variable("SM"))
                value = round(
                    5
                    * (
                        1
                        - min(
                            1,
                            max(
                                0,
                                (sm - self.farm.wilting_point_vwc)
                                / (capacity - self.farm.wilting_point_vwc),
                            ),
                        )
                    ),
                    2,
                )
                observations.append(dict(ridge_id=i, canopy_air_delta_c=value))
            else:
                observations.append(
                    dict(
                        ridge_id=i,
                        ndvi=round(0.15 + 0.7 * (1 - exp(-0.6 * lai)) if available else 0.15, 3),
                    )
                )
        return observations

    def plant(self, indices: Iterable[int], density: float) -> None:
        """将选定垄登记为已播种，density单位株/m²；后端过程范围见类说明。"""
        ids = self._targets(indices)
        finite(density, "density", 0.001)
        if any(self.rows[i]["planted"] for i in ids):
            raise OperationRejected("RIDGE_ALREADY_PLANTED")
        from pcse import signals

        for i in ids:
            e = self.engines[i]
            e.model._send_signal(
                signal=signals.crop_start,
                day=e.model.day,
                crop_name="soybean",
                variety_name=self.crop.wofost_variety,
                crop_start_type="sowing",
                crop_end_type="maturity",
            )
            self.rows[i].update({"planted": True, "density": density})
        self.inputs.append(
            dict(kind="plant", day=self.next_day, indices=list(ids), density=density)
        )

    def queue_irrigation(self, indices: Iterable[int], depth: float) -> None:
        """按目标垄登记毛水深mm；本方法不推进时间，实际供水在统一日界结算。"""
        ids = self._targets(indices)
        finite(depth, "depth_mm", 0)
        for i in ids:
            self.rows[i]["pending_mm"] += depth
        self.inputs.append(dict(kind="irrigate", day=self.next_day, indices=list(ids), depth=depth))

    def project_harvest(self, indices: Iterable[int]) -> HarvestProjection:
        """只读计算可收获毛粮，返回干质量和水质量kg；未成熟垄拒绝。"""
        dry = water = 0.0
        for i in indices:
            if self._phase(i) != "mature":
                raise OperationRejected("CROP_NOT_HARVESTABLE")
            r = self.rows[i]
            amount = r["mature_dry_kg"]
            assert amount is not None  # _phase已确认成熟。
            dry += amount
            water += amount * r["moisture"] / (1 - r["moisture"])
        return dict(gross_dry_kg=dry, gross_water_kg=water)

    def harvest(self, indices: Iterable[int]) -> HarvestProjection:
        """移出整批成熟毛粮并标记收获；物理装备负责回收率、损失和容器。"""
        ids = self._targets(indices)
        batch = self.project_harvest(ids)
        for i in ids:
            r = self.rows[i]
            assert r["mature_dry_kg"] is not None  # project_harvest已验证整批成熟。
            r["removed_dry_kg"] += r["mature_dry_kg"]
            r["harvested"] = True
        self.inputs.append(dict(kind="harvest", day=self.next_day, indices=list(ids)))
        return batch

    def advance_day(self, weather: WeatherDay, irrigation_mm: float) -> WaterFlux:
        """消费恰好一个连续业务日，结算水量与适用的生长过程，返回mm通量/守恒残差。"""
        if weather.day != self.next_day or irrigation_mm != 0:
            raise ValueError("Invalid season daily forcing")
        totals: WaterFlux = {}
        gross = sum(r["pending_mm"] for r in self.rows) / len(self.rows)
        for i, (r, e) in enumerate(zip(self.rows, self.engines)):
            _, capture, _ = self.hydraulic_parameters(i)
            flux = e.advance_day(
                replace(weather, rain_mm=weather.rain_mm * capture), r["pending_mm"]
            )
            # Rain not captured by this soil column leaves as external surface runoff.
            excluded = weather.rain_mm * (1 - capture)
            flux["rain_mm"] = weather.rain_mm
            flux["runoff_mm"] += excluded
            for key, value in flux.items():
                totals[key] = totals.get(key, 0.0) + value / len(self.rows)
            r["irrigation_mm"] += r["pending_mm"]
            r["pending_mm"] = 0.0
            if r["planted"] and r["mature_dry_kg"] is None:
                dvs = e.model.get_variable("DVS")
                summaries = e.model.get_summary_output()
                if dvs is None and summaries:
                    dvs = summaries[-1]["DVS"]
                if dvs is not None and dvs >= 2:
                    mass = e.model.get_variable("TWSO")
                    if mass is None:
                        mass = summaries[-1]["TWSO"]
                    r["mature_dry_kg"] = float(mass) * self.ridge_area / 10000
            if self._phase(i) == "mature":
                r["moisture"] = min(
                    0.35, max(0.10, r["moisture"] - 0.018 + min(0.04, weather.rain_mm * 0.003))
                )
        self.inputs.append(dict(kind="day", day=weather.day, weather=asdict(weather)))
        self.journal.append(dict(weather=asdict(weather), irrigation_mm=gross))
        self.next_day = (date.fromisoformat(self.next_day) + timedelta(days=1)).isoformat()
        return totals

    def audit(self) -> JsonObject:
        """返回可信审计投影，用于守恒和恢复检查，不交给普通Agent。"""
        rows = self.management_state()
        return dict(
            next_day=self.next_day,
            root_vwc=sum(r.root_vwc for r in rows) / len(rows),
            irrigation_mm=sum(r.irrigation_mm for r in rows) / len(rows),
            model_status="uncalibrated Soybean_901 proxy; synthetic grain moisture and sensors",
            ridges=[
                dict(status=asdict(r), pcse=e.audit()["pcse"]) for r, e in zip(rows, self.engines)
            ],
        )

    def export_state(self) -> JsonObject:
        """导出可信恢复快照；此数据不是LLM公开观测。"""
        return dict(
            schema=1,
            capabilities=asdict(self.capabilities),
            crop_data_sha256=self.engines[0].provenance["sha256"],
            inputs=copy.deepcopy(self.inputs),
            summary=dict(next_day=self.next_day, rows=copy.deepcopy(self.rows)),
        )

    def load_state(self, payload: JsonObject) -> None:
        """验证版本与守恒后恢复内部状态；调用方必须提供匹配的场景和新实例。"""
        import json

        def check(expected: Any, actual: Any) -> None:
            # Rebuilt inputs and JSON summaries are deterministic at pinned versions.
            if json.dumps(expected, sort_keys=True, allow_nan=False) != json.dumps(
                actual, sort_keys=True, allow_nan=False
            ):
                raise ValueError("WOFOST reconstructed state mismatch")

        if self.inputs:
            raise ValueError("Restore requires fresh season backend")
        header = self.export_state()
        for key in ("schema", "capabilities", "crop_data_sha256"):
            check(header[key], payload[key])
        for entry in payload["inputs"]:
            if entry["day"] != self.next_day:
                raise ValueError("Noncontiguous season input journal")
            kind = entry["kind"]
            if kind == "day":
                self.advance_day(WeatherDay(**entry["weather"]), 0.0)
            elif kind == "plant":
                self.plant(entry["indices"], entry["density"])
            elif kind == "irrigate":
                self.queue_irrigation(entry["indices"], entry["depth"])
            elif kind == "harvest":
                self.harvest(entry["indices"])
            else:
                raise ValueError("Unknown WOFOST season input")
        check(self.export_state(), payload)
