"""Local agent services and evidence guards. Receives no Runtime or hidden state."""

from __future__ import annotations
from typing import Any, Callable, TYPE_CHECKING, cast

if TYPE_CHECKING:
    from fairy.acps.ontology.contracts import JsonObject, OperationRequest, OperationResult
    from fairy.agents.loop.context.public import PublicContext
    from fairy.agents.models.numeric.irrigation import NumericAdvisor
    from fairy.events.records import HistoryTurn
from fairy.events.records import detached, fingerprint, canonical
from datetime import datetime, time, timedelta
from fairy.agents.models.numeric.irrigation import features
from .public import PublicView, grouped_context
from .guards import guard
from .memory import MemorySnapshot
from fairy.agents.loop.context.history import OBSERVATIONS
from fairy.agents.loop.context.kg.lifecycle import validate_knowledge

ADVICE_TOOL = {
    "type": "function",
    "function": {
        "name": "request_irrigation_advice",
        "description": "Ask the frozen trained numerical adviser for full-field irrigation depth. Requires current-day full-field crop/soil/current weather/forecast observations. Zero means no additional water; missing evidence is not zero. This service never executes irrigation or advances time.",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
    },
}


class AutogenExtension:
    """公开Agent的生命周期、检查、数值建议和经验扩展。

    只接收PublicContext，不持有Runtime；trace记录局部决策，dialogue用于下一次模型输入。
    正剂量仍须LLM发出真实灌溉请求；服务调用不推进仿真时钟。"""

    knowledge: JsonObject  #: 经来源校验的生命周期卡，单次运行固定。
    advisor: NumericAdvisor  #: 冻结数值顾问，不持有Runtime。
    memory: MemorySnapshot  #: 运行内冻结的经验检索快照。
    trace: list[JsonObject]  #: 本地服务、guard与真实物理回执的统一决策轨迹。
    dialogue: list[HistoryTurn]  #: 物理或本地服务回执，保留各自来源序号。
    active_advice: JsonObject | None  #: 与公开证据绑定、尚未消费的正剂量建议。
    used_windows: set[str]  #: 已完成数值决策的业务日期，防止同日重复灌溉。
    stop_reason: str | None  #: 有限无进展/重复失败预算的终止原因。

    def __init__(
        self,
        knowledge: JsonObject,
        advisor: NumericAdvisor,
        *,
        memory: MemorySnapshot | None = None,
        max_chars: int = 24000,
        on_decision: Callable[[JsonObject], None] | None = None,
        state: JsonObject | None = None,
    ) -> None:
        """保存显式依赖与初态；模型服务调用发生在决策阶段而非构造阶段。"""
        self.knowledge = knowledge
        self.advisor = advisor
        self.memory = memory or MemorySnapshot()
        self.max_chars = max_chars
        self.trace = []
        self.dialogue = []
        self.round = 0
        self.active_advice: JsonObject | None = None
        self.used_windows = set()
        self.stop_reason = None
        self.repeats = {}
        self.idle = 0
        self.phase = None
        self.missed_windows = set()
        self.visited_windows = set()
        self.seen_evidence = set()
        self.window_cache = {}
        self.on_decision = on_decision
        if state is not None:
            if (
                state["knowledge_fingerprint"] != fingerprint(knowledge)
                or state["memory_fingerprint"] != self.memory.fingerprint
                or state["policy_fingerprint"] != advisor.fingerprint
            ):
                raise ValueError("Cannot restore different knowledge, memory or numerical policy")
            saved = detached(state["continuation_state"])
            self.trace = detached(state["decision_trace"])
            self.dialogue = saved["dialogue"]
            self.round = saved["round"]
            self.active_advice = saved["active_advice"]
            self.phase = saved["phase"]
            self.used_windows = set(saved["used_windows"])
            self.visited_windows = set(state["numeric_windows_visited"])
            self.missed_windows = set(state["numeric_windows_missed"])
            self.repeats = saved["repeats"]
            self.idle = saved["idle"]
            self.seen_evidence = set(saved["seen_evidence"])
            self.window_cache = detached(saved["window_cache"])
            self.stop_reason = state["stop_reason"]

    def augment(self, context: PublicContext) -> PublicContext:
        """给公开上下文加入阶段、冻结经验和建议状态，并保留原始观测证据。"""
        validate_knowledge(self.knowledge, context)
        self.round += 1
        view = PublicView(context)
        self.phase = view.phase()
        card = detached(next(p for p in self.knowledge["phases"] if p["id"] == self.phase))
        if (
            self.phase == "establishment"
            and len(context.get("progress", {}).get("planted_ridges", ())) == view.count
        ):
            card["goal"] = (
                "Planting is already complete. Verify emergence from crop observations and continue daily crop, root-water and numerical-advice management while waiting; do not plant again."
            )
        c = detached(context)
        if self.dialogue:
            c["history"] = detached(self.dialogue[-4:])
        # Agent-service turns displace physical receipts. Recompute observation
        # retention against the actual model history, not the old physical slice.
        candidates = list(context.get("history", ())) + list(
            context.get("public_memory", {}).get("observations", ())
        )
        candidates.sort(key=lambda turn: (turn["time"], turn.get("event_seq", 0)))
        latest = {}
        for turn in candidates:
            if turn["request"]["tool"] in OBSERVATIONS and turn["result"].get("ok"):
                latest[fingerprint(turn["request"])] = turn
        kept = {fingerprint(turn) for turn in c["history"]}
        c["public_memory"]["observations"] = [
            detached(turn) for turn in latest.values() if fingerprint(turn) not in kept
        ]
        c["tools"].append(detached(ADVICE_TOOL))
        if getattr(self.advisor, "manifest", {}).get("algorithm") != "MaskablePPO":
            c["tools"][-1]["function"]["description"] = c["tools"][-1]["function"][
                "description"
            ].replace("frozen trained numerical adviser", "declared untrained numerical baseline")
        c["oracle_guidance"] = {
            "phase": self.phase,
            "lifecycle_card": card,
            "daily_window": {
                "day": view.now.date().isoformat(),
                "already_assessed": view.now.date().isoformat() in self.window_cache,
                "complete": view.now.date().isoformat() in self.used_windows,
                "clock_has_to_advance_for_next_day": True,
            },
            "service_progress": {
                "consecutive_unproductive_calls": self.idle,
                "stop_limit": 12,
                "warning": (
                    "Repeated reads or consultations do not advance time. Choose legitimate work or explicitly advance_time to the next daily window."
                    if self.idle >= 4
                    else None
                ),
            },
            "workflow_phase_is_not_crop_stage": True,
            "memory_snapshot": self.memory.fingerprint,
            "experiences": self.memory.retrieve(
                self.phase,
                profile={
                    "backend": context["constraints"].get("backend", "native"),
                    "cultivar": context["constraints"]["cultivar"],
                    "contract_fingerprint": self.knowledge["contract_fingerprint"],
                },
            ),
            "numeric_policy": self.advisor.fingerprint,
            "latest_advice": self.active_advice,
            "instructions": "You choose every observation, farm operation and wait. The phase manager never executes work. After planting, use daily management windows: read current crop, soil, weather and forecast, then request numerical advice. A zero recommendation means wait and recheck, never call irrigation with zero. To irrigate, use exactly the active advice parameters without adding advice_id to the farm tool. Renew stale advice. Read inventory after container-changing actions; dry only if needed. Read crop until emergence; harvest requires the observed harvestable flag (Native R8 or WOFOST mature). Use progress.public_containers (derived from your public receipts) and latest_harvest_receipt to track known combine/trailer loads and actual recovered batch amounts. These do not predict unharvested grain. Before each new harvest, compare known free bin capacity with a conservative batch estimate from observed harvests; unload or reduce the batch when necessary. Do not repeat a capacity failure without unloading or reducing the batch. No potential-yield score is used.",
        }
        c = grouped_context(c)
        c["context_policy"].update(
            projection="lossless identical public rows grouped",
            autogen_max_chars=self.max_chars,
            physical_history_available=c["context_policy"]["history_available"],
            agent_history_available=len(self.dialogue),
        )
        while True:
            c["context_policy"]["history_kept"] = len(c["history"])
            for _ in range(3):
                c["context_policy"]["autogen_chars"] = len(canonical(c))
            if len(canonical(c)) <= self.max_chars:
                break
            if c["history"]:
                c["history"].pop(0)
            else:
                raise ValueError("Autogen guidance exceeds configured public budget")
        return c

    def _record(
        self, request: OperationRequest, context: PublicContext, kind: str, **data: Any
    ) -> JsonObject:
        """将局部服务/检查/物理回执写入Agent决策轨迹，保持连续序号。"""
        row = {
            "seq": len(self.trace) + 1,
            "model_round": self.round,
            "time": context["status"]["time"],
            "phase": self.phase,
            "kind": kind,
            "tool": request.tool,
            "request_fingerprint": fingerprint(
                {"tool": request.tool, "arguments": request.arguments}
            ),
            **data,
        }
        self.trace.append(row)
        if self.on_decision:
            self.on_decision(detached(row))
        return row

    def _feedback(
        self, request: OperationRequest, context: PublicContext, data: JsonObject
    ) -> None:
        """记录未执行物理操作的局部反馈，并累计有限的无进展预算。"""
        self.dialogue.append(
            {
                "request": {"tool": request.tool, "arguments": detached(request.arguments)},
                "result": {"executed": False, "agent_service_feedback": data},
                "time": context["status"]["time"],
                "decision_seq": len(self.trace),
            }
        )
        self.idle += 1
        if self.idle >= 12:
            self.stop_reason = "agent_service_no_progress"

    def intercept(self, request: OperationRequest, context: PublicContext) -> bool:
        """True means local service/block handled; False forwards unchanged request."""
        view = PublicView(context)
        if request.tool == "request_irrigation_advice":
            try:
                if request.arguments:
                    raise ValueError("The advice service takes no parameters")
                if view.status["pending_irrigation_mm"] != 0:
                    raise ValueError("PENDING_IRRIGATION:wait for settlement and reobserve")
                window = view.now.date().isoformat()
                next_window = datetime.combine(
                    view.now.date() + timedelta(days=1), time.min, tzinfo=view.now.tzinfo
                )
                if window in self.used_windows:
                    raise ValueError(
                        "WINDOW_COMPLETE: this day already has a completed numerical decision. Reads do not open a new window; explicitly advance_time or do other required work. Next window: "
                        + next_window.isoformat()
                    )
                if self.phase not in ("establishment", "growth_management"):
                    raise ValueError("ADVICE_SCOPE:management before R8 only")
                if len(context.get("progress", {}).get("planted_ridges", ())) != view.count:
                    raise ValueError(
                        "ADVICE_SCOPE:the trained management window starts after full-field planting"
                    )
                evidence = view.evidence_id()
                if window in self.window_cache:
                    advice = detached(self.window_cache[window])
                    if advice["features"] != features(context):
                        raise ValueError(
                            "WINDOW_INPUT_CHANGED: numerical input differs; the next scheduled decision is on a new day"
                        )
                    mode = "CACHED" if advice["evidence_id"] == evidence else "REVALIDATED"
                else:
                    advice = dict(self.advisor.advise(context))
                    mode = "ADVISED"
                    advice["decision_id"] = fingerprint(
                        {
                            "window": window,
                            "features": advice["features"],
                            "policy": self.advisor.fingerprint,
                        }
                    )
                self.active_advice = {
                    **advice,
                    "advice_id": fingerprint(
                        {"decision_id": advice["decision_id"], "evidence": evidence}
                    ),
                    "evidence_id": evidence,
                    "window": window,
                    "parameters": (
                        None
                        if advice["dose_mm"] == 0
                        else {
                            "start_ridge": 0,
                            "end_ridge": view.count - 1,
                            "depth_mm": advice["dose_mm"],
                        }
                    ),
                }
                self.window_cache[window] = detached(self.active_advice)
                if advice["dose_mm"] == 0:
                    self.used_windows.add(window)
                self.visited_windows.add(window)
                feedback = {
                    "status": mode,
                    "advice": self.active_advice,
                    "executed": False,
                    "clock_advanced": False,
                    "model_inference_performed": mode == "ADVISED",
                    "next_window_at": next_window.isoformat(),
                    "hours_until_next_window": (next_window - view.now).total_seconds() / 3600,
                    "window_result": (
                        "Zero input completes this daily decision; advance_time explicitly before the next window. Do not repeat read-only calls at the same clock."
                        if advice["dose_mm"] == 0
                        else "To irrigate, submit the exact parameters; the effect settles at local midnight. No operation was automatically executed."
                    ),
                }
            except ValueError as exc:
                feedback = {
                    "status": (
                        "NEED_OBSERVATION"
                        if str(exc).startswith("NEED_OBSERVATION:")
                        else "BLOCKED"
                    ),
                    "reason": str(exc),
                    "executed": False,
                    "zero_advice": False,
                }
            self._record(request, context, "service", feedback=feedback)
            self._feedback(request, context, feedback)
            return True
        check = guard(context, request)
        if check.status == "PASS" and request.tool == "irrigate_range":
            advice = self.active_advice
            if (
                not advice
                or advice["evidence_id"] != view.evidence_id()
                or advice["parameters"] != request.arguments
            ):
                from .guards import Check

                check = Check(
                    "BLOCKED",
                    "matching_current_advice",
                    "Request fresh numerical advice and use its exact parameters",
                    "generator_protocol",
                )
        if check.status != "PASS":
            data = check.to_dict()
            self._record(
                request,
                context,
                "guard",
                **{k: data[k] for k in ("status", "rule", "source")},
                feedback=data,
            )
            key = fingerprint(
                {
                    "request": request.__dict__,
                    "status": context["status"],
                    "evidence": view.turns,
                    "rule": check.rule,
                }
            )
            self.repeats[key] = self.repeats.get(key, 0) + 1
            if self.repeats[key] >= 3:
                self.stop_reason = "precondition_retry_budget"
            self._feedback(request, context, data)
            return True
        return False

    def observe(
        self,
        request: OperationRequest,
        result: OperationResult,
        turn: HistoryTurn,
        context: PublicContext,
    ) -> None:
        """记录真实物理回执并更新公开阶段、建议消费和无进展状态；不再次执行工具。"""
        extra: JsonObject = {}
        if result.ok and request.tool == "irrigate_range":
            # 已接受的实际灌溉必须对应一个未消费的建议证书。
            assert self.active_advice is not None
            extra = {"advice_id": self.active_advice["advice_id"]}
        self._record(
            request,
            context,
            "physical",
            ok=result.ok,
            error=result.error,
            physical_event_seq=turn["event_seq"],
            **extra,
        )
        self.dialogue.append(detached(turn))
        if result.ok and request.tool in (
            "advance_time",
            "prepare_seedbed",
            "plant",
            "harvest",
            "unload",
            "dry_grain",
            "store_grain",
            "irrigate_range",
        ):
            self.idle = 0
        else:
            identity = fingerprint(
                {"tool": request.tool, "arguments": request.arguments, "result": result.__dict__}
            )
            if result.ok and identity not in self.seen_evidence:
                self.idle = 0
                self.seen_evidence.add(identity)
            else:
                self.idle += 1
        if self.idle >= 12:
            self.stop_reason = "agent_service_no_progress"
        if result.ok and request.tool == "irrigate_range":
            assert self.active_advice is not None
            self.used_windows.add(self.active_advice["window"])
            self.active_advice = None
        if (
            result.ok
            and request.tool == "advance_time"
            and self.phase in ("establishment", "growth_management")
            and len(context.get("progress", {}).get("planted_ridges", ()))
            == context["constraints"]["ridge_count"]
        ):
            window = context["status"]["time"][:10]
            if window not in self.visited_windows:
                self.missed_windows.add(window)

    def snapshot(self) -> JsonObject:
        """保存知识、经验、策略身份及扩展续跑状态，不调用模型或物理引擎。"""
        return {
            "schema": 1,
            "knowledge_fingerprint": fingerprint(self.knowledge),
            "knowledge": self.knowledge,
            "memory_fingerprint": self.memory.fingerprint,
            "memory_snapshot": self.memory.rows,
            "policy_fingerprint": self.advisor.fingerprint,
            "decision_trace": detached(self.trace),
            "numeric_policy_manifest": getattr(
                self.advisor, "manifest", {"status": "test_stub", "algorithm": "not_trained"}
            ),
            "numeric_windows_visited": sorted(self.visited_windows),
            "numeric_windows_missed": sorted(self.missed_windows),
            "continuation_state": {
                "dialogue": detached(self.dialogue),
                "round": self.round,
                "active_advice": detached(self.active_advice),
                "phase": self.phase,
                "used_windows": sorted(self.used_windows),
                "repeats": dict(self.repeats),
                "idle": self.idle,
                "seen_evidence": sorted(self.seen_evidence),
                "window_cache": detached(self.window_cache),
            },
            "boundary": "only public contexts and actual results; no Runtime passed to these services",
            "stop_reason": self.stop_reason,
        }
