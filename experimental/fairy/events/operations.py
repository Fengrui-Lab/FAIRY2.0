"""Runtime-owned operation declarations; no agricultural object is fabricated."""

from fairy.acps.ontology.contracts import OperationDefinition, Parameter

ADVANCE_TIME = OperationDefinition(
    "advance_time",
    "Advance simulation hours. Each crossed local midnight integrates the preceding day's weather and queued irrigation once.",
    (Parameter("hours", "number", maximum=48, exclusive_minimum=0, unit="h"),),
    (Parameter("time", "string"),),
    "advance_clock",
)


from dataclasses import dataclass


@dataclass(frozen=True)
class ScheduledJob:
    """提议作业的时间计划：duration_hours为小时，request_seq为因果请求；不表示作业已完成。"""

    operation: str
    start_time: str
    duration_hours: float
    request_seq: int
