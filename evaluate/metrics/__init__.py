"""Explicit task evaluation dispatch shared by CLI, artifacts and replay."""

def evaluate(runtime,replay_verified=None):
    if runtime.scenario.task_type=="irrigation":
        from .irrigation import evaluate as evaluator
    elif runtime.scenario.task_type=="full_season":
        from .season import evaluate as evaluator
    else:
        raise ValueError("No evaluator registered for this task")
    return evaluator(runtime,replay_verified)
