"""Single-writer local-day simulation runtime. No agent SDK or UI imports."""
from __future__ import annotations
from dataclasses import asdict
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo
from math import ceil, isclose
from fairy import __version__
from fairy.acps.backends.biology.contracts import make_backend, restore_backend
from fairy.acps.ontology.contracts import OperationRequest, OperationResult, OperationRejected
from fairy.events.records import EventRecord, canonical, detached, fingerprint
from fairy.events.registry import load_objects, bind_operations, check_backend, binding_manifest


class Runtime:
    def __init__(self, scenario, backend="native", *, expected_binding_fingerprint=None):
        self.scenario = scenario
        self.backend_name = backend
        self.zone = ZoneInfo(scenario.timezone)
        self.now = datetime.fromisoformat(scenario.start_time).astimezone(self.zone)
        self.deadline = datetime.fromisoformat(scenario.deadline).astimezone(self.zone)
        self.objects = load_objects(scenario)
        self.operations = bind_operations(self, self.objects)
        self.backend = make_backend(backend, scenario.farm, scenario.crop, scenario.sowing_date, scenario.season)
        self.physical = self._make_physical(scenario)
        check_backend(self.operations, self.backend, scenario.crop.crop_id)
        self.binding_fingerprint = fingerprint(binding_manifest(self))
        if expected_binding_fingerprint is not None and self.binding_fingerprint != expected_binding_fingerprint:
            raise ValueError("Operation binding fingerprint mismatch")
        self.weather = {day.day: day for day in scenario.weather}
        prefix = dict(scenario.prefix_irrigation)
        for day in scenario.weather:
            if day.day >= self.now.date().isoformat():
                break
            self.backend.advance_day(day, prefix.get(day.day, 0.0))
        self.pending_irrigation_mm = 0.0
        self.irrigation_used_mm = 0.0
        self.tool_errors = 0
        self.observations = []
        self.first_irrigation_seq = None
        self.last_irrigation_time = None
        self.done = False
        self.events = []
        self._emit("run_started", {"scenario_fingerprint": fingerprint(scenario.to_dict()),
                                  "backend": asdict(self.backend.capabilities),
                                  "binding_fingerprint": self.binding_fingerprint,
                                  "seed": scenario.seed,
                                  "prefix_days": (self.now.date() - datetime.fromisoformat(scenario.sowing_date).date()).days})

    @staticmethod
    def _make_physical(scenario):
        if scenario.season is None:
            return None
        from fairy.acps.backends.physical.native.backend import NativePhysicalBackend
        return NativePhysicalBackend(scenario.season, scenario.farm)

    def _emit(self, kind, payload, cause=None):
        record = EventRecord(len(self.events) + 1, kind, self.now.isoformat(), cause, canonical(payload))
        self.events.append(record)
        return record.seq

    def public_status(self):
        return dict(time=self.now.isoformat(), timezone=self.scenario.timezone,
                    irrigation_remaining_mm=self.scenario.irrigation_quota_mm - self.irrigation_used_mm,
                    pending_irrigation_mm=self.pending_irrigation_mm, done=self.done)

    def invoke(self, request: OperationRequest, actor="agent"):
        if self.done:
            raise ValueError("Run already terminated")
        if not isinstance(actor, str) or not actor:
            raise ValueError("Actor must be a non-empty string")
        # Malformed/non-JSON messages are handled by the model adapter, not the simulator.
        request_payload = detached(asdict(request))
        cause = self._emit("operation_requested", {**request_payload, "actor": actor})
        try:
            binding = self.operations.resolve(request)
        except OperationRejected as exc:
            return self._reject(exc, cause)

        # All accepted handlers run under one rollback boundary. Capture after
        # recording the request so a failed execution remains auditable.
        saved_backend = self.backend.export_state()
        saved_physical = self.physical.export_state() if self.physical else None
        saved_values = {name: detached(getattr(self, name)) for name in (
            "pending_irrigation_mm", "irrigation_used_mm", "observations",
            "first_irrigation_seq", "last_irrigation_time")}
        old_now, old_count = self.now, len(self.events)
        try:
            data = binding.handler(request.arguments, cause)
            binding.definition.validate_result(data)
            result = OperationResult(True, {**detached(data), "status": self.public_status()})
        except OperationRejected as exc:
            self._rollback(saved_backend, saved_values, old_now, old_count, saved_physical)
            return self._reject(exc, cause)
        except Exception:
            self._rollback(saved_backend, saved_values, old_now, old_count, saved_physical)
            self._emit("engine_failed", {"code": "BACKEND_FAILURE"}, cause)
            raise
        self._emit("operation_result", asdict(result), cause)
        return result

    def _reject(self, error, cause):
        self.tool_errors += 1
        result = OperationResult(False, error=str(error))
        self._emit("operation_result", asdict(result), cause)
        return result

    def _rollback(self, backend_state, values, now, count, physical_state=None):
        self.backend = restore_backend(self.backend_name, self.scenario.farm, self.scenario.crop,
                                       self.scenario.sowing_date, backend_state, self.scenario.season)
        if self.physical:
            self.physical.load_state(physical_state)
        self.now = now
        for name, value in values.items():
            setattr(self, name, value)
        del self.events[count:]

    def _observe(self, provider, cause):
        data = provider.observe(self.backend, self.now)
        self.observations.append({**data, "seq": cause})
        return data

    def _irrigate(self, provider, arguments, cause):
        depth = arguments["depth_mm"]
        receipt = provider.prepare(depth, self.scenario.farm,
                                   self.scenario.irrigation_quota_mm - self.irrigation_used_mm)
        if self.scenario.season:
            self.backend.queue_irrigation(range(self.scenario.season.ridge_count), float(depth))
        self.pending_irrigation_mm += float(depth)
        self.irrigation_used_mm += float(depth)
        if self.first_irrigation_seq is None:
            self.first_irrigation_seq = cause
        self.last_irrigation_time = self.now.isoformat()
        return receipt

    def _advance_time(self, arguments, cause):
        target = (self.now.astimezone(timezone.utc) + timedelta(hours=arguments["hours"])).astimezone(self.zone)
        if target <= self.now:
            raise OperationRejected("TIME_STEP_TOO_SMALL")
        if target > self.deadline:
            raise OperationRejected("TIME_BUDGET_EXCEEDED")
        old_now = self.now
        while self.now.date() < target.date():
            day = self.now.date().isoformat()
            midnight = datetime.combine(self.now.date() + timedelta(days=1), time(), self.zone)
            flux = self.backend.advance_day(self.weather[day], 0.0 if self.scenario.season else self.pending_irrigation_mm)
            self.pending_irrigation_mm = 0.0
            self.now = midnight
            self._emit("environment_day_completed", {"day": day, "flux": flux}, cause)
        self.now = target
        self._emit("time_advanced", {"from": old_now.isoformat(), "to": self.now.isoformat()}, cause)
        return {"time": self.now.isoformat()}

    def _season_operation(self, name, args, cause):
        if self.physical is None:
            raise OperationRejected("UNSUPPORTED_SEASON_OPERATION")
        if name in ("inspect_crop", "observe_soil_range"):
            ids=self.backend.indices(args)
            rows=(self.backend.field_observation(ids) if name=="inspect_crop" else self.backend.soil_observation(ids))
            return {"receipt":{"observations":rows,"time":self.now.isoformat()}}
        if name=="inspect_inventory":
            return {"receipt":self.physical.inventory()}
        if name=="irrigate_range":
            ids=list(self.backend.indices(args))
            depth=args["depth_mm"]
            field_mm=depth*len(ids)/self.scenario.season.ridge_count
            if field_mm>self.scenario.irrigation_quota_mm-self.irrigation_used_mm+1e-9:
                raise OperationRejected("WATER_QUOTA_EXCEEDED")
            self.backend.queue_irrigation(ids,depth)
            self.pending_irrigation_mm+=field_mm
            self.irrigation_used_mm+=field_mm
            if self.first_irrigation_seq is None: self.first_irrigation_seq=cause
            self.last_irrigation_time=self.now.isoformat()
            return {"receipt":{"depth_mm":depth,"field_average_mm":field_mm,
                    "volume_m3":self.scenario.farm.volume_m3(field_mm),"settlement":"next_local_midnight"}}
        plan=self.physical.plan(name,args,self.backend,self.weather[self.now.date().isoformat()])
        # A schedule describes proposed work; events describe what actually occurred.
        from fairy.events.operations import ScheduledJob
        job=ScheduledJob(name,self.now.isoformat(),plan["duration_hours"],cause)
        self._emit("job_scheduled",asdict(job),cause)
        self._advance_time({"hours":job.duration_hours},cause)
        receipt=self.physical.execute(name,args,self.backend,self.weather[self.now.date().isoformat()])
        self._emit("job_completed",{"operation":name,"receipt":receipt},cause)
        return {"receipt":receipt}

    def public_objects(self):
        """Only identity, type and target links; no private state or model parameters."""
        return [asdict(spec) for spec in self.scenario.objects]

    def finish(self, reason="completed"):
        if not isinstance(reason, str) or not reason:
            raise ValueError("Termination reason must be a non-empty string")
        if not self.done:
            self.done = True
            self._emit("run_terminated", {"reason": reason})

    def audit_state(self):
        """Privileged API. Never passed into public agent context."""
        result = detached(self.backend.audit())
        if self.physical:
            result["physical"] = self.physical.audit()
        return result

    def export_state(self):
        return detached({
            "schema": 4, "fairy_version": __version__, "scenario": self.scenario.to_dict(),
            "scenario_fingerprint": fingerprint(self.scenario.to_dict()), "backend": self.backend_name,
            "binding_fingerprint": self.binding_fingerprint,
            "backend_state": self.backend.export_state(),
            "physical_state": self.physical.export_state() if self.physical else None, "now": self.now.isoformat(),
            "pending_irrigation_mm": self.pending_irrigation_mm,
            "irrigation_used_mm": self.irrigation_used_mm, "tool_errors": self.tool_errors,
            "observations": self.observations, "first_irrigation_seq": self.first_irrigation_seq,
            "last_irrigation_time": self.last_irrigation_time, "done": self.done,
            "events": [record.to_dict() for record in self.events],
        })

    @classmethod
    def from_state(cls, payload):
        from evaluate.scenarios.spec import ScenarioSpec
        if payload.get("schema") != 4 or payload.get("fairy_version") != __version__:
            raise ValueError("Incompatible runtime state")
        if fingerprint(payload["scenario"]) != payload["scenario_fingerprint"]:
            raise ValueError("Scenario fingerprint mismatch")
        spec = ScenarioSpec.from_dict(payload["scenario"])
        # No warmup followed by a second restore: construct a fresh engine only once.
        obj = cls.__new__(cls)
        obj.scenario, obj.backend_name = spec, payload["backend"]
        obj.zone = ZoneInfo(spec.timezone)
        obj.deadline = datetime.fromisoformat(spec.deadline).astimezone(obj.zone)
        obj.weather = {day.day: day for day in spec.weather}
        obj.objects = load_objects(spec)
        obj.operations = bind_operations(obj, obj.objects)
        obj.backend = make_backend(obj.backend_name, spec.farm, spec.crop, spec.sowing_date, spec.season)
        obj.physical = obj._make_physical(spec)
        check_backend(obj.operations, obj.backend, spec.crop.crop_id)
        obj.binding_fingerprint = fingerprint(binding_manifest(obj))
        if obj.binding_fingerprint != payload.get("binding_fingerprint"):
            raise ValueError("Operation binding fingerprint mismatch")
        obj.backend.load_state(payload["backend_state"])
        if obj.physical:
            obj.physical.load_state(payload["physical_state"])
        elif payload.get("physical_state") is not None:
            raise ValueError("Unexpected physical state")
        obj.now = obj._state_time(payload["now"])
        if type(payload["done"]) is not bool or type(payload["tool_errors"]) is not int or payload["tool_errors"] < 0:
            raise ValueError("Invalid termination or error count")
        if not datetime.fromisoformat(spec.start_time) <= obj.now <= obj.deadline:
            raise ValueError("Restored clock outside task interval")
        if obj.backend.audit()["next_day"] != obj.now.date().isoformat():
            raise ValueError("Backend clock disagrees with runtime clock")
        from fairy.acps.ontology.farm.spec import finite
        for name in ("pending_irrigation_mm", "irrigation_used_mm"):
            finite(payload[name], name, 0, spec.irrigation_quota_mm + 1e-9)
        if payload["pending_irrigation_mm"] > payload["irrigation_used_mm"]:
            raise ValueError("Pending irrigation exceeds total used")
        for name in ("pending_irrigation_mm", "irrigation_used_mm", "tool_errors", "observations",
                     "first_irrigation_seq", "last_irrigation_time", "done"):
            setattr(obj, name, detached(payload[name]))
        if spec.season:
            rows=obj.backend.state["ridges"]
            count=spec.season.ridge_count
            pending=sum(r["pending_mm"] for r in rows)/count
            settled=sum(r["irrigation_mm"] for r in rows)/count
            if not isclose(pending,obj.pending_irrigation_mm,abs_tol=1e-8) or not isclose(settled+pending,obj.irrigation_used_mm,abs_tol=1e-8):
                raise ValueError("Spatial irrigation disagrees with runtime ledger")
            removed=sum(r["grain_removed_g_m2"]*obj.backend.ridge_area/1000 for r in rows)
            physical=obj.physical.state
            if any(r["planted"] and not physical["prepared"][i] for i, r in enumerate(rows)):
                raise ValueError("Planting disagrees across biological and physical states")
            if not isclose(removed,physical["harvested_dry_kg"]+physical["harvest_loss_dry_kg"],abs_tol=1e-7):
                raise ValueError("Harvest disagrees across biological and physical states")
        else:
            settled = obj.backend.audit()["irrigation_mm"] - sum(amount for _, amount in spec.prefix_irrigation)
            if not isclose(settled + obj.pending_irrigation_mm, obj.irrigation_used_mm, rel_tol=1e-10, abs_tol=1e-8):
                raise ValueError("Backend irrigation disagrees with runtime ledger")
        if not isinstance(payload["events"], list):
            raise ValueError("Invalid event history")
        obj.events = [EventRecord.from_dict(event) for event in payload["events"]]
        if [e.seq for e in obj.events] != list(range(1, len(obj.events) + 1)):
            raise ValueError("Invalid event sequence")
        try:
            obj._validate_restored_history()
        except (KeyError, TypeError, IndexError, AttributeError, OverflowError, OperationRejected) as exc:
            raise ValueError("Malformed restored event history") from exc
        return obj

    def _state_time(self, value):
        """Restored timestamps must carry the configured zone's actual UTC offset."""
        try:
            stamp = datetime.fromisoformat(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("Invalid restored timestamp") from exc
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            raise ValueError("Restored timestamp must include a UTC offset")
        local = stamp.astimezone(self.zone)
        if stamp.utcoffset() != local.utcoffset():
            raise ValueError("Restored timestamp disagrees with scenario timezone")
        return local

    def _validate_restored_history(self):
        """Check the persisted ledger in O(events), without executing any engine or agent.

        This is structural consistency validation, not proof that an untrusted history
        was generated by the model. Full physical verification remains replay's job.
        """
        from fairy.acps.ontology.farm.spec import finite
        spec = self.scenario
        clock = self._state_time(spec.start_time)
        expected_start = dict(scenario_fingerprint=fingerprint(spec.to_dict()),
                              backend=asdict(self.backend.capabilities),
                              binding_fingerprint=self.binding_fingerprint, seed=spec.seed,
                              prefix_days=(clock.date() - datetime.fromisoformat(spec.sowing_date).date()).days)
        if not self.events or self.events[0].kind != "run_started":
            raise ValueError("Missing run start")
        used = pending = 0.0
        settled_inputs = dict(spec.prefix_irrigation)
        prepared, planted, harvested, seeds_used = set(), set(), set(), 0
        errors, observations, first, last, terminated = 0, [], None, None, False
        request = None
        for event in self.events:
            stamp, body = self._state_time(event.time), event.payload
            if terminated or not clock <= stamp <= self.deadline:
                raise ValueError("Invalid event clock or events after termination")
            if event.kind == "run_started":
                if event.seq != 1 or event.cause is not None or stamp != clock or body != detached(expected_start):
                    raise ValueError("Invalid run start")
            elif event.kind == "operation_requested":
                if request is not None or event.cause is not None or stamp != clock:
                    raise ValueError("Invalid operation request ordering")
                if set(body) != {"tool", "arguments", "actor"} or not isinstance(body["actor"], str) or not body["actor"]:
                    raise ValueError("Malformed operation request")
                request = event
                request_start = clock
                advanced = scheduled = completed = False
            elif event.kind == "run_terminated":
                if request is not None or event.cause is not None or stamp != clock or set(body) != {"reason"}:
                    raise ValueError("Invalid termination event")
                if not isinstance(body["reason"], str) or not body["reason"]:
                    raise ValueError("Invalid termination reason")
                terminated = True
            else:
                if request is None or event.cause != request.seq:
                    raise ValueError("Event cause must be its active operation request")
                name, args = request.payload["tool"], request.payload["arguments"]
                if event.kind == "environment_day_completed":
                    timing = self.operations.resolve(OperationRequest(name, args)).definition.timing
                    if timing not in ("advance_clock", "scheduled_job") or (timing == "scheduled_job" and not scheduled):
                        raise ValueError("Operation cannot settle an environment day")
                    if advanced or body["day"] != clock.date().isoformat():
                        raise ValueError("Repeated or noncontiguous environment day")
                    midnight = datetime.combine(clock.date() + timedelta(days=1), time(), self.zone)
                    if stamp != midnight:
                        raise ValueError("Environment day must settle at local midnight")
                    flux = body["flux"]
                    finite(flux["irrigation_mm"], "settled irrigation", 0)
                    if not isclose(flux["irrigation_mm"], pending, rel_tol=1e-10, abs_tol=1e-8):
                        raise ValueError("Daily irrigation disagrees with pending ledger")
                    if not isclose(flux["rain_mm"], self.weather[body["day"]].rain_mm, rel_tol=1e-10, abs_tol=1e-8):
                        raise ValueError("Daily rain disagrees with scenario weather")
                    settled_inputs[body["day"]] = pending
                    pending = 0.0
                    clock = stamp
                elif event.kind == "time_advanced":
                    if advanced or self._state_time(body["from"]) != request_start or self._state_time(body["to"]) != stamp:
                        raise ValueError("Invalid time advancement record")
                    if stamp.date() != clock.date() or stamp <= request_start:
                        raise ValueError("Time advancement skipped daily settlement")
                    duration = job_duration if scheduled else args["hours"]
                    if not scheduled and name != "advance_time":
                        raise ValueError("Operation cannot advance the clock")
                    expected = (request_start.astimezone(timezone.utc) + timedelta(hours=duration)).astimezone(self.zone)
                    if stamp != expected:
                        raise ValueError("Time advancement disagrees with requested duration")
                    clock, advanced = stamp, True
                elif event.kind == "job_scheduled":
                    binding = self.operations.resolve(OperationRequest(name, args))
                    if scheduled or advanced or stamp != request_start or binding.definition.timing != "scheduled_job":
                        raise ValueError("Invalid job scheduling")
                    if (body["operation"] != name or body["request_seq"] != request.seq or
                            self._state_time(body["start_time"]) != request_start):
                        raise ValueError("Job does not match request")
                    job_duration = finite(body["duration_hours"], "job duration", 0)
                    if job_duration == 0:
                        raise ValueError("Job duration must be positive")
                    scheduled = True
                elif event.kind == "job_completed":
                    if not scheduled or not advanced or completed or stamp != clock or body["operation"] != name:
                        raise ValueError("Invalid job completion")
                    completed, job_receipt = True, body["receipt"]
                elif event.kind == "operation_result":
                    if stamp != clock or set(body) != {"ok", "data", "error"} or type(body["ok"]) is not bool:
                        raise ValueError("Malformed operation result")
                    if body["ok"]:
                        binding = self.operations.resolve(OperationRequest(name, args))
                        data = dict(body["data"])
                        status = data.pop("status")
                        binding.definition.validate_result(data)
                        if body["error"] is not None:
                            raise ValueError("Successful result contains an error")
                        if advanced != (binding.definition.timing in ("advance_clock", "scheduled_job")):
                            raise ValueError("Operation timing disagrees with event effects")
                        if scheduled != completed or (scheduled and data != {"receipt": job_receipt}):
                            raise ValueError("Job completion disagrees with operation result")
                        if name in ("irrigate", "irrigate_range"):
                            amount = args["depth_mm"]
                            if name == "irrigate_range":
                                amount = amount * (args["end_ridge"] - args["start_ridge"] + 1) / spec.season.ridge_count
                            used += amount
                            pending += amount
                            first = request.seq if first is None else first
                            last = stamp.isoformat()
                        if spec.season and name in ("prepare_seedbed", "plant", "harvest"):
                            indices = set(self.backend.indices(args))
                            if name == "prepare_seedbed":
                                prepared.update(indices)
                            elif name == "plant":
                                planted.update(indices)
                                seeds_used += len(indices) * ceil(2 * spec.farm.length_m / args["spacing_m"])
                            else:
                                harvested.update(indices)
                        if name == "observe_soil":
                            if self._state_time(data["time"]) != stamp or data["plot_id"] != spec.farm.plot_id:
                                raise ValueError("Observation does not match request time or plot")
                            observations.append({**data, "seq": request.seq})
                        expected_status = dict(time=stamp.isoformat(), timezone=spec.timezone,
                                               irrigation_remaining_mm=spec.irrigation_quota_mm - used,
                                               pending_irrigation_mm=pending, done=False)
                        if status != expected_status:
                            raise ValueError("Result status disagrees with event ledger")
                    else:
                        if advanced or scheduled or stamp != request_start or body["data"] != {} or not isinstance(body["error"], str) or not body["error"]:
                            raise ValueError("Rejected operation has effects or invalid error")
                        errors += 1
                    request = None
                elif event.kind == "engine_failed":
                    if advanced or scheduled or stamp != request_start or body != {"code": "BACKEND_FAILURE"}:
                        raise ValueError("Failed engine operation has effects")
                    request = None
                else:
                    raise ValueError("Unknown event kind")
        if request is not None or clock != self.now:
            raise ValueError("Incomplete operation or history clock mismatch")
        if (terminated != self.done or errors != self.tool_errors or observations != self.observations or
                first != self.first_irrigation_seq or last != self.last_irrigation_time):
            raise ValueError("Runtime metadata disagrees with event history")
        if type(self.first_irrigation_seq) not in (int, type(None)):
            raise ValueError("Invalid first irrigation sequence")
        if not isclose(used, self.irrigation_used_mm, rel_tol=1e-10, abs_tol=1e-8) or not isclose(pending, self.pending_irrigation_mm, rel_tol=1e-10, abs_tol=1e-8):
            raise ValueError("Runtime water ledger disagrees with event history")

        if spec.season:
            rows = self.backend.state["ridges"]
            if (prepared != {i for i, value in enumerate(self.physical.state["prepared"]) if value} or
                    planted != {i for i, row in enumerate(rows) if row["planted"]} or
                    harvested != {i for i, row in enumerate(rows) if row["harvested"]} or
                    seeds_used != self.physical.state["seeds_used"]):
                raise ValueError("Field operations disagree with restored object states")
        forcing = [day for day in spec.weather if day.day < self.now.date().isoformat()]
        if self.backend_name == "wofost":
            journal = self.backend.journal
            if len(journal) != len(forcing):
                raise ValueError("WOFOST journal length disagrees with runtime clock")
            for entry, day in zip(journal, forcing):
                if entry["weather"] != asdict(day) or not isclose(entry["irrigation_mm"], settled_inputs.get(day.day, 0.0), rel_tol=1e-10, abs_tol=1e-8):
                    raise ValueError("WOFOST journal disagrees with scenario forcing or irrigation")
        elif spec.season is None:
            audit = self.backend.audit()
            rain = sum(day.rain_mm for day in forcing)
            gdd = sum(max(0.0, (day.tmin_c + day.tmax_c) / 2 - spec.crop.base_temperature_c) for day in forcing)
            if not isclose(audit["rain_mm"], rain, rel_tol=1e-10, abs_tol=1e-8) or not isclose(audit["gdd"], gdd, rel_tol=1e-10, abs_tol=1e-8):
                raise ValueError("Native totals disagree with scenario forcing")
