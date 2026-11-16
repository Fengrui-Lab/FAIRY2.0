"""Deterministic root-zone observation provider, not a simulated 5 cm probe."""
from dataclasses import asdict, dataclass
from fairy.acps.ontology.contracts import ObjectSpec, OperationDefinition, Parameter

OBSERVE_SOIL = OperationDefinition(
    "observe_soil", "Read rounded root-zone soil moisture. Does not advance time or change physical state.", (),
    (Parameter("plot_id", "string"), Parameter("root_vwc", "number", 0, 1),
     Parameter("measurement", "string"), Parameter("unit", "string"), Parameter("time", "string")),
    "read_only")


@dataclass(frozen=True)
class SoilSensor:
    spec: ObjectSpec
    operations = (OBSERVE_SOIL,)

    def observe(self, backend, now):
        observation = backend.observe()
        if observation.plot_id != self.spec.target_id:
            raise RuntimeError("Backend returned observation for a different plot")
        return {**asdict(observation), "time": now.isoformat()}
