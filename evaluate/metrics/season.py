"""Outcome and conservation gates, without potential-yield or workflow scores."""

def evaluate(runtime,replay_verified=None):
    b=runtime.backend
    p=runtime.physical
    c=runtime.scenario.season
    rows=b.state["ridges"]
    ledger=p.audit()
    batches=ledger["stored_batches"]
    planted=sum(r["planted"] for r in rows)
    harvested=sum(r["harvested"] for r in rows)
    stored=sum(p.weight(item) for item in batches)
    safe=bool(batches) and all(0<=p.moisture(item)<=c.safe_moisture+1e-9 for item in batches)
    empty=all(p.weight(ledger[k])<=1e-9 for k in ("combine","trailer"))
    conserved=abs(ledger["dry_mass_balance_error_kg"])<1e-6 and abs(ledger["water_mass_balance_error_kg"])<1e-6
    water_ok=0<=runtime.irrigation_used_mm<=runtime.scenario.irrigation_quota_mm+1e-9
    success=(planted==harvested==c.ridge_count and stored>0 and safe and empty and conserved
             and water_ok and runtime.pending_irrigation_mm==0 and runtime.now<=runtime.deadline)
    return dict(task_success=success,planted_ridges=planted,harvested_ridges=harvested,
        stored_grain_kg=stored,stored_dry_grain_kg=sum(i["dry_kg"] for i in batches),
        safe_storage=safe,containers_empty=empty,dry_mass_balance_error_kg=ledger["dry_mass_balance_error_kg"],
        water_used_mm=runtime.irrigation_used_mm,water_used_m3=runtime.scenario.farm.volume_m3(runtime.irrigation_used_mm),
        water_mass_balance_error_kg=ledger["water_mass_balance_error_kg"],fuel_used_l=ledger["fuel_used_l"],electricity_kwh=ledger["electricity_kwh"],
        tool_errors=runtime.tool_errors,replay_verified=replay_verified,
        model_status="uncalibrated Native full-season; stored mass is a simulated outcome, not a yield score")
