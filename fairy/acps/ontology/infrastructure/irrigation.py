"""Single-plot irrigation declaration; runtime owns pending-input settlement."""
from dataclasses import dataclass
from fairy.acps.ontology.contracts import ObjectSpec, OperationDefinition, OperationRejected, Parameter

IRRIGATE = OperationDefinition(
    "irrigate", "Queue gross irrigation in mm for the bound plot. Applied once at the next local midnight. Does not advance time.",
    (Parameter("depth_mm", "number", exclusive_minimum=0, unit="mm"),),
    (Parameter("queued_mm", "number", 0), Parameter("queued_m3", "number", 0), Parameter("settlement", "string")),
    "next_local_midnight", required_backend_actions=("irrigate",))


@dataclass(frozen=True)
class IrrigationSystem:
    spec: ObjectSpec
    operations = (IRRIGATE,)

    def prepare(self, depth_mm, farm, remaining_mm):
        if farm.plot_id != self.spec.target_id:
            raise RuntimeError("Irrigation target differs from bound plot")
        if depth_mm > remaining_mm + 1e-9:
            raise OperationRejected("WATER_QUOTA_EXCEEDED")
        return {"queued_mm": float(depth_mm), "queued_m3": farm.volume_m3(depth_mm),
                "settlement": "next_local_midnight"}
