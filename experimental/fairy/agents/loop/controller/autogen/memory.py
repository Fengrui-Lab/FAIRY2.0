"""Append-only, evidence-linked workflow memory; no hidden weather or dose recipes."""

from __future__ import annotations
from typing import Sequence, TYPE_CHECKING, cast

if TYPE_CHECKING:
    from fairy.acps.ontology.contracts import JsonObject
    from fairy.agents.models.llm.adapter import ChatClient
from pathlib import Path
import json
import re
from fairy.events.records import fingerprint, detached


class MemorySnapshot:
    """一次运行内冻结的经验证工作流经验；运行结束后才由外部更新JSONL。"""

    rows: list[JsonObject]  #: 已验证、按内容身份去重的本次固定经验。
    fingerprint: str  #: 固定快照身份，续跑不可静默切换。

    def __init__(
        self, path: str | Path | None = None, *, rows: Sequence[JsonObject] | None = None
    ) -> None:
        """保存显式依赖与初态；模型服务调用发生在决策阶段而非构造阶段。"""
        self.rows = []
        if rows is not None:
            for row in rows:
                validate(row)
                self.rows.append(detached(row))
        if path and Path(path).exists():
            for line in Path(path).read_text().splitlines():
                if line.strip():
                    row = json.loads(line)
                    validate(row)
                    self.rows.append(row)
        self.fingerprint = fingerprint(self.rows)

    def retrieve(
        self, phase: str, *, limit: int = 2, profile: JsonObject | None = None
    ) -> list[JsonObject]:
        """按阶段与兼容profile检索有限成功/失败经验，保持运行内快照不变。"""
        found = []
        for kind in ("local_success", "failure_lesson"):
            rows = [
                r
                for r in self.rows
                if r["crop"] == "soybean"
                and r["phase"] == phase
                and r["kind"] == kind
                and r["verified"]
                and (profile is None or r["profile"] == profile)
            ]
            found.extend(rows[-limit:])
        return detached(found)


def validate(row: JsonObject) -> None:
    """检查字段或经验内容的契约要求；错误明确抛出，不自动修复输入。"""
    keys = {
        "id",
        "crop",
        "phase",
        "kind",
        "tool",
        "lesson",
        "verified",
        "source_artifact_fingerprint",
        "source_steps",
        "scope",
        "induction",
        "profile",
    }
    if (
        not isinstance(row, dict)
        or set(row) != keys
        or row["kind"] not in ("local_success", "failure_lesson")
    ):
        raise ValueError("Invalid experience schema")
    if row["crop"] != "soybean" or type(row["verified"]) is not bool or not row["source_steps"]:
        raise ValueError("Missing experience provenance")
    if (
        not isinstance(row["lesson"], str)
        or len(row["lesson"]) > 1200
        or re.search(r"\d\s*(?:mm\b|hours?\b)|\d{4}-\d\d-\d\d", row["lesson"], re.I)
    ):
        raise ValueError("Experiences must not contain dose or date recipes")
    if row["scope"] != "local_workflow_not_full_season_success":
        raise ValueError("Unsupported memory scope")
    profile = row["profile"]
    if (
        set(profile) != {"backend", "cultivar", "contract_fingerprint"}
        or (profile["backend"], profile["cultivar"])
        not in (("native", "HEINONG58"), ("wofost", "Soybean_901_proxy"))
        or len(profile["contract_fingerprint"]) != 64
    ):
        raise ValueError("Unsupported experience backend/cultivar/contract profile")
    from fairy.agents.loop.context.kg.lifecycle import PHASES

    if (
        row["phase"] not in PHASES
        or not isinstance(row["source_artifact_fingerprint"], str)
        or len(row["source_artifact_fingerprint"]) != 64
    ):
        raise ValueError("Invalid experience scope or artifact identity")
    if not all(
        type(step) is int
        and step > 0
        or isinstance(step, str)
        and re.fullmatch(r"(?:decision|event):[1-9][0-9]*", step)
        for step in row["source_steps"]
    ):
        raise ValueError("Invalid experience evidence pointer")
    if row["id"] != fingerprint({k: v for k, v in row.items() if k != "id"}):
        raise ValueError("Experience identity mismatch")


def append(path: str | Path, rows: Sequence[JsonObject]) -> int:
    """校验、按身份去重并追加JSONL经验，返回新增记录数。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    known = {r["id"] for r in MemorySnapshot(path).rows}
    # Each append publishes complete records; CLI owns one writer per run.
    count = 0
    with path.open("a", encoding="utf-8") as stream:
        for row in rows:
            validate(row)
            if row["id"] not in known:
                stream.write(json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n")
                stream.flush()
                known.add(row["id"])
                count += 1
    return count


def induce(record: JsonObject) -> list[JsonObject]:
    """Conservative evidence extraction, not a claimed reproduction of AWM's inducer."""
    from fairy.events.artifacts import replay

    runtime, _ = replay(
        record
    )  # A claimed local success must survive independent re-execution first.
    if runtime.scenario.season is None:
        raise ValueError("Memory requires full season")
    profile = {
        "backend": runtime.backend_name,
        "cultivar": runtime.scenario.season.cultivar,
        "contract_fingerprint": fingerprint(runtime.operations.tool_definitions()),
    }
    source = fingerprint(record)
    rows = []
    phases = record.get("agent", {}).get("autogen", {}).get("decision_trace", [])
    by_event = {r.get("physical_event_seq"): r for r in phases if r.get("physical_event_seq")}
    requests = {
        e["seq"]: e for e in record["state"]["events"] if e["kind"] == "operation_requested"
    }
    failures = []
    for event in record["state"]["events"]:
        if event["kind"] != "operation_result":
            continue
        req = requests[event["cause"]]
        tool = req["payload"]["tool"]
        meta = by_event.get(event["seq"], {})
        phase = meta.get("phase", "growth_management")
        if not event["payload"]["ok"]:
            failures.append((tool, event["seq"], event["payload"]["error"], phase))
            continue
        if tool in (
            "prepare_seedbed",
            "plant",
            "harvest",
            "unload",
            "dry_grain",
            "store_grain",
            "irrigate_range",
            "advance_time",
        ):
            lesson = f"For {tool}, obtain its current public preconditions, submit legal arguments, then verify the actual receipt; reobserve after delayed effects."
            steps = [event["seq"]]
            if tool == "advance_time":
                observed = next(
                    (
                        e
                        for e in record["state"]["events"]
                        if e["seq"] > event["seq"]
                        and e["kind"] == "operation_result"
                        and e["payload"]["ok"]
                        and requests[e["cause"]]["payload"]["tool"]
                        in ("inspect_crop", "observe_soil_range", "get_current_weather")
                    ),
                    None,
                )
                if observed is None:
                    continue
                steps.append(observed["seq"])
                lesson = "To reach the next management opportunity, explicitly advance simulation time, then obtain current crop, soil or weather evidence. Read-only observations do not advance the clock."
            rows.append(_row(source, phase, "local_success", tool, lesson, steps, True, profile))
            previous = next((r for r in reversed(failures) if r[0] == tool), None)
            if previous:
                rows.append(
                    _row(
                        source,
                        previous[3],
                        "failure_lesson",
                        tool,
                        f"After {previous[2]} for {tool}, revise the request using current contract and public evidence; a subsequent successful receipt verifies only that local recovery.",
                        [previous[1], event["seq"]],
                        True,
                        profile,
                    )
                )
                failures = [r for r in failures if r[0] != tool]
    # Guard blocks are decision evidence, never forged environment errors.
    for index, entry in enumerate(phases):
        if entry.get("kind") != "guard" or entry.get("status") == "PASS":
            continue
        recovery = next(
            (
                r
                for r in phases[index + 1 :]
                if r.get("tool") == entry["tool"] and r.get("kind") == "physical" and r.get("ok")
            ),
            None,
        )
        if recovery:
            rows.append(
                _row(
                    source,
                    entry["phase"],
                    "failure_lesson",
                    entry["tool"],
                    f'For {entry["tool"]}, address the {entry["rule"]} public precondition before retrying; a preflight block is not an executed farm operation.',
                    [
                        "decision:" + str(entry["seq"]),
                        "event:" + str(recovery["physical_event_seq"]),
                    ],
                    True,
                    profile,
                )
            )
    # Deduplicate semantic skills within this run while preserving real source evidence.
    unique = {}
    for row in rows:
        unique[(row["phase"], row["kind"], row["tool"], row["lesson"])] = row
    return list(unique.values())


def _row(
    source: str,
    phase: str,
    kind: str,
    tool: str,
    lesson: str,
    steps: list[int | str],
    verified: bool,
    profile: JsonObject | None,
) -> JsonObject:
    """创建带来源、步骤和适用范围的局部经验记录。"""
    row = dict(
        crop="soybean",
        phase=phase,
        kind=kind,
        tool=tool,
        lesson=lesson,
        verified=verified,
        source_artifact_fingerprint=source,
        source_steps=steps,
        scope="local_workflow_not_full_season_success",
        induction={"method": "conservative_rule_extraction"},
        profile=profile,
    )
    return {"id": fingerprint(row), **row}


def condense(
    rows: Sequence[JsonObject], client: ChatClient, model: str, audit_path: str | Path
) -> list[JsonObject]:
    """Offline model wording over verified public candidates; never mine hidden outcomes."""
    from fairy.events.artifacts import write_json

    chosen = rows[-24:]
    public_candidates = [
        {"index": i, **{key: row[key] for key in ("kind", "phase", "tool", "lesson")}}
        for i, row in enumerate(chosen)
    ]
    payload = dict(
        model=model,
        temperature=0.1,
        max_completion_tokens=4096,
        response_format={"type": "json_object"},
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        messages=[
            {
                "role": "system",
                "content": 'Condense verified local workflow lessons. Return {"lessons":[{"index":existing_integer_index,"lesson":English_text}]} for every supplied item exactly once. Preserve meaning and local scope. Do not invent preconditions, numerical doses, dates, optimality or full-season success. Only rewrite wording; no executable plan. Provenance is attached by the program; never generate hashes.',
            },
            {"role": "user", "content": json.dumps(public_candidates, ensure_ascii=False)},
        ],
    )
    response = client.chat.completions.create(**payload)
    if not response.choices or response.choices[0].finish_reason != "stop":
        raise ValueError("Incomplete memory induction")
    raw = json.loads(cast(str, response.choices[0].message.content))
    if set(raw) != {"lessons"} or not isinstance(raw["lessons"], list):
        raise ValueError("Invalid induction schema")
    by_index = dict(enumerate(chosen))
    result = []
    seen = set()
    for entry in raw["lessons"]:
        if (
            set(entry) != {"index", "lesson"}
            or type(entry["index"]) is not int
            or entry["index"] not in by_index
            or entry["index"] in seen
        ):
            raise ValueError("Induction changed evidence identity")
        seen.add(entry["index"])
        row = detached(by_index[entry["index"]])
        row["lesson"] = entry["lesson"]
        row["induction"] = {
            "method": "llm_condensed_verified_public_candidates",
            "model": response.model,
            "request_fingerprint": fingerprint(payload),
        }
        row["id"] = fingerprint({k: v for k, v in row.items() if k != "id"})
        validate(row)
        result.append(row)
    if seen != set(by_index):
        raise ValueError("Induction omitted evidence")
    write_json(
        audit_path,
        {
            "model": response.model,
            "request_fingerprint": fingerprint(payload),
            "input_candidate_ids": [row["id"] for row in chosen],
            "output_ids": [r["id"] for r in result],
            "input_scope": "verified public candidate lessons only; no source code, future weather, dose trace or stored-mass reward",
            "input_tokens": getattr(response.usage, "prompt_tokens", 0),
            "output_tokens": getattr(response.usage, "completion_tokens", 0),
        },
    )
    return result
