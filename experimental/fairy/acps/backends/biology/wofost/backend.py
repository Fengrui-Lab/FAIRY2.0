"""Offline PCSE WOFOST 8.1 water-limited adapter, isolated from legacy FAIRY."""

from __future__ import annotations
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from fairy.acps.backends.biology.contracts import WaterFlux
    from fairy.acps.ontology.contracts import JsonObject
    from fairy.acps.ontology.crops.lib.soybean import SoybeanSpec
    from fairy.acps.ontology.farm.spec import FarmSpec
from fairy.acps.backends.biology.contracts import BiologyBackend
from fairy.acps.ontology.contracts import JsonObject
import copy
import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from pathlib import Path
from fairy.acps.backends.biology.contracts import (
    BackendCapabilities,
    SoilObservation,
    WeatherDay,
)
from fairy.acps.ontology.farm.spec import finite

DATA = Path(__file__).parent
VARIABLES = (
    "SM",
    "RD",
    "DVS",
    "TSUM",
    "TSUME",
    "LAI",
    "TAGP",
    "TWSO",
    "TOTIRR",
    "RAINT",
    "EVST",
    "EVWT",
    "WTRAT",
    "LOSST",
    "PERCT",
    "WWLOW",
    "SS",
    "TSR",
)


@dataclass(frozen=True)
class WaterStressDiagnostic:
    """Offline label from PCSE rates before their daily integration clears them."""

    day: str
    rfws: float


class WofostBackend(BiologyBackend):
    """单土柱PCSE6.0.13水分限制适配器。

    外部契约使用mm、m和kg，PCSE内部水量使用cm、籽粒使用kg/ha；
    journal保存确定性恢复输入。SDK/引擎依赖仅在构建本后端时加载。"""

    journal: list[JsonObject]  #: 固定初始化后重建引擎所需的连续逐日输入。
    gross_irrigation_mm: float  #: 已结算毛灌溉；不同于PCSE有效灌溉TOTIRR。
    bare: bool  #: 全季播前裸地模式，保留实际播种前的土壤水分历史。

    def __init__(
        self, farm: FarmSpec, crop: SoybeanSpec, sowing_date: str, *, bare: bool = False
    ) -> None:
        """保存显式依赖与初态；模型服务调用发生在决策阶段而非构造阶段。"""
        try:
            import pcse
            import yaml
            from pcse.base import (
                ParameterProvider,
                WeatherDataContainer,
                WeatherDataProvider,
            )
            from pcse.input import WOFOST72SiteDataProvider
            from pcse.models import Wofost81_WLP_CWB
        except ImportError as exc:
            raise RuntimeError(
                "WOFOST requires the optional dependency: pip install -e '.[wofost]'"
            ) from exc
        if pcse.__version__ != "6.0.13":
            raise RuntimeError(
                "WOFOST pilot requires PCSE 6.0.13; no fallback is permitted"
            )
        self.capabilities = BackendCapabilities(
            "wofost", "Wofost81_WLP_CWB", pcse.__version__
        )
        self.farm, self.crop, self.sowing_date = farm, crop, sowing_date
        self.bare = bare
        raw = (DATA / "soybean.yaml").read_bytes()
        self.provenance = json.loads((DATA / "provenance.json").read_text())
        if hashlib.sha256(raw).hexdigest() != self.provenance["sha256"]:
            raise ValueError("WOFOST crop parameter integrity check failed")
        document = yaml.safe_load(raw)
        variety = document["CropParameters"]["Varieties"][crop.wofost_variety]
        parameters = {
            key: copy.deepcopy(value[0])
            for key, value in variety.items()
            if key != "Metadata"
        }
        # No invented completion of missing cultivar coefficients.
        parameters["CO2"] = 420.0
        soil = dict(
            SMW=farm.wilting_point_vwc,
            SMFCF=farm.field_capacity_vwc,
            SM0=farm.saturation_vwc,
            CRAIRC=0.06,
            SOPE=5.0,
            KSUB=5.0,
            K0=5.0,
            RDMSOL=farm.root_depth_m * 100,
        )
        site = WOFOST72SiteDataProvider(
            WAV=(farm.initial_vwc - farm.wilting_point_vwc) * farm.root_depth_m * 100,
            SMLIM=farm.initial_vwc,
            SSI=0.0,
            SSMAX=0.0,
            NOTINF=0.0,
            IFUNRN=0,
        )
        provider = WeatherDataProvider()
        self._weather_container = WeatherDataContainer
        self.provider = provider

        class DeferredRates(Wofost81_WLP_CWB):
            # PCSE normally calculates tomorrow's rates at the end of run(1).
            # Runtime has not supplied tomorrow's actions yet. Defer only rates.
            def calc_rates(self, day: date, drv: Any) -> None:
                pass

            def calculate_current_day(self) -> None:
                super().calc_rates(self.day, self.drv)

        start = date.fromisoformat(sowing_date)
        initial_weather = WeatherDay(sowing_date)
        self._store_weather(initial_weather)
        agro = [
            {
                start: {
                    "CropCalendar": {
                        "crop_name": "soybean",
                        "variety_name": crop.wofost_variety,
                        "crop_start_date": start,
                        "crop_start_type": "sowing",
                        "crop_end_date": None,
                        "crop_end_type": "maturity",
                        "max_duration": 200,
                    },
                    "TimedEvents": None,
                    "StateEvents": None,
                }
            },
            {start + timedelta(days=366): None},
        ]
        if bare:
            # A later empty campaign keeps the soil simulation alive after crop maturity.
            agro = [
                {
                    start: {
                        "CropCalendar": None,
                        "TimedEvents": None,
                        "StateEvents": None,
                    }
                },
                {start + timedelta(days=366): None},
            ]
        self.model = DeferredRates(
            ParameterProvider(cropdata=parameters, soildata=soil, sitedata=site),
            provider,
            agro,
            output_vars=[],
            summary_vars=["TWSO", "DVS", "LAI"],
        )
        self.journal: list[JsonObject] = []
        self.gross_irrigation_mm = 0.0
        self.last_water_stress: WaterStressDiagnostic | None = None

    def _store_weather(self, weather: WeatherDay) -> None:
        """将公共SI天气单位转换为PCSE约定并写入该日天气容器。"""
        f = self.farm
        day = date.fromisoformat(weather.day)
        container = self._weather_container(
            DAY=day,
            LAT=f.latitude_deg,
            LON=f.longitude_deg,
            ELEV=f.elevation_m,
            TMIN=weather.tmin_c,
            TMAX=weather.tmax_c,
            TEMP=(weather.tmin_c + weather.tmax_c) / 2,
            IRRAD=weather.radiation_mj_m2 * 1e6,
            RAIN=weather.rain_mm / 10,
            WIND=weather.wind_ms,
            VAP=weather.vapor_pressure_hpa,
            E0=weather.et0_mm / 10,
            ES0=weather.et0_mm / 10,
            ET0=weather.et0_mm / 10,
        )
        self.provider._store_WeatherDataContainer(container, day)

    def observe(self) -> SoilObservation:
        """读取公开观测；不推进业务时钟，不结算pending，也不更新生长。"""
        return SoilObservation(
            self.farm.plot_id, round(float(self.model.get_variable("SM")), 3)
        )

    def advance_day(self, weather: WeatherDay, irrigation_mm: float) -> WaterFlux:
        """消费恰好一个连续业务日，结算水量与适用的生长过程，返回mm通量/守恒残差。"""
        from pcse import signals

        finite(irrigation_mm, "irrigation_mm", 0)
        model = self.model
        day = date.fromisoformat(weather.day)
        if model.day != day or model.flag_terminate:
            raise ValueError("Non-contiguous forcing or terminated WOFOST model")
        self._store_weather(weather)
        tomorrow = day + timedelta(days=1)
        self._store_weather(
            WeatherDay(**{**asdict(weather), "day": tomorrow.isoformat()})
        )
        model.drv = model._get_driving_variables(day)
        if irrigation_mm:
            model._send_signal(
                signal=signals.irrigate,
                amount=irrigation_mm / 10,
                efficiency=self.farm.irrigation_efficiency,
            )
        before = self.audit()
        model.calculate_current_day()
        self.last_water_stress = None
        if model.crop is not None and model.crop.get_variable("STAGE") != "emerging":
            # RFTRA combines water and oxygen effects. RFWS is water shortage alone.
            evap = model.crop.evtra.etmodule.rates
            rfws = float(evap.RFWS)
            if evap.TRAMX > 1e-6 and 0.0 <= rfws <= 1.0:
                self.last_water_stress = WaterStressDiagnostic(weather.day, rfws)
        model.run(days=1)
        if self.bare and model.flag_crop_finish:
            # Finish at the boundary that integrated maturity, before exposing
            # harvestability or computing another day's canopy transpiration.
            model._finish_cropsimulation(model.day)
        self.gross_irrigation_mm += irrigation_mm
        self.journal.append(
            {"weather": asdict(weather), "irrigation_mm": irrigation_mm}
        )
        after = self.audit()
        # Whole soil profile + surface water, not only the moving root zone.
        delta_storage = after["profile_storage_mm"] - before["profile_storage_mm"]
        evap = after["pcse"]["EVST"] - before["pcse"]["EVST"]
        surface_evap = after["pcse"]["EVWT"] - before["pcse"]["EVWT"]
        transp = after["pcse"]["WTRAT"] - before["pcse"]["WTRAT"]
        loss = after["pcse"]["LOSST"] - before["pcse"]["LOSST"]
        runoff = after["pcse"]["TSR"] - before["pcse"]["TSR"]
        effective = irrigation_mm * self.farm.irrigation_efficiency
        residual = (
            weather.rain_mm
            + effective
            - delta_storage
            - 10 * (evap + surface_evap + transp + loss + runoff)
        )
        return dict(
            rain_mm=weather.rain_mm,
            irrigation_mm=irrigation_mm,
            application_loss_mm=irrigation_mm - effective,
            evaporation_mm=10 * evap,
            surface_evaporation_mm=10 * surface_evap,
            transpiration_mm=10 * transp,
            drainage_mm=10 * loss,
            runoff_mm=10 * runoff,
            balance_error_mm=residual,
        )

    def audit(self) -> JsonObject:
        """返回可信审计投影，用于守恒和恢复检查，不交给普通Agent。"""
        values = {key: float(self.model.get_variable(key) or 0.0) for key in VARIABLES}
        return {
            "root_vwc": values["SM"],
            "root_depth_m": values["RD"] / 100,
            "next_day": self.model.day.isoformat(),
            "profile_storage_mm": (values["WWLOW"] + values["SS"]) * 10,
            "irrigation_mm": self.gross_irrigation_mm,
            "pcse": values,
            "pcse_units": "SM: m3/m3; RD: cm; water totals/storage: cm; TAGP/TWSO: kg/ha",
            "progress_semantics": "native PCSE DVS and thermal sums; not Native GDD fraction",
        }

    def export_state(self) -> JsonObject:
        """导出可信恢复快照；此数据不是LLM公开观测。"""
        return {
            "schema": 1,
            "capabilities": asdict(self.capabilities),
            "crop_data_sha256": self.provenance["sha256"],
            "journal": copy.deepcopy(self.journal),
        }

    def load_state(self, payload: JsonObject) -> None:
        """验证版本与守恒后恢复内部状态；调用方必须提供匹配的场景和新实例。"""
        if (
            payload.get("schema") != 1
            or json.dumps(payload.get("capabilities"), sort_keys=True)
            != json.dumps(asdict(self.capabilities), sort_keys=True)
            or payload.get("crop_data_sha256") != self.provenance["sha256"]
        ):
            raise ValueError("Incompatible WOFOST state, engine version, or crop data")
        if self.journal:
            raise ValueError("Restore requires a fresh WOFOST instance")
        for entry in payload["journal"]:
            self.advance_day(WeatherDay(**entry["weather"]), entry["irrigation_mm"])
