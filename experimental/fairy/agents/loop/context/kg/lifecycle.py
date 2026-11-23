"""Versioned lifecycle knowledge with closed schema and audited source identities."""

from __future__ import annotations
from typing import Any, TYPE_CHECKING, cast

if TYPE_CHECKING:
    from fairy.acps.ontology.contracts import JsonObject
    from fairy.agents.loop.context.public import PublicContext
    from fairy.agents.models.llm.adapter import ChatClient
from importlib.resources import files
from pathlib import Path
import hashlib
import json
import re
from fairy.events.records import fingerprint, detached

PHASES = ("preparation", "establishment", "growth_management", "harvest", "postharvest")
REQUIRED_OBSERVATIONS = {
    "preparation": ("observe_soil_range", "get_current_weather", "inspect_inventory"),
    "establishment": ("inspect_crop", "observe_soil_range"),
    "growth_management": (
        "inspect_crop",
        "observe_soil_range",
        "get_current_weather",
        "get_forecast",
    ),
    "harvest": ("inspect_crop", "observe_soil_range", "get_current_weather", "inspect_inventory"),
    "postharvest": ("inspect_inventory",),
}
SOURCE_PATHS = {
    "manual_reference": "agents/loop/controller/season_oracle.py",
    "operations": "acps/ontology/equipment/operations.py",
    "facilities": "acps/ontology/infrastructure/dryer.py",
    "tractor": "acps/ontology/equipment/tractor.py",
    "combine": "acps/ontology/equipment/combine.py",
    "crop_sensor": "acps/ontology/infrastructure/crop_sensor.py",
    "irrigation": "acps/ontology/infrastructure/irrigation.py",
    "grain_warehouse": "acps/ontology/infrastructure/grain_warehouse.py",
    "biology": "acps/backends/biology/native/season.py",
    "physical": "acps/backends/physical/native/backend.py",
    "validator": None,
}


def source_bundle(context: PublicContext) -> JsonObject:
    """整理离线知识提取来源及公开契约；源码发送取决于显式提取选项。"""
    sources = {}
    for key, path in SOURCE_PATHS.items():
        if key == "biology" and context["constraints"].get("backend") == "wofost":
            path = "acps/backends/biology/wofost/season.py"
        node = (
            files("fairy").joinpath(path)
            if path
            else files("evaluate").joinpath("metrics/season.py")
        )
        body = node.read_text()
        sources[key] = {
            "path": ("fairy/" + path) if path else "evaluate/metrics/season.py",
            "sha256": hashlib.sha256(body.encode()).hexdigest(),
            "text": body,
        }
    return {
        "sources": sources,
        "tools": context["tools"],
        "task_constraints": context["constraints"],
        "notice": "Manual reference is coverage evidence, not an optimal policy. Never copy its numeric doses, thresholds or fixed tool order as compulsory agronomy.",
    }


def validate_knowledge(
    value: Any, context: PublicContext, *, check_sources: bool = True
) -> JsonObject:
    """验证知识闭合Schema、工具引用及适用来源；版本不符时拒绝。"""
    required = {"schema", "crop", "cultivar", "phases", "sources", "contract_fingerprint", "status"}
    if not isinstance(value, dict) or set(value) != required or value["schema"] != 1:
        raise ValueError("Invalid lifecycle schema")
    if (
        value["crop"] != "soybean"
        or value["cultivar"] != context["constraints"]["cultivar"]
        or value["status"] != "machine_checked"
    ):
        raise ValueError("Unsupported lifecycle scope or review status")
    if value["contract_fingerprint"] != fingerprint(context["tools"]):
        raise ValueError("Lifecycle/tool contract mismatch")
    if not isinstance(value["phases"], list) or [p.get("id") for p in value["phases"]] != list(
        PHASES
    ):
        raise ValueError("Lifecycle must cover the five ordered management phases")
    tools = {t["function"]["name"] for t in context["tools"]}
    for phase in value["phases"]:
        if set(phase) != {"id", "goal", "observations", "operations", "completion", "source_ids"}:
            raise ValueError("Unknown lifecycle phase field")
        if (
            not isinstance(phase["goal"], str)
            or not phase["goal"].strip()
            or len(phase["goal"]) > 600
            or re.search(r"(?<![A-Za-z0-9_])(?:\d|\.\d)", phase["goal"])
        ):
            raise ValueError("Lifecycle goals must not encode doses or dated schedules")
        if phase["completion"] != phase["id"] + "_verified":
            raise ValueError("Unknown evidence predicate")
        for field in ("observations", "operations", "source_ids"):
            if (
                not isinstance(phase[field], list)
                or not phase[field]
                or len(set(phase[field])) != len(phase[field])
            ):
                raise ValueError("Invalid lifecycle lists")
        if not set(phase["observations"] + phase["operations"]) <= tools:
            raise ValueError("Lifecycle references unknown tools")
        if not set(REQUIRED_OBSERVATIONS[phase["id"]]) <= set(phase["observations"]):
            raise ValueError("Lifecycle omits evidence required by its public predicates")
        if not set(phase["source_ids"]) <= set(value["sources"]):
            raise ValueError("Missing source evidence")
    if check_sources:
        actual = source_bundle(context)["sources"]
        expected = {
            key: {k: v for k, v in row.items() if k != "text"} for key, row in actual.items()
        }
        if value["sources"] != expected:
            raise ValueError("Lifecycle source fingerprints changed")
    return detached(value)


def seed_knowledge(context: PublicContext) -> JsonObject:
    """生成程序定义的生命周期基架，不伪称真实模型提取或执行成果。"""
    rows = [
        (
            "preparation",
            "Check soil, current weather and inventory, then prepare the target field.",
            ["observe_soil_range", "get_current_weather", "inspect_inventory"],
            ["prepare_seedbed"],
        ),
        (
            "establishment",
            "Plant at the task geometry; check crop emergence using actual receipts and observations.",
            ["inspect_crop", "observe_soil_range", "get_current_weather", "inspect_inventory"],
            ["plant", "advance_time"],
        ),
        (
            "growth_management",
            "Maintain daily opportunities to inspect crop, soil and weather, request numerical advice, then choose whether to irrigate and recheck after settlement.",
            ["inspect_crop", "observe_soil_range", "get_current_weather", "get_forecast"],
            ["irrigate_range", "advance_time"],
        ),
        (
            "harvest",
            "Confirm observed harvestability and trafficability; harvest in feasible batches, inspect containers and unload when necessary.",
            ["inspect_crop", "observe_soil_range", "get_current_weather", "inspect_inventory"],
            ["harvest", "unload"],
        ),
        (
            "postharvest",
            "Inspect grain and containers; dry only when necessary, then safely store all recovered grain.",
            ["inspect_inventory"],
            ["unload", "dry_grain", "store_grain"],
        ),
    ]
    sources = {
        key: {k: v for k, v in row.items() if k != "text"}
        for key, row in source_bundle(context)["sources"].items()
    }
    return validate_knowledge(
        {
            "schema": 1,
            "crop": "soybean",
            "cultivar": context["constraints"]["cultivar"],
            "status": "machine_checked",
            "contract_fingerprint": fingerprint(context["tools"]),
            "sources": sources,
            "phases": [
                dict(
                    id=i,
                    goal=g,
                    observations=o,
                    operations=a,
                    completion=i + "_verified",
                    source_ids=list(sources),
                )
                for i, g, o, a in rows
            ],
        },
        context,
    )


def load_knowledge(path: str | Path | None, context: PublicContext) -> JsonObject:
    """加载匹配上下文的阶段知识并验证；不静默借用其他后端知识。"""
    import yaml

    if path is None and context["constraints"].get("backend") == "wofost":
        return seed_knowledge(context)
    text = (
        Path(path).read_text()
        if path
        else files("fairy.agents.loop.context.kg").joinpath("soybean_hn58.yaml").read_text()
    )
    return validate_knowledge(yaml.safe_load(text), context)


def extract(
    client: ChatClient,
    model: str,
    context: PublicContext,
    out: str | Path,
    *,
    thinking: bool | None = False,
    include_source_code: bool = False,
) -> JsonObject:
    """One offline model extraction; validated output, no executable plans or weather."""
    import yaml
    from fairy.events.artifacts import write_json

    destination = Path(out)
    if destination.exists() or destination.with_suffix(".provenance.json").exists():
        raise FileExistsError(destination)
    seed = seed_knowledge(context)
    bundle = source_bundle(context)
    # Show structure, not a completed reference answer that the model can merely copy.
    shape = {
        "phases": [
            {
                "id": phase,
                "goal": "<derive a general English goal from source evidence; no numeric literals>",
                "observations": ["<actual observation tool names>"],
                "operations": ["<actual work or time tool names>"],
                "source_ids": ["<supplied evidence source IDs>"],
            }
            for phase in PHASES
        ]
    }
    if not include_source_code:
        bundle["sources"] = {
            key: {k: v for k, v in row.items() if k != "text"}
            for key, row in bundle["sources"].items()
        }
    payload = dict(
        model=model,
        temperature=0.1,
        max_completion_tokens=4096,
        response_format={"type": "json_object"},
        extra_body={"chat_template_kwargs": {"enable_thinking": thinking}},
        messages=[
            {
                "role": "system",
                "content": "Extract reusable crop lifecycle knowledge, not an executable Oracle. Return ONLY a JSON object with the key phases and the five phase objects in the supplied order. Each phase has exactly id, goal, observations, operations, source_ids. The output_shape container is NOT an output key. Derive content from actual source evidence, without numerical literals, dated schedules, doses, compulsory tool order or optimality claims. Include every required observation for the relevant phase. Never reproduce hashes or paths: the program attaches verified provenance and fixed predicate IDs. Manual Oracle thresholds are not mandatory agronomy.",
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "output_shape": shape,
                        "required_observations": REQUIRED_OBSERVATIONS,
                        "evidence": bundle,
                    },
                    ensure_ascii=False,
                ),
            },
        ],
    )
    response = client.chat.completions.create(**payload)
    write_json(
        destination.with_suffix(".response.json"),
        {
            "request_fingerprint": fingerprint(payload),
            "model": response.model,
            "input_scope": (
                "local_source_code"
                if include_source_code
                else "public_contracts_and_provenance_only"
            ),
            "choices": [
                {"finish_reason": choice.finish_reason, "content": choice.message.content}
                for choice in response.choices
            ],
        },
    )
    if not response.choices or response.choices[0].finish_reason != "stop":
        raise ValueError("Incomplete lifecycle extraction")
    content = json.loads(cast(str, response.choices[0].message.content))
    if set(content) != {"phases"} or not isinstance(content["phases"], list):
        raise ValueError("Invalid extraction content schema")
    rows = []
    for row in content["phases"]:
        if set(row) != {"id", "goal", "observations", "operations", "source_ids"}:
            raise ValueError("Invalid extracted phase fields")
        rows.append({**row, "completion": row["id"] + "_verified"})
    value = validate_knowledge({**seed, "phases": rows}, context)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("x") as stream:
        yaml.safe_dump(value, stream, sort_keys=False, allow_unicode=True)
    write_json(
        destination.with_suffix(".provenance.json"),
        {
            "model": response.model,
            "request_fingerprint": fingerprint(payload),
            "knowledge_fingerprint": fingerprint(value),
            "status": "machine_checked_not_human_reviewed",
            "source_scope": "local rebuilt source only",
            "input_scope": (
                "local_source_code"
                if include_source_code
                else "public_contracts_and_provenance_only"
            ),
            "input_tokens": getattr(response.usage, "prompt_tokens", 0),
            "output_tokens": getattr(response.usage, "completion_tokens", 0),
        },
    )
    return value
