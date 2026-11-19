"""Derive typed, timestamped evidence exclusively from public context."""

from __future__ import annotations
from typing import Iterable, TYPE_CHECKING

if TYPE_CHECKING:
    from fairy.acps.ontology.contracts import JsonObject
    from fairy.agents.loop.context.public import PublicContext
    from fairy.events.records import HistoryTurn
from datetime import datetime
from fairy.events.records import detached, fingerprint

STAGES = ("unplanted", "pre_emergence", "vegetative", "R1", "R3", "R5", "R6", "R7", "R8")


class PublicView:
    """从公开上下文及回执建立证据视图；缺失、过期和不完整覆盖都不等于零。"""

    context: PublicContext  #: 只读输入约定；不得由此访问Runtime审计。
    now: datetime  #: 公开业务时钟，用于同日/24小时证据有效性检查。

    def __init__(self, context: PublicContext) -> None:
        """保存显式依赖与初态；模型服务调用发生在决策阶段而非构造阶段。"""
        self.context = context
        self.now = datetime.fromisoformat(context["status"]["time"])
        self.status = context["status"]
        self.count = context.get("constraints", {}).get("ridge_count", 64)
        self.turns = list(context.get("history", ())) + list(
            context.get("public_memory", {}).get("observations", ())
        )
        self.turns.sort(key=lambda t: (t["time"], t.get("event_seq", 0)))

    def latest(self, tool: str, *, fresh: bool = True) -> HistoryTurn | None:
        """检索目标工具最近成功回执，按需检查业务日、证据有效期和状态变更。"""
        rows = [t for t in self.turns if t["request"]["tool"] == tool and t["result"].get("ok")]
        for turn in reversed(rows):
            age = (self.now - datetime.fromisoformat(turn["time"])).total_seconds() / 3600
            if age >= 0 and (
                not fresh
                or (age <= 24 and datetime.fromisoformat(turn["time"]).date() == self.now.date())
            ):
                if (
                    tool == "inspect_inventory"
                    and fresh
                    and turn.get("event_seq", 0)
                    < self.context.get("progress", {}).get("last_inventory_change_seq", 0)
                ):
                    continue
                return turn
        return None

    def field(
        self, tool: str, indices: Iterable[int] | None = None, *, fresh: bool = True
    ) -> list[JsonObject] | None:
        """读取目标垄完整覆盖的公开行；缺失/过期返回None，而非零水分或空闲库存。"""
        targets = set(range(self.count) if indices is None else indices)
        found = {}
        for turn in self.turns:
            if turn["request"]["tool"] != tool or not turn["result"].get("ok"):
                continue
            if tool == "inspect_crop" and turn.get("event_seq", 0) < self.context.get(
                "progress", {}
            ).get("last_planting_change_seq", 0):
                continue
            stamp = datetime.fromisoformat(turn["time"])
            if fresh and (
                stamp.date() != self.now.date()
                or not 0 <= (self.now - stamp).total_seconds() <= 86400
            ):
                continue
            rows = turn["result"]["data"].get("receipt", {}).get("observations", ())
            for row in rows:
                if row["ridge_id"] in targets:
                    found[row["ridge_id"]] = detached(row)
        return [found[i] for i in sorted(targets)] if set(found) == targets else None

    def data(self, tool: str, *, fresh: bool = True) -> JsonObject | None:
        """读取有效的公开工具数据；不能访问引擎审计真值。"""
        turn = self.latest(tool, fresh=fresh)
        return turn["result"]["data"] if turn else None

    def inventory(self) -> JsonObject | None:
        """返回公开库存/容器回执，不执行作业或推进时间。"""
        value = self.data("inspect_inventory")
        return value.get("receipt") if value else None

    def evidence_id(self) -> str:
        # Only public status and current recorded sensor/weather receipts; no audit truth.
        """根据当前公开证据生成身份，供建议与实际灌溉绑定。"""
        relevant = [
            t
            for t in self.turns
            if t["request"]["tool"]
            in ("inspect_crop", "observe_soil_range", "get_current_weather", "get_forecast")
        ]
        return fingerprint({"status": self.status, "evidence": relevant})

    def phase(self) -> str:
        """从真实管理回执及作物观测判定工作流阶段，区别于引擎生育阶段。"""
        progress = self.context.get("progress", {})
        if len(progress.get("harvested_ridges", ())) == self.count:
            return "postharvest"
        if len(progress.get("planted_ridges", ())) < self.count:
            prepared = set(progress.get("prepared_ridges", ()))
            for t in self.turns:
                if t["result"].get("ok"):
                    receipt = t["result"]["data"].get("receipt", {})
                    if receipt.get("operation") in ("prepare_seedbed", "plant"):
                        prepared.update(receipt.get("completed_ridges", ()))
                    prepared.update(receipt.get("prepared_ridges", ()))
            # Public memory may omit old preparation receipts; an inventory can confirm them.
            if len(prepared) == self.count or progress.get("planted_ridges"):
                return "establishment"
            return "preparation"
        crop = self.field("inspect_crop", fresh=False)
        if not crop:
            return "establishment"
        remaining = [
            r for r in crop if r["ridge_id"] not in set(progress.get("harvested_ridges", ()))
        ]
        if remaining and all(r.get("harvestable", r["stage"] == "R8") for r in remaining):
            return "harvest"
        if any(r["stage"] in ("unplanted", "pre_emergence") for r in crop):
            return "establishment"
        return "growth_management"


def grouped_context(context: PublicContext) -> PublicContext:
    """Lossless grouping of identical per-ridge public observations, with original IDs."""
    result = detached(context)

    def group(turn: HistoryTurn) -> None:
        receipt = turn.get("result", {}).get("data", {}).get("receipt", {})
        rows = receipt.get("observations")
        if not rows or not all("ridge_id" in row for row in rows):
            return
        groups: dict[str, JsonObject] = {}
        for row in rows:
            values = {k: v for k, v in row.items() if k != "ridge_id"}
            key = fingerprint(values)
            groups.setdefault(key, {**values, "ridge_ids": []})["ridge_ids"].append(row["ridge_id"])
        receipt.pop("observations")
        receipt["observations_grouped"] = list(groups.values())
        receipt["projection"] = (
            "lossless identical-value grouping; ridge_ids preserve full coverage"
        )

    for turn in result.get("history", ()):
        group(turn)
    for turn in result.get("public_memory", {}).get("observations", ()):
        group(turn)
    return result
