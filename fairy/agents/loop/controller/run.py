"""Shared execution loop; privileged and public decision paths are explicit."""
from dataclasses import asdict
from fairy.agents.loop.context.public import build_context
from fairy.agents.loop.controller.strategies import is_privileged
from evaluate.metrics import evaluate


def run_agent(runtime, agent, max_steps=12, max_errors=3, max_context_chars=12000, on_step=None, wall_timeout=None, max_history_turns=8):
    for name, value in (("max_steps", max_steps), ("max_errors", max_errors),
                        ("max_history_turns", max_history_turns), ("max_context_chars", max_context_chars)):
        if type(value) is not int or value < 1:
            raise ValueError(f"{name} must be a positive integer")
    if wall_timeout is not None:
        from fairy.acps.ontology.farm.spec import finite
        finite(wall_timeout, "wall_timeout", 0.001)
    if hasattr(agent,"metadata"):
        agent.metadata={**agent.metadata,"execution":{"max_steps":max_steps,"max_errors":max_errors,
            "max_context_chars":max_context_chars,"max_history_turns":max_history_turns,"wall_timeout":wall_timeout,
            "public_progress":"successful_tool_receipts_v1"}}
    from time import monotonic
    started=monotonic()
    history = []
    # The privilege is attached to an explicit trusted strategy class, not model output.
    privileged = is_privileged(agent)
    actor = "oracle" if privileged else "agent"
    try:
        for _ in range(max_steps):
            if wall_timeout is not None and monotonic()-started>=wall_timeout:
                runtime.finish("wall_timeout")
                break
            if runtime.done:
                break
            if evaluate(runtime)["task_success"]:
                runtime.finish("task_success")
                break
            if runtime.tool_errors >= max_errors:
                runtime.finish("tool_error_budget")
                break
            decision = agent.decide(runtime if privileged else build_context(runtime, history, max_chars=max_context_chars, max_turns=max_history_turns))
            if decision.request is None:
                runtime.finish(decision.finish_reason)
                break
            result = runtime.invoke(decision.request, actor=actor)
            history.append({"request": asdict(decision.request), "result": asdict(result)})
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
    except Exception:
        runtime.finish("execution_error")
        raise
    return runtime
