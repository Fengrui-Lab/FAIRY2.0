"""Spatial, reduced soybean season. Explicitly uncalibrated; no legacy imports.

Water is conserved per ridge. Radiation-driven growth supplies dry grain;
no preset yield, maximum-ever yield proxy, or yield-based success score.
"""
from dataclasses import asdict
from datetime import date, timedelta
from math import exp
import copy
from fairy.acps.backends.biology.contracts import BackendCapabilities, SoilObservation
from fairy.acps.ontology.contracts import OperationRejected


class NativeSeasonBackend:
    capabilities = BackendCapabilities("native", "ridge_water_rue_soybean", "2",
        actions=("irrigate", "full_season", "plant", "harvest"))

    def __init__(self, farm, crop, start, season):
        self.farm, self.crop, self.config = farm, crop, season
        self.state = {"next_day": start, "ridges": [dict(
            planted=False, harvested=False, gdd=0.0, lai=0.0,
            dry_matter_g_m2=0.0, grain_dry_g_m2=0.0, grain_removed_g_m2=0.0,
            storage_mm=farm.initial_vwc*farm.root_depth_m*1000,
            pending_mm=0.0, moisture=0.30, density=0.0,
            rain_mm=0.0, irrigation_mm=0.0, runoff_mm=0.0, drainage_mm=0.0,
            evap_mm=0.0, transp_mm=0.0, application_loss_mm=0.0,
        ) for _ in range(season.ridge_count)]}

    @property
    def ridge_area(self):
        return self.farm.area_m2 / self.config.ridge_count

    def hydraulic_parameters(self, ridge):
        for first,last,capacity,capture,efficiency in self.config.hydraulic_zones:
            if first<=ridge<=last: return capacity,capture,efficiency
        return self.farm.field_capacity_vwc,1.0,self.farm.irrigation_efficiency

    def indices(self, args):
        start, end = args["start_ridge"], args["end_ridge"]
        if not 0 <= start <= end < self.config.ridge_count:
            raise OperationRejected("INVALID_RIDGE_RANGE")
        return range(start, end+1)

    def stage(self, r):
        if r["harvested"]:
            return "harvested"
        if not r["planted"]:
            return "unplanted"
        g = r["gdd"]
        if g < self.config.emergence_gdd:
            return "pre_emergence"
        for fraction, stage in ((.35, "vegetative"), (.55, "R1"), (.70, "R3"),
                                (.83, "R5"), (.94, "R6"), (1, "R7")):
            if g < self.config.maturity_gdd*fraction:
                return stage
        return "R8"

    def observe(self):
        return SoilObservation(self.farm.plot_id, round(self.audit()["root_vwc"], 3))

    def field_observation(self, indices):
        """Synthetic visual/moisture inspection, not hidden biomass or potential yield."""
        return [dict(ridge_id=i, stage=self.stage(r), grain_moisture=round(r["moisture"], 3)
                     if self.stage(r) in ("R7", "R8") else None)
                for i in indices for r in (self.state["ridges"][i],)]

    def soil_observation(self, indices):
        return [dict(ridge_id=i, root_vwc=round(self.state["ridges"][i]["storage_mm"] /
                    (self.farm.root_depth_m*1000), 3)) for i in indices]

    def survey(self, indices, thermal=False):
        rows=[]
        for i in indices:
            r=self.state["ridges"][i]
            if thermal and (not r["planted"] or r["harvested"] or r["lai"]==0):
                rows.append(dict(ridge_id=i, canopy_air_delta_c=None, available=False))
                continue
            if thermal:
                vwc=r["storage_mm"]/(self.farm.root_depth_m*1000)
                capacity,_,_=self.hydraulic_parameters(i)
                value=round(5*(1-min(1,max(0,(vwc-self.farm.wilting_point_vwc)/(capacity-self.farm.wilting_point_vwc)))),2)
                rows.append(dict(ridge_id=i, canopy_air_delta_c=value))
            else:
                rows.append(dict(ridge_id=i, ndvi=round(.15+.7*(1-exp(-.6*r["lai"])),3)))
        return rows

    def queue_irrigation(self, indices, depth):
        for i in indices:
            self.state["ridges"][i]["pending_mm"] += depth

    def plant(self, indices, density):
        for i in indices:
            self.state["ridges"][i].update(planted=True, density=density)

    def project_harvest(self, indices):
        """Gross mature grain available for removal, without equipment losses."""
        dry=wet=0.0
        for i in indices:
            r=self.state["ridges"][i]
            if self.stage(r) != "R8":
                raise OperationRejected("CROP_NOT_HARVESTABLE")
            available=r["grain_dry_g_m2"]*self.ridge_area/1000
            dry+=available
            wet+=available/(1-r["moisture"])
        return dict(gross_dry_kg=dry, gross_water_kg=wet-dry)

    def harvest(self, indices):
        """Remove gross grain once; physical machinery owns recovery and loss."""
        indices=tuple(indices)
        batch=self.project_harvest(indices)
        for i in indices:
            r=self.state["ridges"][i]
            r["grain_removed_g_m2"]+=r["grain_dry_g_m2"]
            r.update(grain_dry_g_m2=0.0, harvested=True, lai=0.0)
        return batch

    def advance_day(self, weather, irrigation_mm):
        if weather.day != self.state["next_day"]:
            raise ValueError("Non-contiguous daily forcing")
        if irrigation_mm != 0:
            raise ValueError("Season irrigation must be allocated by ridge")
        f,c=self.farm,self.config
        depth=f.root_depth_m*1000
        totals={k:0.0 for k in ("rain_mm", "irrigation_mm", "application_loss_mm", "runoff_mm",
                 "drainage_mm", "evaporation_mm", "transpiration_mm", "balance_error_mm")}
        for i,r in enumerate(self.state["ridges"]):
            before=r["storage_mm"]
            fc,capture,efficiency=self.hydraulic_parameters(i)
            gross=r["pending_mm"]
            loss=gross*(1-efficiency)
            water=before+weather.rain_mm*capture+gross-loss
            runoff=weather.rain_mm*(1-capture)+max(0,water-f.saturation_vwc*depth)
            water=min(water,f.saturation_vwc*depth)
            drainage=max(0,water-fc*depth)
            water-=drainage
            cover=1-exp(-.6*r["lai"])
            stress=min(1,max(0,(water/depth-f.wilting_point_vwc)/(fc-f.wilting_point_vwc)))
            evap=min(weather.et0_mm*(1-cover),max(0,water-f.wilting_point_vwc*depth))
            water-=evap
            transp=min(weather.et0_mm*cover*stress,max(0,water-f.wilting_point_vwc*depth))
            water-=transp
            residual=before+weather.rain_mm+gross-water-loss-runoff-drainage-evap-transp
            flux=dict(rain_mm=weather.rain_mm,irrigation_mm=gross,application_loss_mm=loss,
                      runoff_mm=runoff,drainage_mm=drainage,evaporation_mm=evap,
                      transpiration_mm=transp,balance_error_mm=residual)
            for key,value in flux.items():
                totals[key]+=value/c.ridge_count
                state_key={"evaporation_mm":"evap_mm","transpiration_mm":"transp_mm"}.get(key,key)
                if state_key in r:
                    r[state_key]+=value
            r.update(storage_mm=water,pending_mm=0.0)
            if r["planted"] and not r["harvested"]:
                r["gdd"]+=max(0,(weather.tmin_c+weather.tmax_c)/2-self.crop.base_temperature_c)
                progress=min(1,r["gdd"]/c.maturity_gdd)
                if self.stage(r) == "R8":
                    # Grain dry mass freezes at maturity; weather alters grain water only.
                    r["moisture"]=min(.35,max(.10,r["moisture"]-.018+min(.04,weather.rain_mm*.003)))
                    r["lai"]*=.90
                elif r["gdd"]>=c.emergence_gdd:
                    if r["lai"]==0:
                        r["lai"]=.10
                    intercepted=1-exp(-.6*r["lai"])
                    growth_stress=transp/(weather.et0_mm*cover) if weather.et0_mm*cover>0 else 0.0
                    increment=weather.radiation_mj_m2*.48*intercepted*c.rue_g_mj_par*growth_stress
                    r["dry_matter_g_m2"]+=increment
                    if progress>=.55:
                        r["grain_dry_g_m2"]+=increment*c.grain_partition
                    if progress<.83:
                        r["lai"]=min(c.max_lai, r["lai"]+.059*r["lai"]*(1-r["lai"]/c.max_lai)*stress)
                    else:
                        r["lai"]*=.965
        self.state["next_day"]=(date.fromisoformat(weather.day)+timedelta(days=1)).isoformat()
        return totals

    def audit(self):
        return {**copy.deepcopy(self.state), "root_vwc":sum(r["storage_mm"] for r in self.state["ridges"])/
                (self.config.ridge_count*self.farm.root_depth_m*1000),
                "model_status":"uncalibrated reduced full-season process"}

    def export_state(self):
        return dict(schema=1,capabilities=asdict(self.capabilities),state=copy.deepcopy(self.state))

    def load_state(self,payload):
        import json
        from fairy.acps.ontology.farm.spec import finite
        if payload.get("schema")!=1 or json.dumps(payload.get("capabilities"),sort_keys=True)!=json.dumps(asdict(self.capabilities),sort_keys=True):
            raise ValueError("Incompatible season backend")
        state=copy.deepcopy(payload["state"])
        date.fromisoformat(state["next_day"])
        if set(state)!=set(self.state) or len(state["ridges"])!=self.config.ridge_count:
            raise ValueError("Incomplete spatial state")
        for r in state["ridges"]:
            if set(r)!=set(self.state["ridges"][0]):
                raise ValueError("Incomplete ridge state")
            for k,v in r.items():
                if k in ("planted","harvested"):
                    if type(v) is not bool: raise ValueError("Invalid crop flags")
                else: finite(v,k,0)
            if r["harvested"] and not r["planted"]: raise ValueError("Invalid crop history")
            if r["storage_mm"]>self.farm.saturation_vwc*self.farm.root_depth_m*1000+1e-8:
                raise ValueError("Invalid soil storage")
            from math import isclose
            expected=(self.farm.initial_vwc*self.farm.root_depth_m*1000+r["rain_mm"]+r["irrigation_mm"]-
                      r["application_loss_mm"]-r["runoff_mm"]-r["drainage_mm"]-r["evap_mm"]-r["transp_mm"])
            if not isclose(expected,r["storage_mm"],abs_tol=1e-7): raise ValueError("Soil water conservation mismatch")
            if r["harvested"] and r["grain_dry_g_m2"]!=0: raise ValueError("Harvested grain still in field")
            if r["grain_dry_g_m2"]+r["grain_removed_g_m2"]>r["dry_matter_g_m2"]+1e-8:
                raise ValueError("Grain exceeds assimilated dry matter")
            if r["moisture"]>=1: raise ValueError("Invalid moisture")
        self.state=state
