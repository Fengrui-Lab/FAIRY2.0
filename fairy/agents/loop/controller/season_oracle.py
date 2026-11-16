"""Privileged reference policy for engineering checks, not an optimum claim."""
from fairy.acps.ontology.contracts import OperationRequest
from fairy.agents.models.decision import Decision


class SeasonOracle:
    metadata={"kind":"oracle","privileged":True,"policy":"season_reference_v1","optimality":"not_proven"}

    def decide(self,runtime):
        b,p=runtime.backend,runtime.physical
        s=p.state
        def request(name,**args): return Decision(OperationRequest(name,args))
        def wait():
            hours=min(48,(runtime.deadline-runtime.now).total_seconds()/3600)
            return request("advance_time",hours=hours) if hours>0 else Decision(finish_reason="deadline")
        def span(name,ids,**args):
            first=ids[0]
            end=first
            while end+1 in ids and end-first<7: end+=1
            return request(name,start_ridge=first,end_ridge=end,**args)
        if not any(e.kind=="operation_result" and e.payload["ok"] for e in runtime.events):
            return request("inspect_inventory")
        unprepared=[i for i,v in enumerate(s["prepared"]) if not v]
        unplanted=[i for i,r in enumerate(b.state["ridges"]) if not r["planted"]]
        weather=runtime.weather[runtime.now.date().isoformat()]
        if unprepared or unplanted:
            if weather.rain_mm>10: return wait()
            ids=unprepared or unplanted
            if any(b.state["ridges"][i]["storage_mm"]/(runtime.scenario.farm.root_depth_m*1000)>.36 for i in ids): return wait()
            return span("prepare_seedbed",ids) if unprepared else span("plant",ids,spacing_m=runtime.scenario.season.seed_spacing_cm/100,depth_mm=runtime.scenario.season.sowing_depth_cm*10)
        if p.weight(s["combine"])>0:
            return request("unload")
        if p.weight(s["trailer"])>0:
            if p.moisture(s["trailer"])>runtime.scenario.season.safe_moisture:
                return request("dry_grain",target_moisture=min(.13,runtime.scenario.season.safe_moisture))
            return request("store_grain")
        unharvested=[i for i,r in enumerate(b.state["ridges"]) if not r["harvested"]]
        if not unharvested:
            if runtime.pending_irrigation_mm: return wait()
            return Decision(finish_reason="oracle_finished")
        mature=[i for i in unharvested if b.stage(b.state["ridges"][i])=="R8"]
        if mature and weather.rain_mm<=10:
            return span("harvest",mature)
        remaining=runtime.scenario.irrigation_quota_mm-runtime.irrigation_used_mm
        dry=[i for i in unharvested if b.stage(b.state["ridges"][i]) in ("R5","R6")
             and b.state["ridges"][i]["storage_mm"]/(runtime.scenario.farm.root_depth_m*1000)<.20]
        if dry and remaining>1e-6 and not runtime.pending_irrigation_mm:
            i=dry[0]
            return request("irrigate_range",start_ridge=i,end_ridge=i,depth_mm=min(15,remaining*64))
        return wait()
