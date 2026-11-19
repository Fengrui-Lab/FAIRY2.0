"""Deterministic root-zone observation provider, not a simulated 5 cm probe."""

from __future__ import annotations
from typing import ClassVar
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime
    from fairy.acps.backends.biology.contracts import BiologyBackend, SoilReading
from dataclasses import dataclass
from fairy.acps.ontology.contracts import ObjectSpec, OperationDefinition, Parameter

OBSERVE_SOIL = OperationDefinition(
    "observe_soil",
    "Read rounded root-zone soil moisture. Does not advance time or change physical state.",
    (),
    (
        Parameter("plot_id", "string"),
        Parameter("root_vwc", "number", 0, 1),
        Parameter("measurement", "string"),
        Parameter("unit", "string"),
        Parameter("time", "string"),
    ),
    "read_only",
)


@dataclass(frozen=True)
class SoilSensor:
    """绑定地块的公开根区观测提供者；读取不推进时间，也不更新引擎。"""

    spec: ObjectSpec
    operations: ClassVar[tuple[OperationDefinition, ...]] = (OBSERVE_SOIL,)

    def observe(self, backend: BiologyBackend, now: datetime) -> SoilReading:
        """读取绑定地块的根区含水率，返回带业务时区的公开观测。

        backend遵守共同生物后端协议，可由Native/WOFOST的L1或全季实现提供。
        now由Runtime传入；本方法不推进时间、不结算灌溉、不更新生长。
        若引擎返回了其他地块的观测则抛出RuntimeError，不静默重绑定。
        """
        observation = backend.observe()
        if observation.plot_id != self.spec.target_id:
            raise RuntimeError("Backend returned observation for a different plot")
        return {
            "plot_id": observation.plot_id,
            "root_vwc": observation.root_vwc,
            "measurement": observation.measurement,
            "unit": observation.unit,
            "time": now.isoformat(),
        }
