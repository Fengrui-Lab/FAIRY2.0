"""Public-only features, legal doses and a frozen MaskablePPO adviser."""

from __future__ import annotations
from typing import Protocol, TYPE_CHECKING, TypedDict

if TYPE_CHECKING:
    from fairy.agents.loop.context.public import PublicContext
from fairy.acps.ontology.contracts import JsonObject
from pathlib import Path
import hashlib
import json
from fairy.agents.loop.controller.autogen.public import PublicView, STAGES
from fairy.events.records import fingerprint


class IrrigationAdvice(TypedDict):
    """冻结数值模型的公开建议；不执行灌溉，剂量为全田毛水深mm。"""

    dose_mm: float
    action: int
    features: list[float]
    policy_fingerprint: str


class NumericAdvisor(Protocol):
    """PPO与显式零剂量对照共用接口，供局部Agent服务调用。"""

    @property
    def manifest(self) -> JsonObject: ...
    @property
    def fingerprint(self) -> str: ...
    def advise(self, context: PublicContext) -> IrrigationAdvice: ...


DOSES = (0.0, 5.0, 10.0, 20.0, 30.0)
FEATURE_VERSION = 2
PHASES = ("unplanted", "pre_emergence", "vegetative", "reproductive", "mature", "harvested")
FEATURES = (
    "phase_fraction_v2",
    "root_vwc_mean",
    "root_vwc_min",
    "root_vwc_max",
    "forecast_rain_3day_mean_over_20",
    "current_et0_over_10",
    "remaining_quota_over_60",
    "pending_over_50",
)


def features(context: PublicContext) -> list[float]:
    """提取固定顺序的公开数值特征；证据缺失或过期时报错，不能伪装成零建议。"""
    view = PublicView(context)
    crop, soil = view.field("inspect_crop"), view.field("observe_soil_range")
    weather, forecast = view.data("get_current_weather"), view.data("get_forecast")
    missing = [
        name
        for name, value in [
            ("inspect_crop", crop),
            ("observe_soil_range", soil),
            ("get_current_weather", weather),
            ("get_forecast", forecast),
        ]
        if not value
    ]
    if missing:
        raise ValueError("NEED_OBSERVATION:" + ",".join(missing))
    assert crop is not None and soil is not None and weather is not None and forecast is not None
    stages = [
        PHASES.index(
            r.get(
                "phase",
                (
                    r["stage"]
                    if r["stage"] in PHASES
                    else ("mature" if r["stage"] == "R8" else "reproductive")
                ),
            )
        )
        for r in crop
    ]
    water = [r["root_vwc"] for r in soil]
    if any(not 0 <= v <= 1 for v in water):
        raise ValueError("Invalid public VWC")
    issued = forecast["issued_at"]
    if issued[:10] != view.now.date().isoformat():
        raise ValueError("NEED_OBSERVATION:get_forecast")
    future = forecast["forecast"][:3]
    if not future:
        raise ValueError("NEED_OBSERVATION:get_forecast")
    return [
        sum(stages) / len(stages) / (len(PHASES) - 1),
        sum(water) / len(water),
        min(water),
        max(water),
        sum(d["rain_mm"] for d in future) / len(future) / 20,
        weather["weather"]["et0_mm"] / 10,
        view.status["irrigation_remaining_mm"] / 60,
        view.status["pending_irrigation_mm"] / 50,
    ]


def action_masks(context: PublicContext) -> list[bool]:
    """根据剩余水额和pending生成合法动作掩码；零剂量始终可用。"""
    status = context["status"]
    return [
        dose == 0
        or (
            status["pending_irrigation_mm"] == 0
            and dose <= status["irrigation_remaining_mm"] + 1e-9
        )
        for dose in DOSES
    ]


class PPOAdvisor:
    """验证来源与权重哈希后加载的冻结数值顾问；只建议全田毛水深，不执行农事。"""

    manifest: JsonObject  #: 已验证权重、特征、后端与场景适用域。
    fingerprint: str  #: manifest内容身份，用于实际建议绑定。

    def __init__(self, path: str | Path, *, expected_source: str | None = None) -> None:
        """保存显式依赖与初态；模型服务调用发生在决策阶段而非构造阶段。"""
        self.path = Path(path)
        self.manifest = json.loads(self.path.with_suffix(".json").read_text())
        if (
            self.manifest.get("schema") != 2
            or self.manifest.get("feature_version") != FEATURE_VERSION
        ):
            raise ValueError("Incompatible numerical policy version; retraining required")
        digest = hashlib.sha256(self.path.read_bytes()).hexdigest()
        if digest != self.manifest["weights_sha256"]:
            raise ValueError("Policy weights fingerprint mismatch")
        if self.manifest["features"] != list(FEATURES) or self.manifest["doses_mm"] != list(DOSES):
            raise ValueError("Incompatible numerical policy semantics")
        if expected_source and self.manifest["source_fingerprint"] != expected_source:
            raise ValueError("Policy was trained against different executable sources")
        from sb3_contrib import MaskablePPO

        self.policy = MaskablePPO.load(self.path, device="cpu")
        self.fingerprint = fingerprint(self.manifest)

    def advise(self, context: PublicContext) -> IrrigationAdvice:
        """从当前公开证据得到冻结剂量建议；不灌溉、不推进时间。"""
        import numpy as np

        scope = context.get("constraints", {})
        if self.manifest.get("backend") != scope.get("backend", "native") or self.manifest.get(
            "crop"
        ) != scope.get("cultivar"):
            raise ValueError("Policy backend/cultivar scope mismatch")
        vector = features(context)
        masks = action_masks(context)
        action, _ = self.policy.predict(
            np.asarray(vector, dtype=np.float32), action_masks=np.asarray(masks), deterministic=True
        )
        action = int(action)
        if not 0 <= action < len(DOSES) or not masks[action]:
            raise ValueError("Policy selected illegal action")
        return {
            "dose_mm": DOSES[action],
            "action": action,
            "features": vector,
            "policy_fingerprint": self.fingerprint,
        }


class ZeroBaseline:
    """Explicit untrained comparator; never qualifies as a trained expert."""

    manifest = {
        "algorithm": "constant_zero_baseline",
        "status": "untrained_baseline",
        "features": list(FEATURES),
        "doses_mm": list(DOSES),
    }
    fingerprint = fingerprint(manifest)

    def advise(self, context: PublicContext) -> IrrigationAdvice:
        """从当前公开证据得到冻结剂量建议；不灌溉、不推进时间。"""
        return {
            "dose_mm": 0.0,
            "action": 0,
            "features": features(context),
            "policy_fingerprint": self.fingerprint,
        }
