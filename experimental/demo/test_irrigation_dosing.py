"""Public LLM intent, numerical dose, and actual WOFOST settlement in one loop."""

from dataclasses import dataclass, field
import json
from types import SimpleNamespace

import pytest

from evaluate.experiments.historical.operations import plant_prefix
from evaluate.scenarios.lib.l3.soybean_wofost import (
    soybean_wofost_calendar_drought,
    soybean_wofost_normal,
)
from fairy.acps.ontology.contracts import OperationRequest
from fairy.agents.loop.context.history import public_history
from fairy.agents.loop.context.water import soil_profile
from fairy.agents.loop.controller.irrigation_dose import INTENT, IrrigationDosing
from fairy.agents.loop.controller.run import run_agent
from fairy.agents.models.llm.adapter import LLMAdapter
from fairy.agents.models.numeric.dose import (
    DoseChoice,
    FixedIrrigationDose,
    IrrigationDoseInput,
)
from fairy.events.artifacts import artifact, replay, write_json
from fairy.events.session import Runtime


class ScriptedChat:
    """Exercise the actual LLM adapter with deterministic responses and capture messages."""

    def __init__(self, operations):
        self.operations = iter(operations)
        self.chat = SimpleNamespace(completions=self)
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        operation = next(self.operations)
        calls = (
            []
            if operation is None
            else [
                SimpleNamespace(
                    function=SimpleNamespace(
                        name=operation[0], arguments=json.dumps(operation[1])
                    )
                )
            ]
        )
        return SimpleNamespace(
            model="scripted",
            usage=None,
            choices=[
                SimpleNamespace(
                    finish_reason="tool_calls" if calls else "stop",
                    message=SimpleNamespace(
                        content=None if calls else "Done", tool_calls=calls
                    ),
                )
            ],
        )


@dataclass
class RecordingPolicy:
    nominal_mm: float
    evidence: list[IrrigationDoseInput] = field(default_factory=list)
    identity: str = "fixed_integration_control"

    def choose(self, evidence: IrrigationDoseInput) -> DoseChoice:
        self.evidence.append(evidence)
        return FixedIrrigationDose(self.nominal_mm).choose(evidence)


def exercise(spec, target, tmp_path):
    runtime = plant_prefix(spec, backend="wofost")
    for _ in range(12):
        result = runtime.invoke(
            OperationRequest("advance_time", {"hours": 48}), actor="fixture"
        )
        assert result.ok, result.error
    policy = RecordingPolicy(8)
    service = IrrigationDosing(policy, soil_profile(runtime.scenario))
    script = ScriptedChat(
        [
            ("irrigate_range", {**target, "depth_mm": 9}),
            (INTENT, {**target, "depth_mm": 9}),
            (INTENT, target),
            ("inspect_crop", target),
            ("observe_soil_range", target),
            ("get_current_weather", {}),
            (INTENT, target),
            (INTENT, target),
            ("advance_time", {"hours": 24}),
            (INTENT, target),
            ("inspect_crop", target),
            ("observe_soil_range", target),
            (INTENT, target),
            ("advance_time", {"hours": 24}),
            None,
        ]
    )
    agent = LLMAdapter(model="fake", client=script)
    run_agent(
        runtime,
        agent,
        irrigation_dosing=service,
        max_steps=16,
        max_errors=8,
        max_context_chars=60000,
        max_history_turns=14,
    )
    history = public_history(runtime)
    water = [turn for turn in history if turn["request"]["tool"] == "irrigate_range"]
    expected_mm = 8 * (target["end_ridge"] - target["start_ridge"] + 1) / 64
    assert len(water) == 2
    assert all(t["result"]["ok"] for t in water)
    assert all(t["request"]["arguments"] == {**target, "depth_mm": 8} for t in water)
    assert runtime.public_status()["pending_irrigation_mm"] == 0
    assert runtime.irrigation_used_mm == pytest.approx(2 * expected_mm)

    assert len(policy.evidence) == 2
    assert policy.evidence[0].last_settled_zone_depth_mm == 0
    assert policy.evidence[1].last_settled_zone_depth_mm == 8
    assert all(
        [r.ridge_id for r in e.ridges]
        == list(range(target["start_ridge"], target["end_ridge"] + 1))
        for e in policy.evidence
    )
    assert all(e.ridges[0].root_vwc > 0 for e in policy.evidence)
    if "get_forecast" not in spec.tools:
        from datetime import timedelta

        observed = policy.evidence[0]
        assert observed.weather is not None
        assert (
            observed.weather.day
            == (observed.time.date() - timedelta(days=1)).isoformat()
        )

    assert [a["rejected"] for a in service.attempts if "rejected" in a] == [
        "IRRIGATION_INTENT_REQUIRED",
        "INVALID_IRRIGATION_TARGET",
        "FRESH_IRRIGATION_EVIDENCE_REQUIRED",
        "IRRIGATION_PENDING",
        "FRESH_IRRIGATION_EVIDENCE_REQUIRED",
    ]
    assert [a["event_seq"] for a in service.attempts if "ok" in a] == [
        t["event_seq"] for t in water
    ]

    before = script.requests[6]
    tools = {t["function"]["name"]: t["function"] for t in before["tools"]}
    assert "irrigate_range" not in tools
    assert set(tools[INTENT]["parameters"]["properties"]) == target.keys()
    public = json.loads(before["messages"][1]["content"])
    assert (
        public["water_diagnostics"]["zones"][0]["current_crop_water"]["status"]
        == "estimated"
    )
    assert '"root_vwc"' not in json.dumps(before["messages"])
    assert (
        json.loads(script.requests[8]["messages"][1]["content"])["dose_feedback"][
            "error"
        ]
        == "IRRIGATION_PENDING"
    )
    calls = [
        c["function"]
        for msg in script.requests[8]["messages"]
        for c in msg.get("tool_calls", ())
    ]
    assert next(c for c in calls if c["name"] == INTENT)["arguments"] == json.dumps(
        target, separators=(",", ":"), sort_keys=True
    )

    saved = artifact(runtime, agent.metadata)
    write_json(tmp_path / "run.json", saved)
    rebuilt, metrics = replay(saved)
    assert metrics["replay_verified"]
    assert rebuilt.irrigation_used_mm == runtime.irrigation_used_mm
    return saved


@pytest.mark.parametrize(
    ("factory", "target"),
    [
        (soybean_wofost_normal, {"start_ridge": 0, "end_ridge": 8}),
        (soybean_wofost_calendar_drought, {"start_ridge": 22, "end_ridge": 43}),
    ],
)
def test_dose_pipeline(factory, target, tmp_path):
    pytest.importorskip("pcse")
    exercise(factory(), target, tmp_path)


def test_dose_resume_preserves_pending_and_replay(monkeypatch, tmp_path):
    pytest.importorskip("pcse")
    from interface.cli.__main__ import execute
    from argparse import Namespace

    spec = soybean_wofost_normal()
    runtime = plant_prefix(spec, backend="wofost")
    for _ in range(12):
        assert runtime.invoke(OperationRequest("advance_time", {"hours": 48})).ok
    target = {"start_ridge": 22, "end_ridge": 29}
    script = ScriptedChat(
        [("inspect_crop", target), ("observe_soil_range", target), (INTENT, target)]
    )
    agent = LLMAdapter(model="fake", client=script)
    service = IrrigationDosing(FixedIrrigationDose(8), soil_profile(spec))

    def interrupt_after_water(_, request, result):
        if request.tool == "irrigate_range" and result.ok:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_agent(
            runtime,
            agent,
            irrigation_dosing=service,
            on_step=interrupt_after_water,
            max_context_chars=60000,
        )
    source = tmp_path / "suspended.json"
    write_json(source, artifact(runtime, agent.metadata))
    assert not runtime.done
    assert runtime.public_status()["pending_irrigation_mm"] == 1

    resumed = ScriptedChat([(INTENT, target), ("advance_time", {"hours": 24}), None])
    monkeypatch.setattr(
        "fairy.agents.models.llm.adapter.LLMAdapter",
        lambda **kwargs: LLMAdapter(model="fake", client=resumed),
    )
    out = tmp_path / "continued.json"
    args = Namespace(
        command="resume",
        path=str(source),
        out=str(out),
        agent="llm",
        model="fake",
        base_url=None,
        max_output_tokens=512,
        disable_thinking=False,
        request_timeout=30,
        fixed_irrigation_dose_mm=5,
        max_steps=8,
        max_errors=8,
        max_protocol_errors=3,
        context_chars=60000,
        wall_timeout=120,
        history_turns=14,
    )
    with pytest.raises(ValueError, match="same dose policy"):
        execute(args, tmp_path / "progress.jsonl")
    args.fixed_irrigation_dose_mm = 8
    assert (
        execute(args, tmp_path / "progress.jsonl") == 1
    )  # The probe ends after settlement.
    from fairy.events.artifacts import load_json

    record = load_json(out)
    actions = record["agent"]["irrigation_dosing"]["attempts"]
    assert actions[0]["ok"]
    assert actions[1]["rejected"] == "IRRIGATION_PENDING"
    assert record["state"]["pending_irrigation_mm"] == 0
    assert record["metrics"]["water_used_mm"] == 1
    assert replay(record)[1]["replay_verified"]
