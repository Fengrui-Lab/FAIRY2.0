"""JSON boundaries detach data; event payloads are immutable serialized values."""

from __future__ import annotations
from typing import Any, TYPE_CHECKING, TypeVar, TypedDict
from dataclasses import dataclass
from fairy.acps.ontology.contracts import JsonObject

T = TypeVar("T")


class PublicStatus(TypedDict):
    """公开时钟及水量额度；pending表示已接受但尚未结算的全田mm。"""

    time: str
    timezone: str
    irrigation_remaining_mm: float
    pending_irrigation_mm: float
    done: bool


class _TurnPayload(TypedDict):
    """一个公开请求/回执对，保留观测发生时刻与物理事件位置。"""

    request: JsonObject
    result: JsonObject
    time: str


class HistoryTurn(_TurnPayload, total=False):
    """物理回执含event_seq，Agent局部服务含decision_seq；两者均不是隐藏状态。"""

    event_seq: int
    decision_seq: int


from datetime import datetime
import hashlib
import json


def canonical(value: Any) -> str:
    """生成可重复指纹化的规范JSON，拒绝NaN/Infinity。"""
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def detached(value: T) -> T:
    """以JSON边界深拷贝数据，避免调用方修改内部容器。"""
    return json.loads(canonical(value))


def fingerprint(value: Any) -> str:
    """对规范JSON计算SHA-256内容身份。"""
    return hashlib.sha256(canonical(value).encode()).hexdigest()


@dataclass(frozen=True)
class EventRecord:
    """不可变的已发生事件。

    seq为连续顺序号，cause链接请求，time带业务时区；payload_json保存规范化副本，
    payload属性每次返回脱离内部数据的字典。"""

    seq: int
    kind: str
    time: str
    cause: int | None
    payload_json: str

    @property
    def payload(self) -> JsonObject:
        """返回脱离事件内部数据的载荷副本，不提供可修改的共享状态。"""
        return json.loads(self.payload_json)

    def to_dict(self) -> JsonObject:
        """返回脱离内部对象的序列化声明，供来源指纹或可信恢复使用。"""
        return dict(
            seq=self.seq, kind=self.kind, time=self.time, cause=self.cause, payload=self.payload
        )

    @classmethod
    def from_dict(cls, value: Any) -> EventRecord:
        """从可信JSON字段重建声明/事件并验证结构，不执行模型或农事。"""
        if not isinstance(value, dict) or set(value) != {"seq", "kind", "time", "cause", "payload"}:
            raise ValueError("Invalid event record")
        if type(value["seq"]) is not int or value["seq"] < 1:
            raise ValueError("Invalid event sequence")
        if not isinstance(value["kind"], str) or not isinstance(value["payload"], dict):
            raise ValueError("Invalid event kind or payload")
        cause = value["cause"]
        if cause is not None and (type(cause) is not int or not 1 <= cause < value["seq"]):
            raise ValueError("Invalid event cause")
        try:
            stamp = datetime.fromisoformat(value["time"])
        except (TypeError, ValueError) as exc:
            raise ValueError("Invalid event time") from exc
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            raise ValueError("Event time must include a UTC offset")
        return cls(value["seq"], value["kind"], value["time"], cause, canonical(value["payload"]))
