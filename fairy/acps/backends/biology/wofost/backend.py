"""Offline PCSE WOFOST 8.1 water-limited adapter, isolated from legacy FAIRY."""
from __future__ import annotations
import copy
import hashlib
import json
from dataclasses import asdict
from datetime import date, timedelta
from pathlib import Path
from fairy.acps.backends.biology.contracts import BackendCapabilities, SoilObservation, WeatherDay
from fairy.acps.ontology.farm.spec import finite

DATA = Path(__file__).parent
VARIABLES = ("SM", "RD", "DVS", "TSUM", "TSUME", "LAI", "TAGP", "TWSO",
             "TOTIRR", "RAINT", "EVST", "EVWT", "WTRAT", "LOSST", "PERCT", "WWLOW", "SS", "TSR")


class WofostBackend:
    def __init__(self, farm, crop, sowing_date):
        try:
            import pcse
            import yaml
            from pcse.base import ParameterProvider, WeatherDataContainer, WeatherDataProvider
            from pcse.input import WOFOST72SiteDataProvider
            from pcse.models import Wofost81_WLP_CWB
        except ImportError as exc:
            raise RuntimeError("WOFOST requires the optional dependency: pip install -e '.[wofost]'") from exc
        if pcse.__version__ != "6.0.13":
            raise RuntimeError("WOFOST pilot requires PCSE 6.0.13; no fallback is permitted")
        self.capabilities = BackendCapabilities("wofost", "Wofost81_WLP_CWB", pcse.__version__)
        self.farm, self.crop, self.sowing_date = farm, crop, sowing_date
        raw = (DATA / "soybean.yaml").read_bytes()
        self.provenance = json.loads((DATA / "provenance.json").read_text())
        if hashlib.sha256(raw).hexdigest() != self.provenance["sha256"]:
            raise ValueError("WOFOST crop parameter integrity check failed")
        document = yaml.safe_load(raw)
        variety = document["CropParameters"]["Varieties"][crop.wofost_variety]
        parameters = {key: copy.deepcopy(value[0]) for key, value in variety.items() if key != "Metadata"}
        # No invented completion of missing cultivar coefficients.
        parameters["CO2"] = 420.0
        soil = dict(SMW=farm.wilting_point_vwc, SMFCF=farm.field_capacity_vwc,
                    SM0=farm.saturation_vwc, CRAIRC=0.06, SOPE=5.0, KSUB=5.0,
                    K0=5.0, RDMSOL=farm.root_depth_m * 100)
        site = WOFOST72SiteDataProvider(
            WAV=(farm.initial_vwc - farm.wilting_point_vwc) * farm.root_depth_m * 100,
            SMLIM=farm.initial_vwc, SSI=0.0, SSMAX=0.0, NOTINF=0.0, IFUNRN=0)
        provider = WeatherDataProvider()
        self._weather_container = WeatherDataContainer
        self.provider = provider

        class DeferredRates(Wofost81_WLP_CWB):
            # PCSE normally calculates tomorrow's rates at the end of run(1).
            # Runtime has not supplied tomorrow's actions yet. Defer only rates.
            def calc_rates(self, day, drv):
                pass

            def calculate_current_day(self):
                super().calc_rates(self.day, self.drv)

        start = date.fromisoformat(sowing_date)
        initial_weather = WeatherDay(sowing_date)
        self._store_weather(initial_weather)
        agro = [{start: {"CropCalendar": {
            "crop_name": "soybean", "variety_name": crop.wofost_variety,
            "crop_start_date": start, "crop_start_type": "sowing",
            "crop_end_date": None, "crop_end_type": "maturity", "max_duration": 200,
        }, "TimedEvents": None, "StateEvents": None}}, {start + timedelta(days=366): None}]
        self.model = DeferredRates(ParameterProvider(cropdata=parameters, soildata=soil, sitedata=site),
                                   provider, agro, output_vars=[], summary_vars=[])
        self.journal = []
        self.gross_irrigation_mm = 0.0

    def _store_weather(self, weather):
        f = self.farm
        day = date.fromisoformat(weather.day)
        container = self._weather_container(
            DAY=day, LAT=f.latitude_deg, LON=f.longitude_deg, ELEV=f.elevation_m,
            TMIN=weather.tmin_c, TMAX=weather.tmax_c,
            TEMP=(weather.tmin_c + weather.tmax_c) / 2,
            IRRAD=weather.radiation_mj_m2 * 1e6, RAIN=weather.rain_mm / 10,
            WIND=weather.wind_ms, VAP=weather.vapor_pressure_hpa,
            E0=weather.et0_mm / 10, ES0=weather.et0_mm / 10, ET0=weather.et0_mm / 10)
        self.provider._store_WeatherDataContainer(container, day)

    def observe(self):
        return SoilObservation(self.farm.plot_id, round(float(self.model.get_variable("SM")), 3))

    def advance_day(self, weather, irrigation_mm):
        from pcse import signals
        finite(irrigation_mm, "irrigation_mm", 0)
        model = self.model
        day = date.fromisoformat(weather.day)
        if model.day != day or model.flag_terminate:
            raise ValueError("Non-contiguous forcing or terminated WOFOST model")
        self._store_weather(weather)
        tomorrow = day + timedelta(days=1)
        self._store_weather(WeatherDay(**{**asdict(weather), "day": tomorrow.isoformat()}))
        model.drv = model._get_driving_variables(day)
        if irrigation_mm:
            model._send_signal(signal=signals.irrigate, amount=irrigation_mm / 10,
                               efficiency=self.farm.irrigation_efficiency)
        before = self.audit()
        model.calculate_current_day()
        model.run(days=1)
        self.gross_irrigation_mm += irrigation_mm
        self.journal.append({"weather": asdict(weather), "irrigation_mm": irrigation_mm})
        after = self.audit()
        # Whole soil profile + surface water, not only the moving root zone.
        delta_storage = after["profile_storage_mm"] - before["profile_storage_mm"]
        evap = after["pcse"]["EVST"] - before["pcse"]["EVST"]
        surface_evap = after["pcse"]["EVWT"] - before["pcse"]["EVWT"]
        transp = after["pcse"]["WTRAT"] - before["pcse"]["WTRAT"]
        loss = after["pcse"]["LOSST"] - before["pcse"]["LOSST"]
        runoff = after["pcse"]["TSR"] - before["pcse"]["TSR"]
        effective = irrigation_mm * self.farm.irrigation_efficiency
        residual = weather.rain_mm + effective - delta_storage - 10 * (evap + surface_evap + transp + loss + runoff)
        return dict(rain_mm=weather.rain_mm, irrigation_mm=irrigation_mm,
                    application_loss_mm=irrigation_mm - effective, evaporation_mm=10 * evap,
                    surface_evaporation_mm=10 * surface_evap,
                    transpiration_mm=10 * transp, drainage_mm=10 * loss,
                    runoff_mm=10 * runoff, balance_error_mm=residual)

    def audit(self):
        values = {key: float(self.model.get_variable(key) or 0.0) for key in VARIABLES}
        return {"root_vwc": values["SM"], "root_depth_m": values["RD"] / 100,
                "next_day": self.model.day.isoformat(),
                "profile_storage_mm": (values["WWLOW"] + values["SS"]) * 10,
                "irrigation_mm": self.gross_irrigation_mm, "pcse": values,
                "pcse_units": "SM: m3/m3; RD: cm; water totals/storage: cm; TAGP/TWSO: kg/ha",
                "progress_semantics": "native PCSE DVS and thermal sums; not Native GDD fraction"}

    def export_state(self):
        return {"schema": 1, "capabilities": asdict(self.capabilities),
                "crop_data_sha256": self.provenance["sha256"], "journal": copy.deepcopy(self.journal)}

    def load_state(self, payload):
        if (payload.get("schema") != 1 or
                json.dumps(payload.get("capabilities"), sort_keys=True) != json.dumps(asdict(self.capabilities), sort_keys=True) or
                payload.get("crop_data_sha256") != self.provenance["sha256"]):
            raise ValueError("Incompatible WOFOST state, engine version, or crop data")
        if self.journal:
            raise ValueError("Restore requires a fresh WOFOST instance")
        for entry in payload["journal"]:
            self.advance_day(WeatherDay(**entry["weather"]), entry["irrigation_mm"])
