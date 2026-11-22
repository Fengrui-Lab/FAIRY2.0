"""Single-writer local-day simulation runtime. No agent SDK or UI imports."""

from __future__ import annotations
from typing import Any, Iterable, Mapping, Sequence, TYPE_CHECKING, cast

if TYPE_CHECKING:
    from evaluate.scenarios.season import SeasonSpec
    from evaluate.scenarios.spec import ScenarioSpec
    from fairy.acps.backends.biology.contracts import SoilReading
    from fairy.acps.backends.physical.native.backend import NativePhysicalBackend
    from fairy.acps.ontology.contracts import JsonObject
    from fairy.acps.ontology.infrastructure.irrigation import IrrigationSystem
    from fairy.acps.ontology.infrastructure.soil_sensor import SoilSensor
    from fairy.events.provenance import SourceIdentity
    from fairy.events.records import PublicStatus
    from fairy.events.registry import OperationRegistry, ProviderObject
from dataclasses import asdict
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo
from math import ceil, isclose
from fairy import __version__
from fairy.acps.backends.biology.contracts import (
    make_backend,
    restore_backend,
    BiologyBackend,
    SeasonBiologyBackend,
    SeedbedBiologyBackend,
    StandRepairBackend,
    SurfaceDrainageBackend,
    BioticManagementBackend,
)
from fairy.acps.ontology.contracts import OperationRequest, OperationResult, OperationRejected
from fairy.events.records import EventRecord, canonical, detached, fingerprint
from fairy.events.registry import load_objects, bind_operations, check_backend, binding_manifest


class Runtime:
    """唯一仿真时钟和事务边界。

    场景声明输入，Ontology提供操作契约，后端拥有物理/生物状态；本类负责路由、
    日界结算、事件与回滚。公开状态和可信审计/恢复包严格分开。"""

    scenario: ScenarioSpec  #: 不可变任务输入，不能被模型修改。
    backend: BiologyBackend  #: 生物状态真值；全季能力通过season_backend取得。
    physical: NativePhysicalBackend | None  #: L1为None；全季唯一装备/物料账本。
    now: datetime  #: 唯一业务时钟，带Asia/Shanghai等显式场景时区。
    deadline: datetime  #: 时间预算上界；作业和显式等待共享此限制。
    objects: Mapping[str, ProviderObject]  #: 按场景加载的身份/提供者，不是第二份物理状态。
    operations: OperationRegistry  #: 当前场景允许的操作及绑定处理器。
    source_identity: SourceIdentity  #: 包源码与数据身份，恢复与回放需匹配。
    pending_irrigation_mm: float  #: 已接受、未日结的全田平均毛水深mm。
    irrigation_used_mm: float  #: 累计已占用额度，包括pending，单位全田平均mm。
    observations: list[JsonObject]  #: L1公开土壤观测及发生序号。
    events: list[EventRecord]  #: 按因果顺序追加的不可变事件，不与ScheduledJob混同。
    tool_errors: int  #: 真实工具拒绝次数；不含本地guard或模型协议错误。
    first_irrigation_seq: int | None  #: 第一次接受灌溉的请求事件号。
    last_irrigation_time: str | None  #: 最近接受灌溉的业务时间，用于复查评价。
    done: bool  #: 已终止的运行不接受新请求；暂停与终止需区分。

    def __init__(
        self,
        scenario: ScenarioSpec,
        backend: str = "native",
        *,
        expected_binding_fingerprint: str | None = None,
    ) -> None:
        """保存显式依赖与初态；模型服务调用发生在决策阶段而非构造阶段。"""
        self.scenario = scenario
        from fairy.events.provenance import source_identity

        self.source_identity = source_identity()
        self.backend_name = backend
        self.zone = ZoneInfo(scenario.timezone)
        self.now = datetime.fromisoformat(scenario.start_time).astimezone(self.zone)
        self.deadline = datetime.fromisoformat(scenario.deadline).astimezone(self.zone)
        self.objects = load_objects(scenario)
        self.operations = bind_operations(self, self.objects)
        self.backend: BiologyBackend = make_backend(
            backend, scenario.farm, scenario.crop, scenario.sowing_date, scenario.season
        )
        self.physical = self._make_physical(scenario)
        check_backend(self.operations, self.backend, scenario.crop.crop_id)
        self.binding_fingerprint = fingerprint(binding_manifest(self))
        if (
            expected_binding_fingerprint is not None
            and self.binding_fingerprint != expected_binding_fingerprint
        ):
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
        self.observations: list[JsonObject] = []
        self.first_irrigation_seq: int | None = None
        self.last_irrigation_time: str | None = None
        self.done = False
        self.events: list[EventRecord] = []
        self._emit(
            "run_started",
            {
                "scenario_fingerprint": fingerprint(scenario.to_dict()),
                "backend": asdict(self.backend.capabilities),
                "binding_fingerprint": self.binding_fingerprint,
                "seed": scenario.seed,
                "prefix_days": (
                    self.now.date() - datetime.fromisoformat(scenario.sowing_date).date()
                ).days,
            },
        )

    @property
    def season_backend(self) -> SeasonBiologyBackend:
        """仅全季场景可用的类型化接口；工厂已保证实现符合全季协议。"""
        if self.scenario.season is None:
            raise ValueError("Full-season backend required")
        return cast(SeasonBiologyBackend, self.backend)

    @property
    def season_physical(self) -> NativePhysicalBackend:
        """取得当前唯一支持的物理账本；L1没有物理装备后端。"""
        if self.physical is None:
            raise ValueError("Full-season physical backend required")
        return self.physical

    @property
    def season_config(self) -> SeasonSpec:
        """取得已验证的全季配置，不将L1伪装成全季。"""
        if self.scenario.season is None:
            raise ValueError("Full-season configuration required")
        return self.scenario.season

    @staticmethod
    def _make_physical(scenario: ScenarioSpec) -> NativePhysicalBackend | None:
        """仅全季场景构建当前Native物理账本；L1返回None。"""
        if scenario.season is None:
            return None
        from fairy.acps.backends.physical.native.backend import NativePhysicalBackend

        return NativePhysicalBackend(scenario.season, scenario.farm)

    def _emit(self, kind: str, payload: JsonObject, cause: int | None = None) -> int:
        """在唯一时钟上追加不可变事件，返回连续序号；cause链接本次操作请求。"""
        record = EventRecord(
            len(self.events) + 1, kind, self.now.isoformat(), cause, canonical(payload)
        )
        self.events.append(record)
        return record.seq

    def public_status(self) -> PublicStatus:
        """返回当前公开时钟、剩余/待结算全田水深mm及done状态。"""
        return dict(
            time=self.now.isoformat(),
            timezone=self.scenario.timezone,
            irrigation_remaining_mm=self.scenario.irrigation_quota_mm - self.irrigation_used_mm,
            pending_irrigation_mm=self.pending_irrigation_mm,
            done=self.done,
        )

    def invoke(self, request: OperationRequest, actor: str = "agent") -> OperationResult:
        """在事务边界验证并执行一次请求。

        拒绝/中断时回滚引擎、库存与时钟；错误计数和合法请求回执另行保留，
        不会让被拒绝的灌溉或作业产生物理副作用。"""
        if self.done:
            raise ValueError("Run already terminated")
        if not isinstance(actor, str) or not actor:
            raise ValueError("Actor must be a non-empty string")
        # Malformed/non-JSON messages are handled by the model adapter, not the simulator.
        request_payload = detached(asdict(request))
        # Snapshot before recording intent, so cancellation is safe even between
        # intent recording, preflight, state updates and result publication.
        saved_backend = self.backend.export_state()
        saved_physical = self.physical.export_state() if self.physical else None
        saved_values = {
            name: detached(getattr(self, name))
            for name in (
                "pending_irrigation_mm",
                "irrigation_used_mm",
                "observations",
                "first_irrigation_seq",
                "last_irrigation_time",
                "tool_errors",
            )
        }
        old_now, old_count = self.now, len(self.events)
        cause = None
        try:
            try:
                cause = self._emit("operation_requested", {**request_payload, "actor": actor})
                binding = self.operations.resolve(request)
                data = binding.handler(request.arguments, cause)
                binding.definition.validate_result(data)
                result = OperationResult(True, {**detached(data), "status": self.public_status()})
                self._emit("operation_result", asdict(result), cause)
            except OperationRejected as exc:
                self._rollback(saved_backend, saved_values, old_now, old_count + 1, saved_physical)
                assert cause is not None  # intent在处理器调用前已记录。
                return self._reject(exc, cause)
            except Exception:
                self._rollback(saved_backend, saved_values, old_now, old_count + 1, saved_physical)
                self._emit("engine_failed", {"code": "BACKEND_FAILURE"}, cause)
                raise
            return result
        except KeyboardInterrupt:
            # Also covers an interrupt during rejection/result publication.
            self._rollback(saved_backend, saved_values, old_now, old_count, saved_physical)
            self.record_cancelled(request, actor)
            raise

    def record_cancelled(self, request: OperationRequest, actor: str) -> None:
        """Replay a cancelled intent; deliberately perform no physical action."""
        cause = self._emit("operation_requested", {**detached(asdict(request)), "actor": actor})
        self._emit("operation_cancelled", {"code": "INTERRUPTED"}, cause)

    def suspend(self, reason: str = "interrupted") -> None:
        """记录可恢复中断；不会把未完成任务伪装为成功终止。"""
        if self.done:
            raise ValueError("Cannot suspend a terminated run")
        if reason != "interrupted":
            raise ValueError("Unsupported suspension reason")
        self._emit("run_suspended", {"reason": reason})

    def _reject(self, error: OperationRejected, cause: int) -> OperationResult:
        """累计真实工具错误并发出无物理副作用的公开拒绝回执。"""
        self.tool_errors += 1
        result = OperationResult(False, error=str(error))
        self._emit("operation_result", asdict(result), cause)
        return result

    def _rollback(
        self,
        backend_state: JsonObject,
        values: JsonObject,
        now: datetime,
        count: int,
        physical_state: JsonObject | None = None,
    ) -> None:
        """从操作前快照恢复生物/物理/时钟/额度，并截断尚未提交的事件。"""
        self.backend = restore_backend(
            self.backend_name,
            self.scenario.farm,
            self.scenario.crop,
            self.scenario.sowing_date,
            backend_state,
            self.scenario.season,
        )
        if self.physical:
            assert physical_state is not None
            self.physical.load_state(physical_state)
        self.now = now
        for name, value in values.items():
            setattr(self, name, value)
        del self.events[count:]

    def _observe(self, provider: SoilSensor, cause: int) -> SoilReading:
        """通过绑定传感器读取当前公开观测并记录时间，不结算任何pending。"""
        data = provider.observe(self.backend, self.now)
        self.observations.append({**data, "seq": cause})
        return data

    def _irrigate(
        self, provider: IrrigationSystem, arguments: JsonObject, cause: int
    ) -> JsonObject:
        """核算并登记全地块毛水深mm、额度与pending；日结由_advance_time负责。"""
        depth = arguments["depth_mm"]
        receipt = provider.prepare(
            depth, self.scenario.farm, self.scenario.irrigation_quota_mm - self.irrigation_used_mm
        )
        if self.scenario.season:
            self.season_backend.queue_irrigation(
                range(self.season_config.ridge_count), float(depth)
            )
        self.pending_irrigation_mm += float(depth)
        self.irrigation_used_mm += float(depth)
        if self.first_irrigation_seq is None:
            self.first_irrigation_seq = cause
        self.last_irrigation_time = self.now.isoformat()
        return receipt

    def _advance_time(self, arguments: JsonObject, cause: int) -> JsonObject:
        """按小时推进唯一时钟，逐个跨越本地午夜。

        每个完整日只调用一次后端advance_day；同日分段推进不重复结算，
        超过期限或精度不足的请求拒绝。"""
        target = (
            self.now.astimezone(timezone.utc) + timedelta(hours=arguments["hours"])
        ).astimezone(self.zone)
        if target <= self.now:
            raise OperationRejected("TIME_STEP_TOO_SMALL")
        if target > self.deadline:
            raise OperationRejected("TIME_BUDGET_EXCEEDED")
        old_now = self.now
        while self.now.date() < target.date():
            day = self.now.date().isoformat()
            midnight = datetime.combine(self.now.date() + timedelta(days=1), time(), self.zone)
            flux = self.backend.advance_day(
                self.weather[day], 0.0 if self.scenario.season else self.pending_irrigation_mm
            )
            self.pending_irrigation_mm = 0.0
            self.now = midnight
            body: JsonObject = {"day": day, "flux": flux}
            if self.scenario.construction_json is not None:
                from evaluate.scenarios.compose import forcing_audit

                body["forcing"] = forcing_audit(self.scenario, day)
            self._emit("environment_day_completed", body, cause)
        self.now = target
        self._emit(
            "time_advanced", {"from": old_now.isoformat(), "to": self.now.isoformat()}, cause
        )
        return {"time": self.now.isoformat()}

    def _seedbed_ready(self, args: JsonObject, *, check_plant: bool) -> JsonObject:
        """Project the seedbed without advancing time, or enforce the planting gate."""
        backend = self.season_backend
        if not isinstance(backend, SeedbedBiologyBackend):
            raise OperationRejected("SEEDBED_OBSERVATION_UNSUPPORTED")
        rows = backend.seedbed_observation(backend.indices(args))
        settled = self.now.date().isoformat() > self.scenario.sowing_date
        if check_plant and not settled:
            raise OperationRejected("SEEDBED_NOT_SETTLED")
        if check_plant and any(not row["temperature_ready"] for row in rows):
            raise OperationRejected("SEEDBED_TOO_COLD")
        return {"observations": rows, "time": self.now.isoformat(), "settled": settled}

    def _season_operation(self, name: str, args: JsonObject, cause: int) -> JsonObject:
        """路由全季观测、分区灌溉与装备作业；装备作业完成前后复检并受同一事务保护。"""
        if self.physical is None:
            raise OperationRejected("UNSUPPORTED_SEASON_OPERATION")
        if name == "observe_stand_range":
            stand_backend = self.season_backend
            if not isinstance(stand_backend, StandRepairBackend):
                raise OperationRejected("STAND_REPAIR_UNAVAILABLE")
            return {"receipt": {"observations": stand_backend.observe_stand(stand_backend.indices(args)),
                                "time": self.now.isoformat()}}
        if name in ("scout_biotic", "treat_biotic"):
            biology = self.season_backend
            if not isinstance(biology, BioticManagementBackend):
                raise OperationRejected("BIOTIC_MANAGEMENT_UNAVAILABLE")
            selected = biology.indices(args)
            if name == "scout_biotic":
                used, queued = biology.protection_ledger()
                return {"receipt": {"observations": biology.observe_biotic(selected),
                                    "time": self.now.isoformat(),
                                    "pending_ridges": [i for i in selected if i in queued],
                                    "protection_remaining_units": self.season_config.plant_protection_units - used}}
            used = biology.queue_biotic_treatment(selected, args["kind"])
            return {"receipt": {"protection_used_units": used, "kind": args["kind"],
                                "settlement": "next_local_midnight"}}
        if name in ("observe_surface_water", "drain_surface"):
            backend = self.season_backend
            if not isinstance(backend, SurfaceDrainageBackend):
                raise OperationRejected("SURFACE_DRAINAGE_UNSUPPORTED")
            surface_ids = backend.indices(args)
            if name == "observe_surface_water":
                return {"receipt": {"observations": backend.surface_water_observation(surface_ids),
                                    "time": self.now.isoformat()}}
            amounts = backend.drain_surface(surface_ids, args["max_depth_mm"])
            total = sum(amounts.values())
            return {"receipt": {"drained": [{"ridge_id": i, "depth_mm": mm} for i, mm in amounts.items()],
                                "field_average_mm": total / self.season_config.ridge_count,
                                "volume_m3": self.scenario.farm.volume_m3(total / self.season_config.ridge_count)}}
        if name == "observe_seedbed_range":
            return {"receipt": self._seedbed_ready(args, check_plant=False)}
        if name in ("inspect_crop", "observe_soil_range"):
            ids: Iterable[int] = self.season_backend.indices(args)
            rows: Sequence[Mapping[str, Any]] = (
                self.season_backend.field_observation(ids)
                if name == "inspect_crop"
                else self.season_backend.soil_observation(ids)
            )
            if name == "inspect_crop":
                statuses = self.season_backend.management_state()
                rows = [
                    {
                        **row,
                        "phase": statuses[row["ridge_id"]].phase,
                        "harvestable": statuses[row["ridge_id"]].harvestable,
                    }
                    for row in rows
                ]
            return {"receipt": {"observations": rows, "time": self.now.isoformat()}}
        if name == "inspect_dryer":
            return {"receipt": self.physical.dryer_observation()}
        if name == "inspect_inventory":
            return {"receipt": self.physical.inventory()}
        if name == "irrigate_range":
            ids = list(self.season_backend.indices(args))
            depth = args["depth_mm"]
            field_mm = depth * len(ids) / self.season_config.ridge_count
            if field_mm > self.scenario.irrigation_quota_mm - self.irrigation_used_mm + 1e-9:
                raise OperationRejected("WATER_QUOTA_EXCEEDED")
            self.season_backend.queue_irrigation(ids, depth)
            self.pending_irrigation_mm += field_mm
            self.irrigation_used_mm += field_mm
            if self.first_irrigation_seq is None:
                self.first_irrigation_seq = cause
            self.last_irrigation_time = self.now.isoformat()
            return {
                "receipt": {
                    "depth_mm": depth,
                    "field_average_mm": field_mm,
                    "volume_m3": self.scenario.farm.volume_m3(field_mm),
                    "settlement": "next_local_midnight",
                }
            }
        check_seedbed = name == "plant" and "observe_seedbed_range" in self.scenario.tools
        if check_seedbed:
            self._seedbed_ready(args, check_plant=True)
        plan = self.physical.plan(
            name, args, self.season_backend, self.weather[self.now.date().isoformat()]
        )
        # A schedule describes proposed work; events describe what actually occurred.
        from fairy.events.operations import ScheduledJob

        job = ScheduledJob(name, self.now.isoformat(), plan["duration_hours"], cause)
        self._emit("job_scheduled", asdict(job), cause)
        self._advance_time({"hours": job.duration_hours}, cause)
        if check_seedbed:
            self._seedbed_ready(args, check_plant=True)
        receipt = self.physical.execute(
            name, args, self.season_backend, self.weather[self.now.date().isoformat()]
        )
        self._emit("job_completed", {"operation": name, "receipt": receipt}, cause)
        return {"receipt": receipt}

    def public_objects(self) -> list[JsonObject]:
        """Only identity, type and target links; no private state or model parameters."""
        return [asdict(spec) for spec in self.scenario.objects]

    def finish(self, reason: str = "completed") -> None:
        """一次性记录终止原因并设置done；不额外推进仿真。"""
        if not isinstance(reason, str) or not reason:
            raise ValueError("Termination reason must be a non-empty string")
        if not self.done:
            count = len(self.events)
            try:
                self._emit("run_terminated", {"reason": reason})
                self.done = True
            except KeyboardInterrupt:
                del self.events[count:]
                self.done = False
                raise

    def audit_state(self) -> JsonObject:
        """Privileged API. Never passed into public agent context."""
        result = detached(self.backend.audit())
        if self.physical:
            result["physical"] = self.physical.audit()
        return result

    def export_state(self) -> JsonObject:
        """导出可信恢复快照；此数据不是LLM公开观测。"""
        return detached(
            {
                "schema": 7,
                "fairy_version": __version__,
                "scenario": self.scenario.to_dict(),
                "source_fingerprint": self.source_identity["fingerprint"],
                "scenario_fingerprint": fingerprint(self.scenario.to_dict()),
                "backend": self.backend_name,
                "binding_fingerprint": self.binding_fingerprint,
                "backend_state": self.backend.export_state(),
                "physical_state": self.physical.export_state() if self.physical else None,
                "now": self.now.isoformat(),
                "pending_irrigation_mm": self.pending_irrigation_mm,
                "irrigation_used_mm": self.irrigation_used_mm,
                "tool_errors": self.tool_errors,
                "observations": self.observations,
                "first_irrigation_seq": self.first_irrigation_seq,
                "last_irrigation_time": self.last_irrigation_time,
                "done": self.done,
                "events": [record.to_dict() for record in self.events],
            }
        )

    @classmethod
    def from_state(cls, payload: JsonObject) -> Runtime:
        """在匹配版本/场景/绑定下构建新实例并验证恢复包，不能重放LLM决策。"""
        from evaluate.scenarios.spec import ScenarioSpec

        if payload.get("schema") != 7 or payload.get("fairy_version") != __version__:
            raise ValueError("Incompatible runtime state")
        from fairy.events.provenance import source_identity

        identity = source_identity()
        if payload.get("source_fingerprint") != identity["fingerprint"]:
            raise ValueError("Executable source fingerprint mismatch")
        if fingerprint(payload["scenario"]) != payload["scenario_fingerprint"]:
            raise ValueError("Scenario fingerprint mismatch")
        spec = ScenarioSpec.from_dict(payload["scenario"])
        # No warmup followed by a second restore: construct a fresh engine only once.
        obj = cls.__new__(cls)
        obj.source_identity = identity
        obj.scenario, obj.backend_name = spec, payload["backend"]
        obj.zone = ZoneInfo(spec.timezone)
        obj.deadline = datetime.fromisoformat(spec.deadline).astimezone(obj.zone)
        obj.weather = {day.day: day for day in spec.weather}
        obj.objects = load_objects(spec)
        obj.operations = bind_operations(obj, obj.objects)
        obj.backend = make_backend(
            obj.backend_name, spec.farm, spec.crop, spec.sowing_date, spec.season
        )
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
        if (
            type(payload["done"]) is not bool
            or type(payload["tool_errors"]) is not int
            or payload["tool_errors"] < 0
        ):
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
        for name in (
            "pending_irrigation_mm",
            "irrigation_used_mm",
            "tool_errors",
            "observations",
            "first_irrigation_seq",
            "last_irrigation_time",
            "done",
        ):
            setattr(obj, name, detached(payload[name]))
        if spec.season:
            rows = obj.season_backend.management_state()
            count = obj.season_config.ridge_count
            pending = sum(r.pending_mm for r in rows) / count
            settled = sum(r.irrigation_mm for r in rows) / count
            if not isclose(pending, obj.pending_irrigation_mm, abs_tol=1e-8) or not isclose(
                settled + pending, obj.irrigation_used_mm, abs_tol=1e-8
            ):
                raise ValueError("Spatial irrigation disagrees with runtime ledger")
            removed = sum(r.removed_dry_kg for r in rows)
            physical = obj.season_physical.state
            if any(r.planted and not physical["prepared"][i] for i, r in enumerate(rows)):
                raise ValueError("Planting disagrees across biological and physical states")
            if not isclose(
                removed,
                physical["harvested_dry_kg"] + physical["harvest_loss_dry_kg"],
                abs_tol=1e-7,
            ):
                raise ValueError("Harvest disagrees across biological and physical states")
        else:
            settled = obj.backend.audit()["irrigation_mm"] - sum(
                amount for _, amount in spec.prefix_irrigation
            )
            if not isclose(
                settled + obj.pending_irrigation_mm,
                obj.irrigation_used_mm,
                rel_tol=1e-10,
                abs_tol=1e-8,
            ):
                raise ValueError("Backend irrigation disagrees with runtime ledger")
        if not isinstance(payload["events"], list):
            raise ValueError("Invalid event history")
        obj.events = [EventRecord.from_dict(event) for event in payload["events"]]
        if [e.seq for e in obj.events] != list(range(1, len(obj.events) + 1)):
            raise ValueError("Invalid event sequence")
        try:
            obj._validate_restored_history()
        except (
            KeyError,
            TypeError,
            IndexError,
            AttributeError,
            OverflowError,
            OperationRejected,
        ) as exc:
            raise ValueError("Malformed restored event history") from exc
        return obj

    def _state_time(self, value: Any) -> datetime:
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

    def _validate_restored_history(self) -> None:
        """Check the persisted ledger in O(events), without executing any engine or agent.

        This is structural consistency validation, not proof that an untrusted history
        was generated by the model. Full physical verification remains replay's job.
        """
        from fairy.acps.ontology.farm.spec import finite

        spec = self.scenario
        clock = self._state_time(spec.start_time)
        expected_start = dict(
            scenario_fingerprint=fingerprint(spec.to_dict()),
            backend=asdict(self.backend.capabilities),
            binding_fingerprint=self.binding_fingerprint,
            seed=spec.seed,
            prefix_days=(clock.date() - datetime.fromisoformat(spec.sowing_date).date()).days,
        )
        if not self.events or self.events[0].kind != "run_started":
            raise ValueError("Missing run start")
        used = pending = 0.0
        settled_inputs = dict(spec.prefix_irrigation)
        season_inputs = []
        surface_drains: dict[int, float] = {}
        protection_used = 0
        pending_protection: dict[int, str] = {}
        replanted_sites: dict[int, int] = {}
        dryer_history: list[str] = []
        prepared, planted, harvested, seeds_used = set(), set(), set(), 0
        errors, observations, first, last, terminated = 0, [], None, None, False
        request = None
        job_duration = 0.0
        for event in self.events:
            stamp, body = self._state_time(event.time), event.payload
            if terminated or not clock <= stamp <= self.deadline:
                raise ValueError("Invalid event clock or events after termination")
            if event.kind == "run_started":
                if (
                    event.seq != 1
                    or event.cause is not None
                    or stamp != clock
                    or body != detached(expected_start)
                ):
                    raise ValueError("Invalid run start")
            elif event.kind == "operation_requested":
                if request is not None or event.cause is not None or stamp != clock:
                    raise ValueError("Invalid operation request ordering")
                if (
                    set(body) != {"tool", "arguments", "actor"}
                    or not isinstance(body["actor"], str)
                    or not body["actor"]
                ):
                    raise ValueError("Malformed operation request")
                request = event
                request_start = clock
                advanced = scheduled = completed = False
            elif event.kind == "run_terminated":
                if (
                    request is not None
                    or event.cause is not None
                    or stamp != clock
                    or set(body) != {"reason"}
                ):
                    raise ValueError("Invalid termination event")
                if not isinstance(body["reason"], str) or not body["reason"]:
                    raise ValueError("Invalid termination reason")
                terminated = True
            elif event.kind == "run_suspended":
                if (
                    request is not None
                    or event.cause is not None
                    or stamp != clock
                    or body != {"reason": "interrupted"}
                ):
                    raise ValueError("Invalid suspension event")
            else:
                if request is None or event.cause != request.seq:
                    raise ValueError("Event cause must be its active operation request")
                name, args = request.payload["tool"], request.payload["arguments"]
                if event.kind == "environment_day_completed":
                    from evaluate.scenarios.compose import forcing_audit

                    expected_forcing = forcing_audit(spec, body["day"])
                    if expected_forcing is not None:
                        if body.get("forcing") != expected_forcing or set(body) != {
                            "day",
                            "flux",
                            "forcing",
                        }:
                            raise ValueError(
                                "Daily forcing provenance disagrees with the construction"
                            )
                    elif set(body) != {"day", "flux"}:
                        raise ValueError("Unexpected forcing provenance")
                    timing = self.operations.resolve(OperationRequest(name, args)).definition.timing
                    if timing not in ("advance_clock", "scheduled_job") or (
                        timing == "scheduled_job" and not scheduled
                    ):
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
                    if not isclose(
                        flux["rain_mm"],
                        self.weather[body["day"]].rain_mm,
                        rel_tol=1e-10,
                        abs_tol=1e-8,
                    ):
                        raise ValueError("Daily rain disagrees with scenario weather")
                    season_inputs.append(
                        dict(kind="day", day=body["day"], weather=asdict(self.weather[body["day"]]))
                    )
                    settled_inputs[body["day"]] = pending
                    pending = 0.0
                    pending_protection.clear()
                    clock = stamp
                elif event.kind == "time_advanced":
                    if (
                        advanced
                        or self._state_time(body["from"]) != request_start
                        or self._state_time(body["to"]) != stamp
                    ):
                        raise ValueError("Invalid time advancement record")
                    if stamp.date() != clock.date() or stamp <= request_start:
                        raise ValueError("Time advancement skipped daily settlement")
                    duration = job_duration if scheduled else args["hours"]
                    if not scheduled and name != "advance_time":
                        raise ValueError("Operation cannot advance the clock")
                    expected = (
                        request_start.astimezone(timezone.utc) + timedelta(hours=duration)
                    ).astimezone(self.zone)
                    if stamp != expected:
                        raise ValueError("Time advancement disagrees with requested duration")
                    clock, advanced = stamp, True
                elif event.kind == "job_scheduled":
                    binding = self.operations.resolve(OperationRequest(name, args))
                    if (
                        scheduled
                        or advanced
                        or stamp != request_start
                        or binding.definition.timing != "scheduled_job"
                    ):
                        raise ValueError("Invalid job scheduling")
                    if (
                        body["operation"] != name
                        or body["request_seq"] != request.seq
                        or self._state_time(body["start_time"]) != request_start
                    ):
                        raise ValueError("Job does not match request")
                    job_duration = finite(body["duration_hours"], "job duration", 0)
                    if job_duration == 0:
                        raise ValueError("Job duration must be positive")
                    scheduled = True
                elif event.kind == "job_completed":
                    if (
                        not scheduled
                        or not advanced
                        or completed
                        or stamp != clock
                        or body["operation"] != name
                    ):
                        raise ValueError("Invalid job completion")
                    completed, job_receipt = True, body["receipt"]
                elif event.kind == "operation_result":
                    if (
                        stamp != clock
                        or set(body) != {"ok", "data", "error"}
                        or type(body["ok"]) is not bool
                    ):
                        raise ValueError("Malformed operation result")
                    if body["ok"]:
                        binding = self.operations.resolve(OperationRequest(name, args))
                        data = dict(body["data"])
                        status = data.pop("status")
                        binding.definition.validate_result(data)
                        if body["error"] is not None:
                            raise ValueError("Successful result contains an error")
                        if advanced != (
                            binding.definition.timing in ("advance_clock", "scheduled_job")
                        ):
                            raise ValueError("Operation timing disagrees with event effects")
                        if scheduled != completed or (
                            scheduled and data != {"receipt": job_receipt}
                        ):
                            raise ValueError("Job completion disagrees with operation result")
                        if name in ("irrigate", "irrigate_range"):
                            amount = args["depth_mm"]
                            if name == "irrigate_range":
                                amount = (
                                    amount
                                    * (args["end_ridge"] - args["start_ridge"] + 1)
                                    / self.season_config.ridge_count
                                )
                            used += amount
                            pending += amount
                            first = request.seq if first is None else first
                            last = stamp.isoformat()
                        if spec.season and name in ("prepare_seedbed", "plant", "harvest"):
                            indices = set(self.season_backend.indices(args))
                            if name == "prepare_seedbed":
                                prepared.update(indices)
                            elif name == "plant":
                                planted.update(indices)
                                seeds_used += len(indices) * ceil(
                                    2 * spec.farm.length_m / args["spacing_m"]
                                )
                            else:
                                harvested.update(indices)
                        if spec.season and name in (
                            "plant",
                            "harvest",
                            "irrigate_range",
                            "irrigate",
                        ):
                            ids = (
                                list(self.season_backend.indices(args))
                                if name != "irrigate"
                                else list(range(self.season_config.ridge_count))
                            )
                            entry: JsonObject = dict(
                                kind={
                                    "plant": "plant",
                                    "harvest": "harvest",
                                    "irrigate_range": "irrigate",
                                    "irrigate": "irrigate",
                                }[name],
                                day=stamp.date().isoformat(),
                                indices=ids,
                            )
                            if name == "plant":
                                entry["density"] = (
                                    2
                                    / (spec.farm.width_m / self.season_config.ridge_count)
                                    / args["spacing_m"]
                                )
                            elif name != "harvest":
                                entry["depth"] = args["depth_mm"]
                            season_inputs.append(entry)
                        if name in ("dry_grain", "store_grain", "repair_dryer"):
                            dryer_history.append(name)
                        if name == "replant_gaps":
                            receipt = data["receipt"]
                            filled = receipt["filled_sites"]
                            sites = {row["ridge_id"]: row["seeds"] for row in filled}
                            if (list(sites) != list(self.season_backend.indices(args))
                                or len(sites) != len(filled)
                                or any(type(count) is not int or count <= 0 for count in sites.values())
                                or sites.keys() & replanted_sites.keys()
                                or sum(sites.values()) != receipt["seeds_used"]):
                                raise ValueError("Invalid replant site or seed ledger")
                            replanted_sites.update(sites)
                            seeds_used += receipt["seeds_used"]
                        if name == "treat_biotic":
                            receipt = data["receipt"]
                            treated_ids = self.season_backend.indices(args)
                            if receipt != {"protection_used_units": len(treated_ids), "kind": args["kind"],
                                           "settlement": "next_local_midnight"} or any(i in pending_protection for i in treated_ids):
                                raise ValueError("Invalid or duplicated crop protection receipt")
                            pending_protection.update({i: args["kind"] for i in treated_ids})
                            protection_used += len(treated_ids)
                        if name == "drain_surface":
                            receipt = data["receipt"]
                            rows = receipt["drained"]
                            drains = {row["ridge_id"]: row["depth_mm"] for row in rows}
                            if list(drains) != list(self.season_backend.indices(args)) or len(rows) != len(drains):
                                raise ValueError("Drainage receipt targets differ from the request")
                            total = 0.0
                            for i, mm in drains.items():
                                total += finite(mm, "managed surface outflow", 0, args["max_depth_mm"])
                                surface_drains[i] = surface_drains.get(i, 0.0) + mm
                            field_mm = total / self.season_config.ridge_count
                            if (total <= 0 or receipt["field_average_mm"] != field_mm
                                or receipt["volume_m3"] != spec.farm.volume_m3(field_mm)):
                                raise ValueError("Inconsistent drainage receipt")
                        if name == "observe_soil":
                            if (
                                self._state_time(data["time"]) != stamp
                                or data["plot_id"] != spec.farm.plot_id
                            ):
                                raise ValueError("Observation does not match request time or plot")
                            observations.append({**data, "seq": request.seq})
                        expected_status = dict(
                            time=stamp.isoformat(),
                            timezone=spec.timezone,
                            irrigation_remaining_mm=spec.irrigation_quota_mm - used,
                            pending_irrigation_mm=pending,
                            done=False,
                        )
                        if status != expected_status:
                            raise ValueError("Result status disagrees with event ledger")
                    else:
                        if (
                            advanced
                            or scheduled
                            or stamp != request_start
                            or body["data"] != {}
                            or not isinstance(body["error"], str)
                            or not body["error"]
                        ):
                            raise ValueError("Rejected operation has effects or invalid error")
                        errors += 1
                    request = None
                elif event.kind == "operation_cancelled":
                    if (
                        advanced
                        or scheduled
                        or stamp != request_start
                        or body != {"code": "INTERRUPTED"}
                    ):
                        raise ValueError("Cancelled operation has effects")
                    request = None
                elif event.kind == "engine_failed":
                    if (
                        advanced
                        or scheduled
                        or stamp != request_start
                        or body != {"code": "BACKEND_FAILURE"}
                    ):
                        raise ValueError("Failed engine operation has effects")
                    request = None
                else:
                    raise ValueError("Unknown event kind")
        if request is not None or clock != self.now:
            raise ValueError("Incomplete operation or history clock mismatch")
        if (
            terminated != self.done
            or errors != self.tool_errors
            or observations != self.observations
            or first != self.first_irrigation_seq
            or last != self.last_irrigation_time
        ):
            raise ValueError("Runtime metadata disagrees with event history")
        if type(self.first_irrigation_seq) not in (int, type(None)):
            raise ValueError("Invalid first irrigation sequence")
        if not isclose(used, self.irrigation_used_mm, rel_tol=1e-10, abs_tol=1e-8) or not isclose(
            pending, self.pending_irrigation_mm, rel_tol=1e-10, abs_tol=1e-8
        ):
            raise ValueError("Runtime water ledger disagrees with event history")

        if spec.season:
            rows = self.season_backend.management_state()
            if (
                prepared
                != {i for i, value in enumerate(self.season_physical.state["prepared"]) if value}
                or planted != {i for i, row in enumerate(rows) if row.planted}
                or harvested != {i for i, row in enumerate(rows) if row.harvested}
                or seeds_used != self.season_physical.state["seeds_used"]
            ):
                raise ValueError("Field operations disagree with restored object states")
            self.season_physical.validate_dryer_history(dryer_history)
        if isinstance(self.backend, StandRepairBackend) and self.backend.replant_ledger() != replanted_sites:
            raise ValueError("Replanted cohorts disagree with event receipts")
        if isinstance(self.backend, BioticManagementBackend) and self.backend.protection_ledger() != (
            protection_used, pending_protection
        ):
            raise ValueError("Crop protection state disagrees with event receipts")
        if isinstance(self.backend, SurfaceDrainageBackend) and any(
            not isclose(amount, surface_drains.get(i, 0.0), abs_tol=1e-9)
            for i, amount in enumerate(self.backend.managed_surface_drains())
        ):
            raise ValueError("Surface drainage state disagrees with event receipts")
        forcing = [day for day in spec.weather if day.day < self.now.date().isoformat()]
        if self.backend_name == "wofost" and spec.season:
            from fairy.acps.backends.biology.wofost.season import WofostSeasonBackend

            if cast(WofostSeasonBackend, self.backend).inputs != season_inputs:
                raise ValueError(
                    "WOFOST season input journal disagrees with public operation history"
                )
        if self.backend_name == "wofost":
            from fairy.acps.backends.biology.wofost.backend import WofostBackend
            from fairy.acps.backends.biology.wofost.season import WofostSeasonBackend

            journal = cast(WofostBackend | WofostSeasonBackend, self.backend).journal
            if len(journal) != len(forcing):
                raise ValueError("WOFOST journal length disagrees with runtime clock")
            for entry, day in zip(journal, forcing):
                if entry["weather"] != asdict(day) or not isclose(
                    entry["irrigation_mm"],
                    settled_inputs.get(day.day, 0.0),
                    rel_tol=1e-10,
                    abs_tol=1e-8,
                ):
                    raise ValueError("WOFOST journal disagrees with scenario forcing or irrigation")
        elif spec.season is None:
            audit = self.backend.audit()
            rain = sum(day.rain_mm for day in forcing)
            gdd = sum(
                max(0.0, (day.tmin_c + day.tmax_c) / 2 - spec.crop.base_temperature_c)
                for day in forcing
            )
            if not isclose(audit["rain_mm"], rain, rel_tol=1e-10, abs_tol=1e-8) or not isclose(
                audit["gdd"], gdd, rel_tol=1e-10, abs_tol=1e-8
            ):
                raise ValueError("Native totals disagree with scenario forcing")
