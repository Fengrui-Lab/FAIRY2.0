"""Reduced single-bucket water balance; no legacy engine objects are reused."""
from __future__ import annotations
from dataclasses import asdict
from datetime import date, timedelta
import json
from math import isclose
from fairy.acps.backends.biology.contracts import BackendCapabilities, SoilObservation
from fairy.acps.ontology.farm.spec import finite


class NativeBackend:
    capabilities = BackendCapabilities("native", "single_bucket_gdd", "1")

    def __init__(self, farm, crop, sowing_date):
        self.farm, self.crop = farm, crop
        self.sowing_date = sowing_date
        self.state = {
            "next_day": sowing_date, "storage_mm": farm.initial_vwc * farm.root_depth_m * 1000,
            "gdd": 0.0, "rain_mm": 0.0, "irrigation_mm": 0.0,
            "application_loss_mm": 0.0, "runoff_mm": 0.0, "drainage_mm": 0.0,
            "evaporation_mm": 0.0, "transpiration_mm": 0.0,
        }

    def observe(self):
        return SoilObservation(self.farm.plot_id, round(self.audit()["root_vwc"], 3))

    def advance_day(self, weather, irrigation_mm):
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
        evaporation = min(demand * (1 - c.canopy_cover), max(0.0, water - f.wilting_point_vwc * depth))
        water -= evaporation
        stress = min(1.0, max(0.0, (water / depth - f.wilting_point_vwc) /
                                  (f.field_capacity_vwc - f.wilting_point_vwc)))
        transpiration = min(demand * c.canopy_cover * stress,
                            max(0.0, water - f.wilting_point_vwc * depth))
        water -= transpiration
        loss = irrigation_mm - effective
        residual = before + weather.rain_mm + irrigation_mm - water - runoff - drainage - evaporation - transpiration - loss
        flux = dict(rain_mm=weather.rain_mm, irrigation_mm=irrigation_mm,
                    application_loss_mm=loss, runoff_mm=runoff, drainage_mm=drainage,
                    evaporation_mm=evaporation, transpiration_mm=transpiration)
        for key, value in flux.items():
            s[key] += value
        s["storage_mm"] = water
        s["gdd"] += max(0.0, (weather.tmin_c + weather.tmax_c) / 2 - c.base_temperature_c)
        s["next_day"] = (date.fromisoformat(weather.day) + timedelta(days=1)).isoformat()
        return {**flux, "balance_error_mm": residual}

    def audit(self):
        s, c = self.state, self.crop
        gdd = s["gdd"]
        return {**s, "root_vwc": s["storage_mm"] / (self.farm.root_depth_m * 1000),
                "root_depth_m": self.farm.root_depth_m,
                "stage": "pre_emergence" if gdd < c.emergence_gdd else "vegetative_or_later",
                "thermal_progress": min(1.0, gdd / c.maturity_gdd),
                "progress_semantics": "GDD fraction, not WOFOST DVS"}

    def export_state(self):
        return {"schema": 1, "capabilities": asdict(self.capabilities), "state": dict(self.state)}

    def load_state(self, payload):
        if (payload.get("schema") != 1 or
                json.dumps(payload.get("capabilities"), sort_keys=True) != json.dumps(asdict(self.capabilities), sort_keys=True)):
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
        upper = (self.farm.initial_vwc if next_day == date.fromisoformat(self.sowing_date)
                 else self.farm.field_capacity_vwc) * depth
        if not lower - 1e-8 <= state["storage_mm"] <= upper + 1e-8:
            raise ValueError("Native storage outside modeled bounds")
        expected = (initial + state["rain_mm"] + state["irrigation_mm"]
                    - sum(state[k] for k in ("application_loss_mm", "runoff_mm", "drainage_mm",
                                             "evaporation_mm", "transpiration_mm")))
        if not isclose(state["storage_mm"], expected, rel_tol=1e-10, abs_tol=1e-8):
            raise ValueError("Native water balance mismatch")
        if not isclose(state["application_loss_mm"], state["irrigation_mm"] *
                       (1 - self.farm.irrigation_efficiency), rel_tol=1e-10, abs_tol=1e-8):
            raise ValueError("Native irrigation efficiency mismatch")
        if next_day == date.fromisoformat(self.sowing_date):
            if any(state[k] != 0 for k in state if k not in ("next_day", "storage_mm")):
                raise ValueError("Nonzero Native totals before first day")
        self.state = dict(state)
