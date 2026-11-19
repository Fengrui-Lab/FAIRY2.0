from typing import Protocol, TYPE_CHECKING, TypeAlias, TypeVar

if TYPE_CHECKING:
    from fairy.agents.loop.context.public import PublicContext
    from fairy.events.session import Runtime
from dataclasses import dataclass
from fairy.acps.ontology.contracts import OperationRequest


@dataclass(frozen=True)
class Decision:
    """模型的一次决策：工具请求、停止意图或协议错误；本身不产生物理效果。"""

    request: OperationRequest | None = None
    finish_reason: str = "model_finished"
    protocol_error: str | None = None


# Public and privileged strategies use separate input types. The controller,
# not metadata supplied by a model, decides which input a strategy may receive.
from fairy.acps.ontology.contracts import JsonObject

ContextT = TypeVar("ContextT", contravariant=True)


class Agent(Protocol[ContextT]):
    """一次决策返回请求或停止意图；只有Controller负责执行。"""

    metadata: JsonObject

    def decide(self, context: ContextT) -> Decision: ...


PublicAgent: TypeAlias = Agent["PublicContext"]
PrivilegedAgent: TypeAlias = Agent["Runtime"]
