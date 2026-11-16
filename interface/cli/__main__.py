"""python -m interface.cli {run,replay,evaluate,inspect,resume}."""
from __future__ import annotations
import argparse
import json
import sys
from fairy.events.artifacts import artifact, load_json, replay, write_json
from fairy.events.session import Runtime
from evaluate.metrics import evaluate
from evaluate.scenarios.registry import SCENARIOS


def _agent_arguments(command):
    command.add_argument("--agent", choices=("oracle", "llm"), default="oracle")
    command.add_argument("--model")
    command.add_argument("--base-url")
    command.add_argument("--max-steps", type=int, default=None)
    command.add_argument("--max-output-tokens", type=int, default=1024)
    command.add_argument("--disable-thinking", action="store_true")
    command.add_argument("--request-timeout", type=float, default=120.0)
    command.add_argument("--wall-timeout", type=float, default=3600.0)
    command.add_argument("--max-errors", type=int, default=10)
    command.add_argument("--history-turns", type=int, default=None)
    command.add_argument("--context-chars", type=int, default=60000)


def main(argv=None):
    parser = argparse.ArgumentParser(description="FAIRY shared L1/L3 runtime")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run")
    run.add_argument("--scenario", choices=tuple(SCENARIOS), default="soybean_l1_irrigation_v1")
    run.add_argument("--backend", choices=("native", "wofost"), default="native")
    _agent_arguments(run)
    run.add_argument("--out", default=".runs/native_oracle.json")
    for name in ("replay", "evaluate", "inspect"):
        command = commands.add_parser(name)
        command.add_argument("path")
    resume = commands.add_parser("resume", help="Restore without model calls, then continue with the selected agent")
    resume.add_argument("path")
    resume.add_argument("--out", required=True)
    _agent_arguments(resume)
    args = parser.parse_args(argv)
    try:
        if args.command in ("run", "resume"):
            from fairy.agents.loop.controller.run import run_agent
            from fairy.agents.loop.controller.strategies import oracle_for
            if args.command == "resume":
                source_record = load_json(args.path)
                runtime = Runtime.from_state(source_record["state"])
                if runtime.done:
                    raise ValueError("Cannot resume a terminated run")
            else:
                runtime = Runtime(SCENARIOS[args.scenario](), args.backend)
            if args.agent == "oracle":
                agent = oracle_for(runtime.scenario)
            else:
                from fairy.agents.models.llm.adapter import LLMAdapter
                agent = LLMAdapter(model=args.model, base_url=args.base_url,
                    max_output_tokens=args.max_output_tokens, thinking=False if args.disable_thinking else None,
                    timeout=args.request_timeout)
            if args.command == "resume":
                from fairy.events.records import fingerprint
                agent.metadata={**agent.metadata,"resume":{
                    "source_artifact_fingerprint":fingerprint(source_record),
                    "source_adapter":source_record.get("agent",{}).get("adapter"),
                    "through_event_seq":len(runtime.events)}}
            from pathlib import Path
            progress_path=Path(args.out).with_suffix(".progress.jsonl")
            progress_path.parent.mkdir(parents=True,exist_ok=True)
            progress_path.write_text("")
            def progress(r,request,result):
                with progress_path.open("a") as stream:
                    stream.write(json.dumps({"time":r.now.isoformat(),"tool":request.tool,
                        "ok":result.ok,"error":result.error,"event_seq":len(r.events)})+"\n")
            try:
                run_agent(runtime, agent, max_steps=args.max_steps if args.max_steps is not None else (400 if runtime.scenario.season else 12),
                          max_errors=getattr(args, "max_errors", 10), max_context_chars=getattr(args, "context_chars", 60000),
                          on_step=progress, wall_timeout=getattr(args,"wall_timeout",3600.0),
                          max_history_turns=args.history_turns if args.history_turns is not None else (20 if runtime.scenario.season else 8))
            finally:
                write_json(args.out, artifact(runtime, agent.metadata))
            print(json.dumps(evaluate(runtime), ensure_ascii=False, indent=2))
            return 0 if evaluate(runtime)["task_success"] else 1
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
    except Exception as exc:
        # Error details remain local; no credential-bearing API response is printed.
        if type(exc).__module__.startswith("openai"):
            print(f"LLM request failed ({type(exc).__name__}); runtime artifact saved if execution started.", file=sys.stderr)
        else:
            print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
