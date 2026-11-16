"""Physical adapters share Runtime's clock and transaction/recovery boundary."""
from dataclasses import dataclass
from typing import Protocol,Any

@dataclass(frozen=True)
class PhysicalCapabilities:
    backend_id: str
    model: str
    version: str
    operations: tuple[str,...]=()

class PhysicalBackend(Protocol):
    capabilities: PhysicalCapabilities
    def plan(self,operation: str,arguments: dict[str,Any],biology,weather) -> dict: ...
    def execute(self,operation: str,arguments: dict[str,Any],biology,weather) -> dict: ...
    def inventory(self) -> dict: ...
    def audit(self) -> dict: ...
    def export_state(self) -> dict: ...
    def load_state(self,payload: dict) -> None: ...

# plan is pure and reports duration_hours / fuel_l. Runtime owns the clock and
# checks time budget; execute revalidates and commits at completion. Both physical
# and biological mutations roll back together on failure. No wall-clock stepping.
