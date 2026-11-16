from dataclasses import dataclass
from fairy.acps.ontology.contracts import OperationRequest


@dataclass(frozen=True)
class Decision:
    request: OperationRequest | None = None
    finish_reason: str = "model_finished"
