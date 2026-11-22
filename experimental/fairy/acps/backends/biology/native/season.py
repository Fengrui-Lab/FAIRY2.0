"""FAIRY2.0 season adapter for the ported FAIRY soil and canopy processes."""

from __future__ import annotations
from typing import Iterable, Literal, Mapping, TYPE_CHECKING, TypedDict, cast

if TYPE_CHECKING:
    from evaluate.scenarios.season import SeasonSpec
    from fairy.acps.backends.biology.contracts import (
        CropObservation,
        BioticObservation,
        HarvestProjection,
        RidgeStatus,
        SoilRidgeObservation,
        StandObservation,
        SeedbedObservation,
        SurfaceWaterObservation,
        SurveyObservation,
        WaterFlux,
        WeatherDay,
    )
    from fairy.acps.ontology.contracts import JsonObject
    from fairy.acps.ontology.crops.lib.soybean import SoybeanSpec
    from fairy.acps.ontology.farm.spec import FarmSpec
from fairy.acps.backends.biology.contracts import SeasonBiologyBackend
from fairy.acps.ontology.contracts import JsonObject
from dataclasses import asdict, replace
from datetime import date, timedelta
from math import ceil, exp
import copy
from fairy.acps.backends.biology.contracts import BackendCapabilities, SoilObservation
from fairy.acps.ontology.contracts import OperationRejected
from .canopy_biomass_engine import (
    DEFAULT_SEED_GROWTH_PARAMS,
    CanopyBiomassGrowthEngine,
    CanopyBiomassState,
    GrowthSoilInput,
    GrowthStage,
    GrowthWeatherInput,
    ManagementStressInput,
    PhenologyInput,
    SeedType,
)
from .soil_engine import RidgeSoilState, SoilEngine, SoilParameters, WeatherInput

from .biotic_pressure_engine import (
    BioticPressureEngine,
    BioticPressureParameters,
    BioticPressureState,
    BioticCropInput,
    BioticSoilInput,
    BioticWeatherInput,
    GrowthStage as BioticStage,
    TreatmentApplication,
    TreatmentType,
)

TREATMENTS = {"insect": TreatmentType.INSECTICIDE, "disease": TreatmentType.FUNGICIDE}

CANOPY_STAGES = {
    "unplanted": GrowthStage.NOT_PLANTED,
    "pre_emergence": GrowthStage.PLANTED_PRE_EMERGENCE,
    "vegetative": GrowthStage.V4_PLUS,
    "R1": GrowthStage.R1,
    "R3": GrowthStage.R3,
    "R5": GrowthStage.R5,
    "R6": GrowthStage.R6,
    "R7": GrowthStage.R7,
    "R8": GrowthStage.R8,
}


class NativeCropState(TypedDict):
    """One cohort's development and biomass per unit of its occupied area."""

    planted: bool
    harvested: bool
    gdd: float
    lai: float
    dry_matter_g_m2: float
    grain_dry_g_m2: float
    moisture: float
    density: float


class NativeRidgeState(NativeCropState):
    """The primary cohort plus the shared ridge soil and management counters."""

    grain_removed_g_m2: float
    storage_mm: float
    top_storage_mm: float
    top_temp_c: float
    root_temp_c: float
    pending_mm: float
    rain_mm: float
    irrigation_mm: float
    runoff_mm: float
    drainage_mm: float
    evap_mm: float
    transp_mm: float
    application_loss_mm: float
    ponded_mm: float
    managed_surface_drain_mm: float
    waterlogging_days: int


class RidgeBioticState(TypedDict):
    pressure: JsonObject
    pending: str | None


class StandRepairState(TypedDict):
    skipped_seeds: int
    replant: NativeCropState | None


class NativeSeasonState(TypedDict):
    """全季快照；next_day由统一日结驱动，ridges保持场景固定顺序。"""

    next_day: str
    ridges: list[NativeRidgeState]
    biotic: list[RidgeBioticState]
    protection_used: int
    stands: list[StandRepairState]


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
    """原版双层土壤与冠层引擎，由 2.0 的统一时钟结算。

    快照是唯一动态状态。积水对生长的作用采用显式工程参数；养分、病虫和播深未接入。
    """

    state: NativeSeasonState  #: 下一待结算日及逐垄完整状态，可信恢复时验证守恒。
    farm: FarmSpec
    crop: SoybeanSpec
    config: SeasonSpec
    top_depth_mm = SoilParameters().top_depth_m * 1000

    capabilities = BackendCapabilities(
        "native",
        "fairy_legacy_soil_canopy_soybean",
        "6",
        actions=(
            "irrigate",
            "full_season",
            "plant",
            "harvest",
            "seedbed_observation",
            "surface_drainage",
            "biotic_management",
            "stand_repair",
        ),
    )

    def __init__(
        self, farm: FarmSpec, crop: SoybeanSpec, start: str, season: SeasonSpec
    ) -> None:
        """保存显式依赖与初态；模型服务调用发生在决策阶段而非构造阶段。"""
        self.farm, self.crop, self.config = farm, crop, season
        self._canopy = CanopyBiomassGrowthEngine(
            num_ridges=0,
            seed_params={
                SeedType.HEINONG58: replace(
                    DEFAULT_SEED_GROWTH_PARAMS[SeedType.HEINONG58],
                    max_lai=season.max_lai,
                    rue_g_mj_apar=season.rue_g_mj_par,
                    harvest_index=season.grain_partition,
                )
            },
        )
        self._soil = tuple(self._make_soil(i) for i in range(season.ridge_count))
        self._ponding: dict[int, tuple[float, float]] = {
            i: (capacity, passive)
            for first, last, capacity, passive in season.ponding_zones
            for i in range(first, last + 1)
        }
        self._biotic_engine = (
            BioticPressureEngine(
                num_ridges=0, params=BioticPressureParameters(weed_base_growth=0.0)
            )
            if season.biotic_outbreaks
            else None
        )
        self._outbreaks: dict[str, dict[int, dict[str, float]]] = {}
        for day, start_ridge, end_ridge, kind, pressure in season.biotic_outbreaks:
            daily = self._outbreaks.setdefault(day, {})
            for ridge_id in range(start_ridge, end_ridge + 1):
                daily.setdefault(ridge_id, {})[kind] = pressure
        self._seed_sites = ceil(2 * farm.length_m / (season.seed_spacing_cm / 100))
        self._planter_skips = {
            i: min(self._seed_sites - 1, round(self._seed_sites * fraction))
            for start_ridge, end_ridge, fraction in season.planter_skip_zones
            for i in range(start_ridge, end_ridge + 1)
        }
        self.state: NativeSeasonState = {
            "next_day": start,
            "biotic": (
                [
                    {
                        "pressure": asdict(BioticPressureState(i, weed_pressure=0.0)),
                        "pending": None,
                    }
                    for i in range(season.ridge_count)
                ]
                if self._biotic_engine is not None
                else []
            ),
            "protection_used": 0,
            "stands": (
                [
                    {"skipped_seeds": 0, "replant": None}
                    for _ in range(season.ridge_count)
                ]
                if season.planter_skip_zones
                else []
            ),
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
                    top_storage_mm=farm.initial_vwc * self.top_depth_mm,
                    top_temp_c=10.0,
                    root_temp_c=9.0,
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
                    ponded_mm=0.0,
                    managed_surface_drain_mm=0.0,
                    waterlogging_days=0,
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

    def _make_soil(self, ridge: int) -> SoilEngine:
        """Bind the source hydraulic kernel to this ridge's declared parameters."""
        capacity, capture, efficiency = self.hydraulic_parameters(ridge)
        farm = self.farm
        return SoilEngine(
            num_ridges=0,
            params=SoilParameters(
                root_depth_m=farm.root_depth_m,
                wilting_point_vwc=farm.wilting_point_vwc,
                field_capacity_vwc=capacity,
                saturation_vwc=farm.saturation_vwc,
                water_stress_vwc=min(
                    capacity,
                    max(
                        0.18,
                        (
                            (farm.wilting_point_vwc + capacity) / 2
                            if farm.wilting_point_vwc >= 0.18
                            else 0.18
                        ),
                    ),
                ),
                rainfall_capture_efficiency=capture,
                irrigation_efficiency=efficiency,
            ),
        )

    def indices(self, args: JsonObject) -> range:
        """校验闭区间垄号并展开；非法范围拒绝，不产生状态变化。"""
        start, end = args["start_ridge"], args["end_ridge"]
        if not 0 <= start <= end < self.config.ridge_count:
            raise OperationRejected("INVALID_RIDGE_RANGE")
        return range(start, end + 1)

    def stage(self, r: NativeCropState) -> str:
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

    def _cohorts(self, i: int) -> tuple[tuple[NativeCropState, float], ...]:
        original = self.state["ridges"][i]
        if not self.state["stands"]:
            return ((original, 1.0),)
        stand = self.state["stands"][i]
        missed = stand["skipped_seeds"] / self._seed_sites
        replanted = stand["replant"]
        base = ((original, 1.0 - missed),)
        return base if replanted is None else (*base, (replanted, missed))

    def _ridge_stage(self, i: int) -> str:
        """A ridge is ready for whole-ridge harvest when its latest cohort matures."""
        cohorts = self._cohorts(i)
        return self.stage(cohorts[-1][0])

    def _ridge_grain(self, i: int) -> tuple[float, float]:
        dry = 0.0
        wet = 0.0
        for crop, fraction in self._cohorts(i):
            mass = crop["grain_dry_g_m2"] * fraction
            dry += mass
            wet += mass / (1.0 - crop["moisture"])
        return dry, wet

    def observe_stand(self, indices: Iterable[int]) -> list[StandObservation]:
        """Expose the visible stand after emergence and each cohort's own stage."""
        if not self.state["stands"]:
            raise OperationRejected("STAND_REPAIR_UNAVAILABLE")
        observations: list[StandObservation] = []
        for i in indices:
            cohorts = self._cohorts(i)
            original = self.stage(cohorts[0][0])
            latest = self.stage(cohorts[-1][0]) if len(cohorts) > 1 else None
            visible = original not in ("unplanted", "pre_emergence", "harvested")
            established = sum(
                fraction
                for crop, fraction in cohorts
                if self.stage(crop) not in ("unplanted", "pre_emergence", "harvested")
            )
            observations.append(
                dict(
                    ridge_id=i,
                    established_fraction=round(established, 3) if visible else None,
                    original_stage=original,
                    replant_stage=latest,
                )
            )
        return observations

    def plan_replant(self, indices: Iterable[int]) -> dict[int, int]:
        """Read the number of unfilled seed sites; reject unsafe repeated or late work."""
        if not self.state["stands"]:
            raise OperationRejected("STAND_REPAIR_UNAVAILABLE")
        missing = {}
        for i in indices:
            stand = self.state["stands"][i]
            if not stand["skipped_seeds"] or stand["replant"] is not None:
                raise OperationRejected("NO_UNFILLED_SEED_SITES")
            if self.stage(self.state["ridges"][i]) != "vegetative":
                raise OperationRejected("STAND_REPAIR_WINDOW_CLOSED")
            missing[i] = stand["skipped_seeds"]
        return missing

    def replant(self, indices: Iterable[int]) -> dict[int, int]:
        missing = self.plan_replant(indices)
        for i in missing:
            self.state["stands"][i]["replant"] = dict(
                planted=True,
                harvested=False,
                gdd=0.0,
                lai=0.0,
                dry_matter_g_m2=0.0,
                grain_dry_g_m2=0.0,
                moisture=0.30,
                density=self.state["ridges"][i]["density"],
            )
        return missing

    def replant_ledger(self) -> dict[int, int]:
        return {
            i: row["skipped_seeds"]
            for i, row in enumerate(self.state["stands"])
            if row["replant"] is not None
        }

    def management_state(self) -> tuple[RidgeStatus, ...]:
        """返回跨引擎管理状态，供可信物理作业/评价使用，不读取其他后端的私有字典。"""
        from fairy.acps.backends.biology.contracts import RidgeStatus

        def phase(i: int) -> str:
            stage = self._ridge_stage(i)
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
                phase(i),
                self._ridge_stage(i) == "R8",
            )
            for i, r in enumerate(self.state["ridges"])
        )

    def observe(self) -> SoilObservation:
        """读取公开观测；不推进业务时钟，不结算pending，也不更新生长。"""
        return SoilObservation(self.farm.plot_id, round(self.audit()["root_vwc"], 3))

    def field_observation(self, indices: Iterable[int]) -> list[CropObservation]:
        """Synthetic visual/moisture inspection, not hidden biomass or potential yield."""
        rows: list[CropObservation] = []
        for i in indices:
            stage = self._ridge_stage(i)
            dry, wet = self._ridge_grain(i)
            original = self.state["ridges"][i]
            moisture = (
                (1.0 - dry / wet)
                if len(self._cohorts(i)) > 1 and wet
                else original["moisture"]
            )
            rows.append(
                dict(
                    ridge_id=i,
                    stage=stage,
                    grain_moisture=(
                        round(moisture, 3) if stage in ("R7", "R8") else None
                    ),
                )
            )
        return rows

    def soil_observation(self, indices: Iterable[int]) -> list[SoilRidgeObservation]:
        """返回目标垄的根区体积含水率，单位m³/m³，按公开精度取整。"""
        return [
            dict(
                ridge_id=i,
                root_vwc=round(
                    self.state["ridges"][i]["storage_mm"]
                    / (self.farm.root_depth_m * 1000),
                    3,
                ),
            )
            for i in indices
        ]

    def seedbed_observation(self, indices: Iterable[int]) -> list[SeedbedObservation]:
        """Report topsoil at the same precision used by the planting gate."""
        rows: list[SeedbedObservation] = []
        for i in indices:
            state = self.state["ridges"][i]
            minimum = self._soil[i].params.planting_temp_min_c
            temp = round(state["top_temp_c"], 2)
            rows.append(
                {
                    "ridge_id": i,
                    "seedbed_temperature_c": temp,
                    "seedbed_vwc": round(
                        state["top_storage_mm"] / self.top_depth_mm, 3
                    ),
                    "minimum_temperature_c": minimum,
                    "temperature_ready": temp >= minimum,
                }
            )
        return rows

    def observe_biotic(self, indices: Iterable[int]) -> list[BioticObservation]:
        """Expose simulated scouting values only when living crop is present."""
        if self._biotic_engine is None:
            raise OperationRejected("BIOTIC_MANAGEMENT_UNAVAILABLE")
        result: list[BioticObservation] = []
        for i in indices:
            value = self.state["biotic"][i]["pressure"]
            visible = self.stage(self.state["ridges"][i]) not in (
                "unplanted",
                "pre_emergence",
                "harvested",
            )
            result.append(
                {
                    "ridge_id": i,
                    "insect_pressure": (
                        round(value["insect_pressure"], 3) if visible else None
                    ),
                    "disease_pressure": (
                        round(value["disease_pressure"], 3) if visible else None
                    ),
                    "insect_protection_days": value["insecticide_residual_days_left"],
                    "disease_protection_days": value["fungicide_residual_days_left"],
                }
            )
        return result

    def queue_biotic_treatment(self, indices: Iterable[int], kind: str) -> int:
        """Reserve one abstract unit per eligible ridge, with no same-day effect."""
        if self._biotic_engine is None:
            raise OperationRejected("BIOTIC_MANAGEMENT_UNAVAILABLE")
        if kind not in TREATMENTS:
            raise OperationRejected("UNKNOWN_PROTECTION_KIND")
        ids = tuple(indices)
        if any(
            self.stage(self.state["ridges"][i])
            not in ("vegetative", "R1", "R3", "R5", "R6")
            for i in ids
        ):
            raise OperationRejected("CROP_NOT_ELIGIBLE_FOR_PROTECTION")
        if any(self.state["biotic"][i]["pending"] is not None for i in ids):
            raise OperationRejected("BIOTIC_TREATMENT_ALREADY_QUEUED")
        if (
            self.state["protection_used"] + len(ids)
            > self.config.plant_protection_units
        ):
            raise OperationRejected("INSUFFICIENT_PROTECTION_STOCK")
        for i in ids:
            self.state["biotic"][i]["pending"] = kind
        self.state["protection_used"] += len(ids)
        return len(ids)

    def protection_ledger(self) -> tuple[int, dict[int, str]]:
        pending = {}
        for i, row in enumerate(self.state["biotic"]):
            if row["pending"] is not None:
                pending[i] = row["pending"]
        return self.state["protection_used"], pending

    def _biotic_day(
        self,
        i: int,
        weather: WeatherInput,
        soil: RidgeSoilState,
        stage: GrowthStage,
        cover: float,
    ) -> dict[str, float]:
        """Run the ported pressure kernel from the authoritative ridge snapshot."""
        if self._biotic_engine is None:
            return {}
        row = self.state["biotic"][i]
        state = BioticPressureState(**row["pressure"])
        pressure = self._outbreaks.get(weather.day.isoformat(), {}).get(i, {})
        state.insect_pressure = max(state.insect_pressure, pressure.get("insect", 0.0))
        state.disease_pressure = max(
            state.disease_pressure, pressure.get("disease", 0.0)
        )
        pending = row["pending"]
        treatment = (
            [] if pending is None else [TreatmentApplication(TREATMENTS[pending])]
        )
        result = self._biotic_engine._update_ridge_day(
            state,
            BioticWeatherInput(
                weather.day,
                weather.air_temp_mean_c,
                weather.rain_mm,
                weather.rain_mm > 0,
            ),
            BioticCropInput(BioticStage(stage.value), cover, self.config.cultivar),
            BioticSoilInput(soil.top_vwc, soil.root_vwc),
            treatment,
        )
        row["pressure"] = asdict(state)
        row["pending"] = None
        return {
            "biotic_stress": result.biotic_stress_multiplier,
            "insect_pressure": result.insect_pressure,
            "disease_pressure": result.disease_pressure,
        }

    def surface_water_observation(
        self, indices: Iterable[int]
    ) -> list[SurfaceWaterObservation]:
        """Project completed standing-water and crop-exposure states."""
        return [
            dict(
                ridge_id=i,
                standing_water_mm=round(r["ponded_mm"], 3),
                crop_waterlogging_days=r["waterlogging_days"],
                drainage_enabled=i in self._ponding,
            )
            for i in indices
            for r in (self.state["ridges"][i],)
        ]

    def drain_surface(
        self, indices: Iterable[int], max_depth_mm: float
    ) -> dict[int, float]:
        """Drain only standing water, returning the physically removed depth per ridge."""
        ids = tuple(indices)
        if any(i not in self._ponding for i in ids):
            raise OperationRejected("RIDGE_DRAINAGE_UNAVAILABLE")
        actual = {
            i: min(max_depth_mm, self.state["ridges"][i]["ponded_mm"]) for i in ids
        }
        if not any(actual.values()):
            raise OperationRejected("NO_STANDING_WATER")
        for i, depth_mm in actual.items():
            row = self.state["ridges"][i]
            row["ponded_mm"] -= depth_mm
            row["managed_surface_drain_mm"] += depth_mm
        return actual

    def managed_surface_drains(self) -> tuple[float, ...]:
        return tuple(r["managed_surface_drain_mm"] for r in self.state["ridges"])

    def survey(
        self, indices: Iterable[int], thermal: bool = False
    ) -> list[SurveyObservation]:
        """计算合成NDVI或冠层温差；非真实仪器模拟，不可用冠层温差用None表达。"""
        rows: list[SurveyObservation] = []
        for i in indices:
            r = self.state["ridges"][i]
            lai = sum(crop["lai"] * fraction for crop, fraction in self._cohorts(i))
            if thermal and (not r["planted"] or r["harvested"] or lai == 0):
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
                    dict(
                        ridge_id=i,
                        ndvi=round(0.15 + 0.7 * (1 - exp(-0.6 * lai)), 3),
                    )
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
            if self.state["stands"]:
                self.state["stands"][i]["skipped_seeds"] = self._planter_skips.get(i, 0)

    def project_harvest(self, indices: Iterable[int]) -> HarvestProjection:
        """Gross mature grain available for removal, without equipment losses."""
        dry = wet = 0.0
        for i in indices:
            if self._ridge_stage(i) != "R8":
                raise OperationRejected("CROP_NOT_HARVESTABLE")
            dry_per_area, wet_per_area = self._ridge_grain(i)
            dry += dry_per_area * self.ridge_area / 1000
            wet += wet_per_area * self.ridge_area / 1000
        return dict(gross_dry_kg=dry, gross_water_kg=wet - dry)

    def harvest(self, indices: Iterable[int]) -> HarvestProjection:
        """Remove gross grain once; physical machinery owns recovery and loss."""
        indices = tuple(indices)
        batch = self.project_harvest(indices)
        for i in indices:
            r = self.state["ridges"][i]
            r["grain_removed_g_m2"] += self._ridge_grain(i)[0]
            for crop, _ in self._cohorts(i):
                crop.update({"grain_dry_g_m2": 0.0, "harvested": True, "lai": 0.0})
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
        day = date.fromisoformat(weather.day)
        air = (weather.tmin_c + weather.tmax_c) / 2
        soil_weather = WeatherInput(
            day,
            air,
            weather.tmin_c,
            weather.tmax_c,
            weather.rain_mm,
            weather.radiation_mj_m2,
            weather.wind_ms,
            weather.et0_mm,
        )
        growth_weather = GrowthWeatherInput(day, weather.radiation_mj_m2, air)
        for i, r in enumerate(self.state["ridges"]):
            soil = self._soil[i]
            before = r["storage_mm"] + r["top_storage_mm"] + r["ponded_mm"]
            gross = r["pending_mm"]
            loss = gross * (1 - soil.params.irrigation_efficiency)
            soil_state = RidgeSoilState(
                i,
                r["top_storage_mm"] / self.top_depth_mm,
                r["storage_mm"] / depth,
                r["top_temp_c"],
                r["root_temp_c"],
            )
            cover = sum(
                self._canopy._canopy_cover_from_lai(crop["lai"]) * fraction
                for crop, fraction in self._cohorts(i)
            )
            soil._update_ridge_day(soil_state, soil_weather, gross, cover)
            runoff = (
                weather.rain_mm * (1 - soil.params.rainfall_capture_efficiency)
                + soil_state.cumulative_runoff_mm
            )
            passive = 0.0
            if i in self._ponding:
                capacity_mm, passive_mm_day = self._ponding[i]
                passive = min(r["ponded_mm"], passive_mm_day)
                r["ponded_mm"] -= passive
                captured = min(runoff, max(0.0, capacity_mm - r["ponded_mm"]))
                r["ponded_mm"] += captured
                runoff -= captured
            r.update(
                {
                    "storage_mm": soil_state.root_vwc * depth,
                    "top_storage_mm": soil_state.top_vwc * self.top_depth_mm,
                    "top_temp_c": soil_state.top_temp_c,
                    "root_temp_c": soil_state.root_temp_c,
                    "pending_mm": 0.0,
                }
            )
            flux = dict(
                rain_mm=weather.rain_mm,
                irrigation_mm=gross,
                application_loss_mm=loss,
                runoff_mm=runoff,
                drainage_mm=soil_state.cumulative_drainage_mm + passive,
                evaporation_mm=soil_state.cumulative_evap_mm,
                transpiration_mm=soil_state.cumulative_transpiration_mm,
            )
            totals["balance_error_mm"] += (
                before
                + weather.rain_mm
                + gross
                - r["storage_mm"]
                - r["top_storage_mm"]
                - r["ponded_mm"]
                - sum(
                    flux[k]
                    for k in RIDGE_COUNTERS
                    if k not in ("rain_mm", "irrigation_mm")
                )
            ) / c.ridge_count
            for key, value in flux.items():
                totals[key] += value / c.ridge_count
                r[RIDGE_COUNTERS[key]] += value
            stage = CANOPY_STAGES.get(self.stage(r), GrowthStage.R8)
            biotic = self._biotic_day(i, soil_weather, soil_state, stage, cover)
            if r["planted"] and not r["harvested"]:
                stressed = False
                for crop, _ in self._cohorts(i):
                    stressed |= self._advance_crop(
                        i,
                        crop,
                        growth_weather,
                        soil_state.root_vwc,
                        weather.rain_mm,
                        r["ponded_mm"],
                        biotic,
                    )
                r["waterlogging_days"] += int(stressed)
        self.state["next_day"] = (
            date.fromisoformat(weather.day) + timedelta(days=1)
        ).isoformat()
        return totals

    def _advance_crop(
        self,
        ridge: int,
        row: NativeCropState,
        weather: GrowthWeatherInput,
        root_vwc: float,
        rain_mm: float,
        ponded_mm: float,
        biotic: dict[str, float],
    ) -> bool:
        """Drive the original canopy kernel with 2.0 stage and density inputs."""
        row["gdd"] += max(0.0, weather.air_temp_mean_c - self.crop.base_temperature_c)
        stage = CANOPY_STAGES[self.stage(row)]
        if stage == GrowthStage.PLANTED_PRE_EMERGENCE:
            return False
        params = self._canopy.params
        emerged_today = row["dry_matter_g_m2"] == 0
        state = CanopyBiomassState(
            ridge_id=ridge,
            initialized=True,
            seed_type=SeedType.HEINONG58,
            lai=params.initial_lai_at_emergence if emerged_today else row["lai"],
            aboveground_biomass_g_m2=(
                params.initial_biomass_g_m2_at_emergence
                if emerged_today
                else row["dry_matter_g_m2"]
            ),
        )
        # The source orchestrator uses a distinct post-ET stress ramp for crop growth.
        water_stress = min(1.0, max(0.0, (root_vwc - 0.12) / (0.18 - 0.12)))
        stressed = ponded_mm >= 5.0 and stage not in (GrowthStage.R7, GrowthStage.R8)
        if stressed:
            water_stress *= 1.0 - 0.60 * min(1.0, ponded_mm / 20.0)
        self._canopy._update_ridge_day(
            state,
            weather,
            PhenologyInput(stage, min(1.0, row["gdd"] / self.config.maturity_gdd)),
            GrowthSoilInput(water_stress=water_stress, root_vwc=root_vwc),
            ManagementStressInput(planting_density_plants_m2=row["density"], **biotic),
        )
        row["lai"] = state.lai
        row["dry_matter_g_m2"] = state.aboveground_biomass_g_m2
        if stage == GrowthStage.R8:
            row["grain_dry_g_m2"] = state.yield_potential_g_m2
            row["moisture"] = min(
                0.35, max(0.10, row["moisture"] - 0.018 + min(0.04, rain_mm * 0.003))
            )

        return stressed

    def audit(self) -> JsonObject:
        """返回可信审计投影，用于守恒和恢复检查，不交给普通Agent。"""
        return {
            **copy.deepcopy(self.state),
            "root_vwc": sum(r["storage_mm"] for r in self.state["ridges"])
            / (self.config.ridge_count * self.farm.root_depth_m * 1000),
            "model_status": "uncalibrated FAIRY legacy soil and canopy port",
        }

    def export_state(self) -> JsonObject:
        """导出可信恢复快照；此数据不是LLM公开观测。"""
        return dict(
            schema=5,
            capabilities=asdict(self.capabilities),
            state=copy.deepcopy(self.state),
        )

    def load_state(self, payload: JsonObject) -> None:
        """验证版本与守恒后恢复内部状态；调用方必须提供匹配的场景和新实例。"""
        import json
        from fairy.acps.ontology.farm.spec import finite

        if payload.get("schema") != 5 or json.dumps(
            payload.get("capabilities"), sort_keys=True
        ) != json.dumps(asdict(self.capabilities), sort_keys=True):
            raise ValueError("Incompatible season backend")
        state = copy.deepcopy(payload["state"])
        date.fromisoformat(state["next_day"])
        if (
            set(state) != set(self.state)
            or len(state["ridges"]) != self.config.ridge_count
        ):
            raise ValueError("Incomplete spatial state")
        if (
            type(state["protection_used"]) is not int
            or not 0 <= state["protection_used"] <= self.config.plant_protection_units
            or len(state["biotic"]) != len(self.state["biotic"])
        ):
            raise ValueError("Invalid protection ledger")
        queued = 0
        for i, entry in enumerate(state["biotic"]):
            if set(entry) != {"pressure", "pending"}:
                raise ValueError("Invalid biotic ridge state")
            pressure = entry["pressure"]
            expected = self.state["biotic"][i]["pressure"]
            if set(pressure) != set(expected) or pressure["ridge_id"] != i:
                raise ValueError("Invalid biotic pressure keys or ridge")
            for key, value in pressure.items():
                if key == "tags":
                    if not isinstance(value, list) or any(
                        not isinstance(tag, str) for tag in value
                    ):
                        raise ValueError("Invalid modeled crop scouting tags")
                elif key != "ridge_id":
                    finite(
                        value,
                        key,
                        0,
                        (
                            1
                            if key
                            in ("insect_pressure", "disease_pressure", "weed_pressure")
                            else None
                        ),
                    )
                    if key.endswith("days_left") and type(value) is not int:
                        raise ValueError("Invalid residual duration")
            if entry["pending"] is not None:
                if (
                    entry["pending"] not in TREATMENTS
                    or not state["ridges"][i]["planted"]
                ):
                    raise ValueError("Invalid pending crop protection")
                queued += 1
        if queued > state["protection_used"]:
            raise ValueError("Pending treatments exceed protection inventory used")
        if len(state["stands"]) != len(self.state["stands"]):
            raise ValueError("Invalid stand ledger length")
        cohort_fields = set(NativeCropState.__annotations__)
        for i, row in enumerate(state["stands"]):
            ridge = state["ridges"][i]
            skipped = self._planter_skips.get(i, 0) if ridge["planted"] else 0
            if (
                set(row) != {"skipped_seeds", "replant"}
                or type(row["skipped_seeds"]) is not int
                or row["skipped_seeds"] != skipped
            ):
                raise ValueError("Stand ledger disagrees with declared planting gaps")
            cohort = row["replant"]
            if cohort is None:
                continue
            if (
                not skipped
                or set(cohort) != cohort_fields
                or cohort["planted"] is not True
                or cohort["harvested"] is not ridge["harvested"]
            ):
                raise ValueError("Invalid replanted cohort identity")
            for field in cohort_fields - {"planted", "harvested"}:
                finite(cohort[field], field, 0)
            if (
                cohort["gdd"] > ridge["gdd"]
                or cohort["density"] != ridge["density"]
                or cohort["moisture"] >= 1
                or cohort["grain_dry_g_m2"] > cohort["dry_matter_g_m2"] + 1e-8
                or (cohort["harvested"] and cohort["grain_dry_g_m2"])
            ):
                raise ValueError("Invalid replanted crop development or grain state")
        for i, r in enumerate(state["ridges"]):
            if set(r) != set(self.state["ridges"][0]):
                raise ValueError("Incomplete ridge state")
            for k, v in r.items():
                if k in ("planted", "harvested"):
                    if type(v) is not bool:
                        raise ValueError("Invalid crop flags")
                elif k in ("top_temp_c", "root_temp_c"):
                    finite(v, k)
                else:
                    finite(v, k, 0)
            capacity_mm = self._ponding.get(i, (0.0, 0.0))[0]
            if (
                r["ponded_mm"] > capacity_mm + 1e-9
                or (
                    not capacity_mm
                    and (r["managed_surface_drain_mm"] or r["waterlogging_days"])
                )
                or type(r["waterlogging_days"]) is not int
                or (not r["planted"] and r["waterlogging_days"])
            ):
                raise ValueError("Invalid surface water or crop exposure state")
            if r["harvested"] and not r["planted"]:
                raise ValueError("Invalid crop history")
            for storage, depth in (
                (r["storage_mm"], self.farm.root_depth_m * 1000),
                (r["top_storage_mm"], self.top_depth_mm),
            ):
                if (
                    not self.farm.wilting_point_vwc * depth - 1e-8
                    <= storage
                    <= (self.farm.saturation_vwc * depth + 1e-8)
                ):
                    raise ValueError("Invalid soil storage")
            from math import isclose

            expected = (
                self.farm.initial_vwc
                * (self.farm.root_depth_m * 1000 + self.top_depth_mm)
                + r["rain_mm"]
                + r["irrigation_mm"]
                - r["application_loss_mm"]
                - r["runoff_mm"]
                - r["drainage_mm"]
                - r["evap_mm"]
                - r["transp_mm"]
                - r["managed_surface_drain_mm"]
            )
            if not isclose(
                expected,
                r["storage_mm"] + r["top_storage_mm"] + r["ponded_mm"],
                abs_tol=1e-7,
            ):
                raise ValueError("Soil water conservation mismatch")
            if r["harvested"] and r["grain_dry_g_m2"] != 0:
                raise ValueError("Harvested grain still in field")
            stand = state["stands"][i] if state["stands"] else None
            missed = stand["skipped_seeds"] / self._seed_sites if stand else 0.0
            replanted = stand["replant"] if stand else None
            biomass = r["dry_matter_g_m2"] * (1.0 - missed)
            grain = r["grain_dry_g_m2"] * (1.0 - missed)
            if replanted is not None:
                biomass += replanted["dry_matter_g_m2"] * missed
                grain += replanted["grain_dry_g_m2"] * missed
            if grain + r["grain_removed_g_m2"] > biomass + 1e-8:
                raise ValueError("Grain exceeds assimilated dry matter")
            if r["moisture"] >= 1:
                raise ValueError("Invalid moisture")
        self.state = cast(NativeSeasonState, state)
