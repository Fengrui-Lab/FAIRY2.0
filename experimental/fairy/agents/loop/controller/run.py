"""Shared execution loop; privileged and public decision paths are explicit."""

from __future__ import annotations
from typing import Callable, Protocol, TYPE_CHECKING, cast

if TYPE_CHECKING:
    from fairy.acps.ontology.contracts import OperationRequest, OperationResult
    from fairy.agents.loop.context.public import PublicContext
    from fairy.agents.models.decision import PrivilegedAgent, PublicAgent
    from fairy.events.session import Runtime
from fairy.acps.ontology.contracts import JsonObject
from fairy.events.records import HistoryTurn
from fairy.agents.loop.context.public import build_context
from fairy.agents.loop.controller.strategies import is_privileged
from fairy.agents.models.decision import PublicAgent, PrivilegedAgent
from evaluate.metrics import evaluate
from fairy.agents.loop.context.history import public_history, progress_diagnostic


class ContextExtension(Protocol):
    """公开上下文扩展；仅处理建议/检查，不能取得Runtime或暗中执行农事。"""

    stop_reason: str | None

    def augment(self, context: PublicContext) -> PublicContext: ...
    def intercept(self, request: OperationRequest, context: PublicContext) -> bool: ...
    def observe(
        self,
        request: OperationRequest,
        result: OperationResult,
        turn: HistoryTurn,
        context: PublicContext,
    ) -> None: ...
    def snapshot(self) -> JsonObject: ...


def run_agent(
    runtime: Runtime,
    agent: PublicAgent | PrivilegedAgent,
    max_steps: int = 12,
    max_errors: int = 3,
    max_context_chars: int = 12000,
    on_step: Callable[[Runtime, OperationRequest, OperationResult], None] | None = None,
    wall_timeout: float | None = None,
    max_history_turns: int = 8,
    max_protocol_errors: int = 3,
    context_extension: ContextExtension | None = None,
) -> Runtime:
    """执行共享决策循环。

    公开Agent只收到PublicContext，特权Oracle才收到Runtime；所有真实操作经invoke，
    局部服务、模型协议失败和工具拒绝分别计数；终止/恢复元数据始终保存。"""
    for name, value in (
        ("max_steps", max_steps),
        ("max_errors", max_errors),
        ("max_history_turns", max_history_turns),
        ("max_context_chars", max_context_chars),
        ("max_protocol_errors", max_protocol_errors),
    ):
        if type(value) is not int or value < 1:
            raise ValueError(f"{name} must be a positive integer")
    if wall_timeout is not None:
        from fairy.acps.ontology.farm.spec import finite

        finite(wall_timeout, "wall_timeout", 0.001)
    if hasattr(agent, "metadata"):
        agent.metadata = {
            **agent.metadata,
            "execution": {
                "max_steps": max_steps,
                "max_errors": max_errors,
                "max_context_chars": max_context_chars,
                "max_history_turns": max_history_turns,
                "wall_timeout": wall_timeout,
                "max_protocol_errors": max_protocol_errors,
                "public_progress": "public_history_v2",
            },
        }
    from time import monotonic

    started = monotonic()
    history = public_history(runtime)
    metadata = getattr(agent, "metadata", {})
    protocol_errors = metadata.get("prior_usage", {}).get("protocol_errors", 0) + metadata.get(
        "protocol_errors", 0
    )
    protocol_feedback = metadata.get("protocol_feedback")
    # The privilege is attached to an explicit trusted strategy class, not model output.
    privileged = is_privileged(agent)
    if privileged and context_extension is not None:
        raise ValueError("Public agent services cannot wrap a privileged strategy")
    actor = "oracle" if privileged else "agent"
    try:
        for _ in range(max_steps):
            if wall_timeout is not None and monotonic() - started >= wall_timeout:
                runtime.finish("wall_timeout")
                break
            if runtime.done:
                break
            if evaluate(runtime)["task_success"]:
                runtime.finish("task_success")
                break
            if context_extension is not None and context_extension.stop_reason:
                runtime.finish(context_extension.stop_reason)
                break
            if runtime.tool_errors >= max_errors:
                runtime.finish("tool_error_budget")
                break
            if protocol_errors >= max_protocol_errors:
                runtime.finish("protocol_error_budget")
                break
            base_context = (
                None
                if privileged
                else build_context(
                    runtime,
                    history,
                    max_chars=max_context_chars,
                    max_turns=max_history_turns,
                    protocol_feedback=protocol_feedback,
                )
            )
            if privileged:
                decision = cast(PrivilegedAgent, agent).decide(runtime)
            else:
                assert base_context is not None
                context = (
                    context_extension.augment(base_context)
                    if context_extension is not None
                    else base_context
                )
                decision = cast(PublicAgent, agent).decide(context)
            if decision.protocol_error:
                protocol_errors += 1
                protocol_feedback = decision.protocol_error
                if hasattr(agent, "metadata"):
                    agent.metadata["protocol_feedback"] = protocol_feedback
                if protocol_errors >= max_protocol_errors:
                    runtime.finish("protocol_error_budget")
                    break
                continue
            protocol_feedback = None
            if hasattr(agent, "metadata"):
                agent.metadata["protocol_feedback"] = None
            if decision.request is None:
                runtime.finish(decision.finish_reason)
                break
            if context_extension is not None:
                assert base_context is not None
                if context_extension.intercept(decision.request, base_context):
                    continue
            result = runtime.invoke(decision.request, actor=actor)
            history = public_history(runtime)
            if context_extension is not None:
                assert base_context is not None
                context_extension.observe(decision.request, result, history[-1], base_context)
            if hasattr(agent, "metadata"):
                agent.metadata["progress_diagnostic"] = progress_diagnostic(history, runtime.now)
            if on_step is not None:
                on_step(runtime, decision.request, result)
            if evaluate(runtime)["task_success"]:
                runtime.finish("task_success")
                break
            if runtime.tool_errors >= max_errors:
                runtime.finish("tool_error_budget")
                break
        else:
            runtime.finish("step_budget")
    except KeyboardInterrupt:
        if not runtime.done:
            runtime.suspend()
        raise
    except Exception:
        runtime.finish("execution_error")
        raise
    finally:
        if context_extension is not None and hasattr(agent, "metadata"):
            agent.metadata["autogen"] = context_extension.snapshot()
    return runtime
