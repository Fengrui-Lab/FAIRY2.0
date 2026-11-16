"""Allowlisted model input. No runtime instance, snapshot, or audit state."""
from dataclasses import asdict
from fairy.events.records import detached, canonical


def build_context(runtime, history, max_chars=12000, max_turns=8):
    if any(type(value) is not int or value < 1 for value in (max_chars, max_turns)):
        raise ValueError("Context budgets must be positive integers")
    spec = runtime.scenario
    context = dict(task=spec.task, status=runtime.public_status(),
                   deadline=spec.deadline,
                   objects=runtime.public_objects(),
                   tools=runtime.operations.tool_definitions(), history=detached(history[-max_turns:]))
    if spec.task_type == "irrigation":
        context["target_vwc"] = [spec.target_vwc_min, spec.target_vwc_max]
    if spec.season:
        planted,harvested=set(),set()
        stored=0
        for event in runtime.events:
            if event.kind!="operation_result" or not event.payload["ok"]:
                continue
            receipt=event.payload.get("data",{}).get("receipt",{})
            operation=receipt.get("operation")
            if operation=="plant": planted.update(receipt["completed_ridges"])
            elif operation=="harvest": harvested.update(receipt["completed_ridges"])
            elif operation=="store_grain": stored+=1
        targets=set(range(spec.season.ridge_count))
        context["progress"]={"source":"successful public tool receipts",
            "planted_ridges":sorted(planted),"harvested_ridges":sorted(harvested),
            "remaining_to_plant":sorted(targets-planted),"remaining_to_harvest":sorted(targets-harvested),
            "successful_storage_transfers":stored}
        context["constraints"] = dict(ridge_count=spec.season.ridge_count,
            cultivar=spec.season.cultivar, spacing_m=spec.season.seed_spacing_cm/100,
            depth_mm=spec.season.sowing_depth_cm*10, safe_storage_moisture=spec.season.safe_moisture)
    if spec.weather_in_context:
        context["weather"] = [asdict(w) for w in spec.public_weather if w.day >= runtime.now.date().isoformat()]
    while len(canonical(context)) > max_chars and context["history"]:
        context["history"].pop(0)  # remove complete request/result pairs
    if len(canonical(context)) > max_chars:
        raise ValueError("Public task context exceeds character budget")
    return detached(context)
