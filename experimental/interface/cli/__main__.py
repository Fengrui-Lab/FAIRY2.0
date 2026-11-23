"""python -m interface.cli {run,replay,evaluate,inspect,resume}."""

from __future__ import annotations
from typing import Iterator, Sequence, TYPE_CHECKING

if TYPE_CHECKING:
    from fairy.acps.ontology.contracts import OperationRequest, OperationResult
    from fairy.agents.models.decision import PrivilegedAgent, PublicAgent
import argparse
import json
import sys
from pathlib import Path
from uuid import uuid4
from contextlib import contextmanager
from fairy.events.artifacts import artifact, load_json, replay, write_json
from fairy.events.session import Runtime
from evaluate.metrics import evaluate
from evaluate.scenarios.registry import SCENARIOS


def _agent_arguments(command: argparse.ArgumentParser) -> None:
    """声明共有模型预算和配置项，不读取或发送凭证。"""
    command.add_argument("--agent", choices=("oracle", "llm"), default="oracle")
    command.add_argument("--model")
    command.add_argument("--base-url")
    command.add_argument("--max-steps", type=int, default=None)
    command.add_argument("--max-output-tokens", type=int, default=1024)
    command.add_argument("--disable-thinking", action="store_true")
    command.add_argument("--request-timeout", type=float, default=120.0)
    command.add_argument("--wall-timeout", type=float, default=3600.0)
    command.add_argument("--max-errors", type=int, default=10)
    command.add_argument("--max-protocol-errors", type=int, default=3)
    command.add_argument("--history-turns", type=int, default=None)
    command.add_argument("--context-chars", type=int, default=60000)
    command.add_argument(
        "--fixed-irrigation-dose-mm",
        type=float,
        help="WOFOST soybean LLM integration control: request a target; a fixed policy supplies the dose",
    )


@contextmanager
def reserve_output(path: str | Path) -> Iterator[Path]:
    """在构建引擎/调用模型前预留全新输出，退出时释放本次锁。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    progress = path.with_suffix(".progress.jsonl")
    lock = path.with_name(path.name + ".lock")
    # Cooperating runs reserve an attempt before initializing an engine or model.
    with lock.open("x", encoding="utf-8") as stream:
        stream.write("FAIRY output reservation\n")
    try:
        if path.exists() or progress.exists():
            raise FileExistsError("Artifact or progress path already exists; choose a new --out")
        yield progress
    finally:
        lock.unlink(missing_ok=True)


def execute(args: argparse.Namespace, progress_path: Path) -> int:
    """在预留输出中运行/恢复策略，共享Runtime和controller，始终保存可审查产物。"""
    from fairy.agents.loop.controller.run import run_agent
    from fairy.agents.loop.controller.strategies import oracle_for

    if args.command == "resume":
        source_record = load_json(args.path)
        runtime = Runtime.from_state(source_record["state"])
        if runtime.done:
            raise ValueError("Cannot resume a terminated run")
    else:
        runtime = Runtime(SCENARIOS[args.scenario](), args.backend)
    previous = source_record.get("agent", {}) if args.command == "resume" else {}
    prior_dosing = previous.get("irrigation_dosing")
    irrigation_dosing = None
    if args.fixed_irrigation_dose_mm is not None:
        from fairy.agents.loop.context.water import soil_profile
        from fairy.agents.loop.controller.irrigation_dose import IrrigationDosing
        from fairy.agents.models.numeric.dose import FixedIrrigationDose

        season = runtime.scenario.season
        if (
            args.agent != "llm"
            or runtime.backend_name != "wofost"
            or season is None
            or season.cultivar != "Soybean_901_proxy"
            or "irrigate_range" not in runtime.scenario.tools
        ):
            raise ValueError("Fixed dosing requires a WOFOST Soybean_901 full-season LLM run")
        policy = FixedIrrigationDose(args.fixed_irrigation_dose_mm)
        irrigation_dosing = IrrigationDosing(policy, soil_profile(runtime.scenario))
        if prior_dosing is not None:
            if prior_dosing["policy"] != policy.identity:
                raise ValueError("Resume the run with the same dose policy")
            irrigation_dosing.attempts.extend(prior_dosing["attempts"])
            irrigation_dosing.feedback = prior_dosing.get("feedback")
    elif prior_dosing is not None:
        raise ValueError("Resume this run with its original --fixed-irrigation-dose-mm")
    agent: PublicAgent | PrivilegedAgent
    if args.agent == "oracle":
        agent = oracle_for(runtime.scenario)
    else:
        from fairy.agents.models.llm.adapter import LLMAdapter

        agent = LLMAdapter(
            model=args.model,
            base_url=args.base_url,
            max_output_tokens=args.max_output_tokens,
            thinking=False if args.disable_thinking else None,
            timeout=args.request_timeout,
        )
    agent.metadata = {**agent.metadata, "attempt_id": uuid4().hex}
    if args.command == "resume":
        from fairy.events.records import fingerprint

        prior = previous.get("prior_usage", {})
        agent.metadata.update(
            resume={
                "source_artifact_fingerprint": fingerprint(source_record),
                "source_adapter": previous.get("adapter"),
                "through_event_seq": len(runtime.events),
                "context_mode": "rebuild_public_receipts",
            },
            prior_usage={
                key: prior.get(key, 0) + previous.get(key, 0)
                for key in ("calls", "input_tokens", "output_tokens", "protocol_errors")
            },
        )
        agent.metadata["protocol_feedback"] = previous.get("protocol_feedback")
    with progress_path.open("x", encoding="utf-8"):
        pass

    def progress(r: Runtime, request: OperationRequest, result: OperationResult) -> None:
        with progress_path.open("a", encoding="utf-8") as stream:
            stream.write(
                json.dumps(
                    {
                        "time": r.now.isoformat(),
                        "tool": request.tool,
                        "ok": result.ok,
                        "error": result.error,
                        "event_seq": len(r.events),
                    }
                )
                + "\n"
            )

    try:
        run_agent(
            runtime,
            agent,
            max_steps=(
                args.max_steps
                if args.max_steps is not None
                else (400 if runtime.scenario.season else 12)
            ),
            max_errors=args.max_errors,
            max_protocol_errors=args.max_protocol_errors,
            max_context_chars=args.context_chars,
            on_step=progress,
            wall_timeout=args.wall_timeout,
            irrigation_dosing=irrigation_dosing,
            max_history_turns=(
                args.history_turns
                if args.history_turns is not None
                else (20 if runtime.scenario.season else 8)
            ),
        )
    finally:
        write_json(args.out, artifact(runtime, agent.metadata))
        args.artifact_saved = True
    print(json.dumps({"artifact": args.out, **evaluate(runtime)}, ensure_ascii=False, indent=2))
    return 0 if evaluate(runtime)["task_success"] else 1


def main(argv: Sequence[str] | None = None) -> int:
    """解析CLI参数并分派运行/查看/验证入口，以返回码报告结果。"""
    parser = argparse.ArgumentParser(description="FAIRY shared L1/L3 runtime")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run")
    run.add_argument("--scenario", choices=tuple(SCENARIOS), default="soybean_l1_irrigation_v1")
    run.add_argument("--backend", choices=("native", "wofost"), default="native")
    _agent_arguments(run)
    run.add_argument("--out", help="New artifact path; existing artifacts are never overwritten")
    for name in ("replay", "evaluate", "inspect"):
        command = commands.add_parser(name)
        command.add_argument("path")
    resume = commands.add_parser(
        "resume", help="Restore without model calls, then continue with the selected agent"
    )
    resume.add_argument("path")
    resume.add_argument("--out", required=True)
    _agent_arguments(resume)
    args = parser.parse_args(argv)
    try:
        if args.command in ("run", "resume"):
            args.out = args.out or str(Path(".runs") / ("run-" + uuid4().hex + ".json"))
            with reserve_output(args.out) as progress_path:
                return execute(args, progress_path)
        record = load_json(args.path)
        if args.command == "inspect":
            # Inspection is pure JSON reading: no backend imports/construction.
            print(json.dumps(record, ensure_ascii=False, indent=2))
            return 0
        if args.command == "replay":
            _, metrics = replay(record)
        else:
            metrics = evaluate(Runtime.from_state(record["state"]))
        print(json.dumps(metrics, ensure_ascii=False, indent=2))
        return 0
    except KeyboardInterrupt:
        print(
            (
                "Interrupted; a consistent resumable artifact was saved."
                if getattr(args, "artifact_saved", False)
                else "Interrupted before an executable state could be saved."
            ),
            file=sys.stderr,
        )
        return 130
    except Exception as exc:
        # Error details remain local; no credential-bearing API response is printed.
        if type(exc).__module__.startswith("openai"):
            print(
                f"LLM request failed ({type(exc).__name__}); runtime artifact saved if execution started.",
                file=sys.stderr,
            )
        else:
            print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
