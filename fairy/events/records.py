"""JSON boundaries detach data; event payloads are immutable serialized values."""
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def detached(value):
    return json.loads(canonical(value))


def fingerprint(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


@dataclass(frozen=True)
class EventRecord:
    seq: int
    kind: str
    time: str
    cause: int | None
    payload_json: str

    @property
    def payload(self):
        return json.loads(self.payload_json)

    def to_dict(self):
        return dict(seq=self.seq, kind=self.kind, time=self.time, cause=self.cause, payload=self.payload)

    @classmethod
    def from_dict(cls, value):
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
