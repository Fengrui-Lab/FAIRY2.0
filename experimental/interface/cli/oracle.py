"""python -m interface.cli.oracle {audit,extract,train,evaluate,generate,induce,qualify}."""

from __future__ import annotations
from typing import Sequence, TYPE_CHECKING, cast

if TYPE_CHECKING:
    from fairy.acps.ontology.contracts import JsonObject
    from fairy.agents.models.llm.adapter import ChatClient
from pathlib import Path
import argparse
import json
import os
import sys
from fairy.events.artifacts import artifact, write_json, load_json
from fairy.events.session import Runtime
from fairy.agents.models.llm.adapter import ChatClient
from fairy.events.provenance import source_identity
from fairy.agents.loop.context.public import build_context
from evaluate.scenarios.registry import SCENARIOS


def client(args: argparse.Namespace) -> ChatClient:
    """按显式配置创建可选模型客户端；服务凭证只从本地环境读取。"""
    from openai import OpenAI

    key = os.getenv("FAIRY_LLM_API_KEY") or os.getenv("OPENAI_API_KEY")
    if not key:
        raise ValueError("Set FAIRY_LLM_API_KEY or OPENAI_API_KEY")
    return cast(
        ChatClient,
        OpenAI(
            api_key=key,
            base_url=args.base_url or os.getenv("FAIRY_LLM_BASE_URL"),
            timeout=args.request_timeout,
            max_retries=0,
        ),
    )


def model_args(command: argparse.ArgumentParser) -> None:
    """声明模型服务的公共CLI配置项。"""
    command.add_argument("--model", default=os.getenv("FAIRY_LLM_MODEL"))
    command.add_argument("--base-url", default=os.getenv("FAIRY_LLM_BASE_URL"))
    command.add_argument("--request-timeout", type=float, default=240)


def generate(args: argparse.Namespace) -> int:
    """运行或续跑公开LLM候选，固定知识/经验/权重，保存轨迹并独立资格审查。"""
    from fairy.agents.models.llm.adapter import LLMAdapter
    from fairy.agents.models.numeric.irrigation import PPOAdvisor, ZeroBaseline
    from fairy.agents.loop.context.kg.lifecycle import load_knowledge, validate_knowledge
    from fairy.agents.loop.context.history import public_history
    from fairy.events.records import fingerprint
    from fairy.agents.loop.controller.autogen.extension import AutogenExtension
    from fairy.agents.loop.controller.autogen.memory import MemorySnapshot
    from fairy.agents.loop.controller.run import run_agent
    from evaluate.oracle.qualification import qualify

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=False)
    previous = load_json(args.path) if args.command == "resume" else None
    runtime = (
        Runtime.from_state(previous["state"])
        if previous
        else Runtime(SCENARIOS[args.scenario](), args.backend)
    )
    if runtime.done:
        raise ValueError("Cannot resume a terminated candidate")
    if not runtime.scenario.season:
        raise ValueError("Autogen requires full-season scenario")
    if runtime.scenario.scenario_id not in (
        "soybean_l3_hn58_normal_v1",
        "soybean_l3_901_proxy_normal_v1",
        "soybean_l3_901_proxy_calendar_drought_v1",
        "soybean_l3_901_proxy_reproductive_drought_v1",
    ):
        raise ValueError("Unsupported numerical-adviser scenario; no fallback")
    context = build_context(runtime, public_history(runtime), max_chars=60000, max_turns=4)
    old = previous["agent"]["autogen"] if previous else None
    knowledge = (
        validate_knowledge(old["knowledge"], context)
        if old and not args.knowledge
        else load_knowledge(args.knowledge, context)
    )
    if args.numeric == "ppo" and not args.policy:
        raise ValueError("PPO generation requires --policy")
    if args.numeric == "zero" and args.policy:
        raise ValueError("Zero baseline does not load PPO weights")
    policy = (
        PPOAdvisor(args.policy, expected_source=runtime.source_identity["fingerprint"])
        if args.numeric == "ppo"
        else ZeroBaseline()
    )
    if args.numeric == "ppo" and runtime.export_state()[
        "scenario_fingerprint"
    ] not in policy.manifest.get(
        "qualification_scenario_fingerprints",
        [policy.manifest.get("qualification_scenario_fingerprint")],
    ):
        raise ValueError("Policy scenario scope mismatch")
    memory = (
        MemorySnapshot(rows=old["memory_snapshot"])
        if old and not args.memory
        else MemorySnapshot(args.memory)
    )
    prior = (
        {
            key: previous["agent"].get("prior_usage", {}).get(key, 0)
            + previous["agent"].get(key, 0)
            for key in ("calls", "input_tokens", "output_tokens", "protocol_errors")
        }
        if previous
        else {}
    )
    remaining = args.max_steps - prior.get("calls", 0)
    if remaining < 1:
        raise ValueError("Cumulative model decision budget exhausted")
    write_json(
        out / "inputs.json",
        {
            "source": runtime.source_identity,
            "scenario": runtime.scenario.to_dict(),
            "knowledge": knowledge,
            "policy": policy.manifest,
            "memory_snapshot": memory.rows,
            "memory_fingerprint": memory.fingerprint,
            "resume_source_fingerprint": fingerprint(previous) if previous else None,
            "budget": {
                "total_max_steps": args.max_steps,
                "remaining_steps": remaining,
                "max_errors": 10,
                "max_protocol_errors": 3,
                "wall_timeout": args.wall_timeout,
            },
        },
    )
    progress = out / "decisions.jsonl"

    def on_decision(row: JsonObject) -> None:
        with progress.open("a") as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")

    extension = AutogenExtension(
        knowledge, policy, memory=memory, on_decision=on_decision, state=old
    )
    agent = LLMAdapter(
        model=args.model,
        base_url=args.base_url,
        max_output_tokens=1024,
        thinking=False,
        timeout=args.request_timeout,
    )
    if previous:
        agent.metadata.update(
            prior_usage=prior,
            protocol_feedback=previous["agent"].get("protocol_feedback"),
            resume={
                "source_artifact_fingerprint": fingerprint(previous),
                "context_mode": "public_receipts_and_pinned_agent_services",
            },
        )
    try:
        run_agent(
            runtime,
            agent,
            max_steps=remaining,
            max_errors=10,
            max_protocol_errors=3,
            max_history_turns=4,
            max_context_chars=60000,
            wall_timeout=args.wall_timeout,
            context_extension=extension,
        )
    finally:
        record = artifact(runtime, agent.metadata)
        write_json(out / "run.json", record)
    report = qualify(record)
    write_json(out / "qualification.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["reference_eligible"] else 1


def main(argv: Sequence[str] | None = None) -> int:
    """解析CLI参数并分派运行/查看/验证入口，以返回码报告结果。"""
    p = argparse.ArgumentParser(
        description="Public-evidence automatic Oracle MVP on the rebuilt FAIRY runtime"
    )
    commands = p.add_subparsers(dest="command", required=True)
    audit = commands.add_parser("audit")
    audit.add_argument("--out", required=True)
    extract = commands.add_parser("extract")
    extract.add_argument("--out", required=True)
    model_args(extract)
    extract.add_argument(
        "--include-source-code",
        action="store_true",
        help="Explicitly send the audited local source bundle; default sends public contracts and source identities only",
    )
    train_parser = commands.add_parser("train")
    train_parser.add_argument("--out", required=True)
    train_parser.add_argument("--timesteps", type=int, default=2048)
    train_parser.add_argument("--seed", type=int, default=42)
    train_parser.add_argument("--backend", choices=("native", "wofost"), default="native")
    evaluate = commands.add_parser("evaluate")
    evaluate.add_argument("--policy", required=True)
    evaluate.add_argument("--out", required=True)
    for name in ("generate", "resume"):
        gen = commands.add_parser(name)
        if name == "generate":
            gen.add_argument(
                "--scenario",
                choices=(
                    "soybean_l3_hn58_normal_v1",
                    "soybean_l3_901_proxy_normal_v1",
                    "soybean_l3_901_proxy_calendar_drought_v1",
                    "soybean_l3_901_proxy_reproductive_drought_v1",
                ),
                default="soybean_l3_hn58_normal_v1",
            )
            gen.add_argument("--backend", choices=("native", "wofost"), default="native")
        else:
            gen.add_argument("path")
        gen.add_argument("--policy")
        gen.add_argument("--numeric", choices=("ppo", "zero"), default="ppo")
        gen.add_argument("--knowledge")
        gen.add_argument("--memory")
        gen.add_argument("--out", required=True)
        gen.add_argument("--max-steps", type=int, default=800)
        gen.add_argument("--wall-timeout", type=float, default=10800)
        model_args(gen)
    induce_parser = commands.add_parser("induce")
    induce_parser.add_argument("path")
    induce_parser.add_argument("--memory", required=True)
    induce_parser.add_argument("--llm-condense", action="store_true")
    induce_parser.add_argument("--out")
    model_args(induce_parser)
    qualify_parser = commands.add_parser("qualify")
    qualify_parser.add_argument("paths", nargs="+")
    qualify_parser.add_argument("--out", required=True)
    for command in (audit, extract):
        command.add_argument("--backend", choices=("native", "wofost"), default="native")
        command.add_argument(
            "--scenario",
            choices=("soybean_l3_hn58_normal_v1", "soybean_l3_901_proxy_normal_v1"),
            default="soybean_l3_hn58_normal_v1",
        )
    args = p.parse_args(argv)
    if args.command in ("extract", "audit"):
        from fairy.agents.loop.context.kg.lifecycle import (
            source_bundle,
            seed_knowledge,
            extract as extract_knowledge,
        )

        runtime = Runtime(SCENARIOS[args.scenario](), args.backend)
        context = build_context(runtime, [], max_chars=60000, max_turns=4)
        if args.command == "audit":
            write_json(
                args.out,
                {
                    "source": source_identity(),
                    "scenario": runtime.scenario.to_dict(),
                    "knowledge": seed_knowledge(context),
                    "extraction_bundle": source_bundle(context),
                    "scope": f"{runtime.backend_name} {runtime.season_config.cultivar}; local audited processes only",
                },
            )
        else:
            if not args.model:
                raise ValueError("Model is required for real offline extraction")
            value = extract_knowledge(
                client(args),
                args.model,
                context,
                args.out,
                include_source_code=args.include_source_code,
            )
            print(json.dumps(value, indent=2))
    elif args.command == "train":
        from fairy.agents.models.numeric.training import train

        print(
            json.dumps(
                train(args.out, timesteps=args.timesteps, seed=args.seed, backend=args.backend),
                indent=2,
            )
        )
    elif args.command == "evaluate":
        from fairy.agents.models.numeric.training import evaluate_policy

        print(json.dumps(evaluate_policy(args.policy, args.out), indent=2))
    elif args.command in ("generate", "resume"):
        return generate(args)
    elif args.command == "induce":
        from fairy.agents.loop.controller.autogen.memory import induce, append, condense

        rows = induce(load_json(args.path))
        if args.llm_condense:
            if not args.out or not args.model:
                raise ValueError("LLM induction requires --out audit and --model")
            rows = condense(rows, client(args), args.model, args.out)
        print(json.dumps({"candidate_records": len(rows), "appended": append(args.memory, rows)}))
    else:
        from evaluate.oracle.qualification import qualify, select

        reports = {path: qualify(load_json(path)) for path in args.paths}
        write_json(args.out, {"qualification": reports, "selection": select(reports)})
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:
        # Never print a potentially credential-bearing service exception body.
        print("Oracle command failed: " + type(exc).__name__, file=sys.stderr)
        raise SystemExit(2)
