"""Transactional equipment and dry-grain/water ledger, driven by Runtime time."""
from dataclasses import asdict
import copy
from math import ceil
from fairy.acps.backends.physical.contracts import PhysicalCapabilities
from fairy.acps.ontology.contracts import OperationRejected


class NativePhysicalBackend:
    capabilities=PhysicalCapabilities("native", "serial_equipment_material_ledger", "2",
        ("prepare_seedbed","plant","harvest","unload","dry_grain","store_grain",
         "survey_multispectral","survey_thermal","ground_inspect","charge_equipment"))

    def __init__(self,config,farm):
        self.config,self.farm=config,farm
        self.state=dict(prepared=[False]*config.ridge_count,seeds=config.initial_seed_count,
            seeds_used=0,fuel_l=config.initial_fuel_l,fuel_used_l=0.0,electricity_kwh=0.0,
            battery={"multispectral":100.0,"thermal":100.0,"robot":100.0},
            combine=dict(dry_kg=0.0,water_kg=0.0),trailer=dict(dry_kg=0.0,water_kg=0.0),
            stored_batches=[],harvested_dry_kg=0.0,harvested_water_kg=0.0,
            harvest_loss_dry_kg=0.0,harvest_loss_water_kg=0.0,removed_water_kg=0.0)

    @staticmethod
    def weight(batch):
        return batch["dry_kg"]+batch["water_kg"]

    @classmethod
    def moisture(cls,batch):
        return batch["water_kg"]/cls.weight(batch) if cls.weight(batch)>0 else 0.0

    def inventory(self):
        s=self.state
        return dict(seeds=s["seeds"],fuel_l=round(s["fuel_l"],3),battery=copy.deepcopy(s["battery"]),
            combine={**s["combine"],"moisture":self.moisture(s["combine"])},
            trailer={**s["trailer"],"moisture":self.moisture(s["trailer"])},
            stored_batches=copy.deepcopy(s["stored_batches"]),electricity_kwh=s["electricity_kwh"],
            prepared_ridges=[i for i,v in enumerate(s["prepared"]) if v],
            combine_capacity_kg=self.config.combine_capacity_kg,
            trailer_capacity_kg=self.config.trailer_capacity_kg,storage_capacity_kg=self.config.storage_capacity_kg)

    def plan(self,op,args,biology,weather):
        """Pure preflight. The runtime advances the clock, then rechecks at completion."""
        s,c=self.state,self.config
        ids=list(biology.indices(args)) if "start_ridge" in args else []
        n=len(ids)
        duration,fuel=0.0,0.0
        if op in ("prepare_seedbed","plant","harvest"):
            if weather.rain_mm>10 or any(biology.state["ridges"][i]["storage_mm"]/(self.farm.root_depth_m*1000)>.36 for i in ids):
                raise OperationRejected("FIELD_NOT_TRAFFICABLE")
            duration=n*({"prepare_seedbed":.08,"plant":.06,"harvest":.10}[op])
            fuel=n*({"prepare_seedbed":.6,"plant":.4,"harvest":.9}[op])
            if s["fuel_l"]+1e-9<fuel: raise OperationRejected("INSUFFICIENT_FUEL")
        if op=="prepare_seedbed":
            if any(s["prepared"][i] or biology.state["ridges"][i]["planted"] for i in ids):
                raise OperationRejected("RIDGE_ALREADY_PREPARED_OR_PLANTED")
        elif op=="plant":
            if abs(args["spacing_m"]-c.seed_spacing_cm/100)>1e-9 or abs(args["depth_mm"]-c.sowing_depth_cm*10)>1e-9:
                raise OperationRejected("PLANTING_CONTRACT_MISMATCH")
            if any(biology.state["ridges"][i]["planted"] for i in ids):
                raise OperationRejected("RIDGE_ALREADY_PLANTED")
            if any(not s["prepared"][i] for i in ids):
                raise OperationRejected("SEEDBED_NOT_PREPARED")
            needed=n*ceil(2*self.farm.length_m/args["spacing_m"])
            if needed>s["seeds"]: raise OperationRejected("INSUFFICIENT_SEEDS")
        elif op=="harvest":
            if any(biology.stage(biology.state["ridges"][i])!="R8" for i in ids):
                raise OperationRejected("CROP_NOT_HARVESTABLE")
            gross=biology.project_harvest(ids)
            incoming=(gross["gross_dry_kg"]+gross["gross_water_kg"])*c.harvest_recovery_fraction
            if incoming<=0: raise OperationRejected("NO_RECOVERABLE_GRAIN")
            if self.weight(s["combine"])+incoming>c.combine_capacity_kg+1e-9:
                raise OperationRejected("COMBINE_CAPACITY_EXCEEDED")
        elif op=="unload":
            if self.weight(s["combine"])<=0: raise OperationRejected("COMBINE_EMPTY")
            if self.weight(s["combine"])+self.weight(s["trailer"])>c.trailer_capacity_kg+1e-9:
                raise OperationRejected("TRAILER_CAPACITY_EXCEEDED")
            duration=.25
        elif op=="dry_grain":
            if self.weight(s["trailer"])<=0: raise OperationRejected("TRAILER_EMPTY")
            if self.moisture(s["trailer"])<=args["target_moisture"]:
                raise OperationRejected("DRYING_NOT_REQUIRED")
            duration=max(.25,(s["trailer"]["water_kg"]-s["trailer"]["dry_kg"]*args["target_moisture"]/(1-args["target_moisture"]))/100)
        elif op=="store_grain":
            if self.weight(s["trailer"])<=0: raise OperationRejected("TRAILER_EMPTY")
            if self.moisture(s["trailer"])>c.safe_moisture+1e-9: raise OperationRejected("UNSAFE_STORAGE_MOISTURE")
            if sum(self.weight(b) for b in s["stored_batches"])+self.weight(s["trailer"])>c.storage_capacity_kg:
                raise OperationRejected("STORAGE_CAPACITY_EXCEEDED")
            duration=.25
        elif op in ("survey_multispectral","survey_thermal","ground_inspect"):
            key={"survey_multispectral":"multispectral","survey_thermal":"thermal","ground_inspect":"robot"}[op]
            cost=n*(1.0 if key!="robot" else 1.2)
            if s["battery"][key]+1e-9<cost: raise OperationRejected("INSUFFICIENT_BATTERY")
            if key!="robot" and (weather.rain_mm>10 or weather.wind_ms>8):
                raise OperationRejected("UNSAFE_FLIGHT_WEATHER")
            duration=n*(.01 if key!="robot" else .04)
        elif op=="charge_equipment":
            if args["equipment"] not in s["battery"]: raise OperationRejected("UNKNOWN_EQUIPMENT")
            if s["battery"][args["equipment"]]>=100: raise OperationRejected("BATTERY_FULL")
            duration=.75
        else:
            raise OperationRejected("UNSUPPORTED_PHYSICAL_OPERATION")
        return dict(duration_hours=duration,fuel_l=fuel)

    def execute(self,op,args,biology,weather):
        plan=self.plan(op,args,biology,weather)
        s,c=self.state,self.config
        ids=list(biology.indices(args)) if "start_ridge" in args else []
        # Preflight tolerates floating-point roundoff at an exhausted budget.
        # Do not persist a tiny negative quantity that restore must then reject.
        s["fuel_l"]=max(0.0,s["fuel_l"]-plan["fuel_l"])
        s["fuel_used_l"]=c.initial_fuel_l-s["fuel_l"]
        result={"operation":op,**plan}
        if op=="prepare_seedbed":
            for i in ids: s["prepared"][i]=True
        elif op=="plant":
            used=len(ids)*ceil(2*self.farm.length_m/args["spacing_m"])
            s["seeds"]-=used
            s["seeds_used"]+=used
            biology.plant(ids,2/(self.farm.width_m/c.ridge_count)/args["spacing_m"])
            result["seeds_used"]=used
        elif op=="harvest":
            gross=biology.harvest(ids)
            batch={key:gross["gross_"+key]*c.harvest_recovery_fraction for key in ("dry_kg","water_kg")}
            for key in ("dry_kg","water_kg"): s["combine"][key]+=batch[key]
            s["harvested_dry_kg"]+=batch["dry_kg"]
            s["harvested_water_kg"]+=batch["water_kg"]
            s["harvest_loss_dry_kg"]+=gross["gross_dry_kg"]-batch["dry_kg"]
            s["harvest_loss_water_kg"]+=gross["gross_water_kg"]-batch["water_kg"]
            result.update({k:batch[k] for k in ("dry_kg","water_kg")})
        elif op=="unload":
            for key in ("dry_kg","water_kg"):
                s["trailer"][key]+=s["combine"][key]
                s["combine"][key]=0.0
        elif op=="dry_grain":
            final=s["trailer"]["dry_kg"]*args["target_moisture"]/(1-args["target_moisture"])
            removed=s["trailer"]["water_kg"]-final
            s["removed_water_kg"]+=removed
            s["electricity_kwh"]+=removed*.8
            s["trailer"]["water_kg"]=final
            result["removed_water_kg"]=removed
        elif op=="store_grain":
            s["stored_batches"].append({**s["trailer"],"moisture":self.moisture(s["trailer"])})
            s["trailer"]={"dry_kg":0.0,"water_kg":0.0}
        elif op in ("survey_multispectral","survey_thermal","ground_inspect"):
            key={"survey_multispectral":"multispectral","survey_thermal":"thermal","ground_inspect":"robot"}[op]
            s["battery"][key]=max(0.0,s["battery"][key]-len(ids)*(1.0 if key!="robot" else 1.2))
            result["observations"]=(biology.field_observation(ids) if key=="robot" else biology.survey(ids,key=="thermal"))
            if key=="robot": result["soil"]=biology.soil_observation(ids)
            result["sensor_semantics"]="deterministic synthetic observation; not calibrated instrument output"
        elif op=="charge_equipment":
            key=args["equipment"]
            s["electricity_kwh"]+=(100-s["battery"][key])*.01
            s["battery"][key]=100.0
        if ids: result["completed_ridges"]=ids
        return result

    def export_state(self):
        return dict(schema=1,capabilities=asdict(self.capabilities),state=copy.deepcopy(self.state))

    def load_state(self,payload):
        import json
        if payload.get("schema")!=1 or json.dumps(payload.get("capabilities"),sort_keys=True)!=json.dumps(asdict(self.capabilities),sort_keys=True):
            raise ValueError("Incompatible physical backend")
        if set(payload["state"])!=set(self.state): raise ValueError("Incomplete physical state")
        from math import isclose
        from fairy.acps.ontology.farm.spec import finite
        candidate=copy.deepcopy(payload["state"])
        c=self.config
        for k,v in candidate.items():
            if k not in ("prepared","battery","combine","trailer","stored_batches"):
                finite(v,k,0)
        if any(type(candidate[k]) is not int for k in ("seeds","seeds_used")):
            raise ValueError("Invalid seed count")
        if candidate["seeds"]+candidate["seeds_used"]!=c.initial_seed_count:
            raise ValueError("Seed conservation mismatch")
        if not isclose(candidate["fuel_l"]+candidate["fuel_used_l"],c.initial_fuel_l,abs_tol=1e-8):
            raise ValueError("Fuel conservation mismatch")
        if len(candidate["prepared"])!=c.ridge_count or any(type(v) is not bool for v in candidate["prepared"]):
            raise ValueError("Invalid prepared coverage")
        if set(candidate["battery"])!={"multispectral","thermal","robot"}:
            raise ValueError("Invalid equipment identities")
        for v in candidate["battery"].values(): finite(v,"battery",0,100)
        for name,cap in (("combine",c.combine_capacity_kg),("trailer",c.trailer_capacity_kg)):
            batch=candidate[name]
            if set(batch)!={"dry_kg","water_kg"}: raise ValueError("Invalid grain container")
            for v in batch.values(): finite(v,"grain mass",0)
            finite(self.weight(batch),"container mass",0,cap+1e-8)
        for batch in candidate["stored_batches"]:
            if set(batch)!={"dry_kg","water_kg","moisture"}: raise ValueError("Invalid stored batch")
            for v in batch.values(): finite(v,"stored batch",0)
            if not isclose(self.moisture(batch),batch["moisture"],abs_tol=1e-9) or batch["moisture"]>c.safe_moisture+1e-9:
                raise ValueError("Unsafe or inconsistent stored grain")
        if sum(self.weight(b) for b in candidate["stored_batches"])>c.storage_capacity_kg+1e-8:
            raise ValueError("Invalid warehouse capacity")
        all_batches=[candidate["combine"],candidate["trailer"],*candidate["stored_batches"]]
        for key,total in (("dry_kg",candidate["harvested_dry_kg"]),
                          ("water_kg",candidate["harvested_water_kg"]-candidate["removed_water_kg"])):
            if not isclose(sum(b[key] for b in all_batches),total,abs_tol=1e-8):
                raise ValueError("Grain material conservation mismatch")
        for component in ("dry_kg","water_kg"):
            recovered=candidate["harvested_"+component]
            loss=candidate["harvest_loss_"+component]
            if not isclose(recovered,(recovered+loss)*c.harvest_recovery_fraction,abs_tol=1e-8):
                raise ValueError("Harvest recovery disagrees with machinery parameters")
        self.state=candidate

    def audit(self):
        s=self.state
        dry=sum(s[k]["dry_kg"] for k in ("combine","trailer"))+sum(b["dry_kg"] for b in s["stored_batches"])
        water=sum(s[k]["water_kg"] for k in ("combine","trailer"))+sum(b["water_kg"] for b in s["stored_batches"])
        return {**copy.deepcopy(s),"dry_mass_balance_error_kg":s["harvested_dry_kg"]-dry,
                "water_mass_balance_error_kg":s["harvested_water_kg"]-water-s["removed_water_kg"]}
