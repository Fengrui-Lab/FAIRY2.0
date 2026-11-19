"""Explicit, per-session object loading and frozen public operation bindings."""

from __future__ import annotations
from typing import Any, Callable, Iterable, Mapping, TYPE_CHECKING, TypeAlias

if TYPE_CHECKING:
    from evaluate.scenarios.spec import ScenarioSpec
    from fairy.acps.backends.biology.contracts import BiologyBackend, SoilReading
    from fairy.acps.ontology.contracts import JsonObject, OperationRequest
    from fairy.acps.ontology.crops.lib.soybean import SoybeanSpec
    from fairy.events.session import Runtime
from dataclasses import asdict, dataclass, replace
from types import MappingProxyType
from fairy.acps.ontology.farm.spec import FarmSpec
from fairy.acps.ontology.contracts import OperationDefinition, OperationRejected, JsonObject
from fairy.acps.ontology.infrastructure.soil_sensor import SoilSensor
from fairy.acps.ontology.infrastructure.irrigation import IrrigationSystem
from fairy.acps.ontology.service.weather import WeatherService
from fairy.events.operations import ADVANCE_TIME
from fairy.acps.ontology.equipment.operations import SeasonProvider, PROVIDERS

ProviderObject: TypeAlias = (
    FarmSpec | SoilSensor | IrrigationSystem | WeatherService | SeasonProvider
)
OperationHandler: TypeAlias = Callable[[JsonObject, int], Mapping[str, Any]]


@dataclass(frozen=True)
class BoundOperation:
    """一个公开操作、提供者身份与类型化处理器的绑定；处理器延迟读取当前Runtime。"""

    definition: OperationDefinition
    provider_id: str
    handler: OperationHandler


class OperationRegistry:
    """每次运行独立的只读操作目录；参数验证通过后才返回绑定处理器。"""

    def __init__(self, bindings: Iterable[BoundOperation]) -> None:
        """保存显式依赖与初态；模型服务调用发生在决策阶段而非构造阶段。"""
        items = {}
        for binding in bindings:
            name = binding.definition.name
            if name in items:
                raise ValueError(f"Duplicate operation: {name}")
            items[name] = binding
        self._items = MappingProxyType(items)

    def resolve(self, request: OperationRequest) -> BoundOperation:
        """按公开操作名查找绑定并验证参数，未知操作明确拒绝。"""
        if not isinstance(request.tool, str) or request.tool not in self._items:
            raise OperationRejected("UNKNOWN_TOOL")
        binding = self._items[request.tool]
        binding.definition.validate_arguments(request.arguments)
        return binding

    def tool_definitions(self) -> list[JsonObject]:
        """返回当前允许操作的公开JSON Schema，不包含处理器或引擎状态。"""
        return [b.definition.tool_schema() for b in self._items.values()]

    def manifest(self) -> list[JsonObject]:
        """返回可指纹化的版本化契约或绑定描述。"""
        return [
            {**b.definition.manifest(), "provider_id": b.provider_id} for b in self._items.values()
        ]


def load_objects(spec: ScenarioSpec) -> Mapping[str, ProviderObject]:
    """Load approved types only. The plot is passive and offers no operations."""
    objects: dict[str, ProviderObject] = {}
    obj: ProviderObject
    for declaration in spec.objects:
        if declaration.object_id == "runtime" or declaration.object_id in objects:
            raise ValueError("Duplicate or reserved object ID")
        if declaration.type_id == "plot":
            if declaration.object_id != spec.farm.plot_id or declaration.target_id is not None:
                raise ValueError("Pilot plot declaration must match farm geometry")
            obj = spec.farm
        elif declaration.type_id == "soil_sensor":
            obj = SoilSensor(declaration)
        elif declaration.type_id == "irrigation_system":
            obj = IrrigationSystem(declaration)
        elif declaration.type_id in PROVIDERS and spec.season is not None:
            obj = SeasonProvider(declaration)
        elif declaration.type_id == "weather_service":
            obj = WeatherService(declaration, spec.public_weather)
        else:
            raise ValueError(f"Unsupported object type: {declaration.type_id}")
        objects[declaration.object_id] = obj
    if spec.farm.plot_id not in objects or objects[spec.farm.plot_id] is not spec.farm:
        raise ValueError("Scenario must declare its passive plot object")
    for declaration in spec.objects:
        if declaration.type_id != "plot" and declaration.target_id != spec.farm.plot_id:
            raise ValueError("Provider must target the declared pilot plot")
    return MappingProxyType(objects)


def _season_handler(runtime: Runtime, name: str) -> OperationHandler:
    """在绑定时捕获操作名，执行时读取当前Runtime，兼容回滚替换后端。"""

    def handle(args: JsonObject, cause: int) -> JsonObject:
        return runtime._season_operation(name, args, cause)

    return handle


def _soil_handler(runtime: Runtime, provider: SoilSensor) -> OperationHandler:
    """传感器绑定保留具体提供者类型；不提前读取观测。"""

    def handle(args: JsonObject, cause: int) -> SoilReading:
        return runtime._observe(provider, cause)

    return handle


def _irrigation_handler(runtime: Runtime, provider: IrrigationSystem) -> OperationHandler:
    """仅登记水量；实际结算仍由统一日界触发。"""

    def handle(args: JsonObject, cause: int) -> JsonObject:
        return runtime._irrigate(provider, args, cause)

    return handle


def _weather_handler(
    runtime: Runtime, provider: WeatherService, *, forecast: bool
) -> OperationHandler:
    """执行时按业务日期返回已声明的公开天气，不读取引擎forcing。"""

    def handle(args: JsonObject, cause: int) -> JsonObject:
        return (
            provider.forecast(runtime.now, args["days"])
            if forecast
            else provider.current(runtime.now)
        )

    return handle


def bind_operations(runtime: Runtime, objects: Mapping[str, ProviderObject]) -> OperationRegistry:
    """Handlers resolve current session state, including a backend replaced on rollback."""
    bound: list[BoundOperation] = []
    handler: OperationHandler
    for binding in runtime.scenario.operation_bindings:
        if binding.provider_id == "runtime":
            if binding.operation != ADVANCE_TIME.name:
                raise ValueError("Unsupported runtime operation")
            runtime_definition = replace(
                ADVANCE_TIME,
                parameters=(
                    replace(ADVANCE_TIME.parameters[0], maximum=runtime.scenario.max_advance_hours),
                ),
            )
            bound.append(BoundOperation(runtime_definition, "runtime", runtime._advance_time))
            continue
        provider = objects.get(binding.provider_id)
        definition = next(
            (d for d in getattr(provider, "operations", ()) if d.name == binding.operation), None
        )
        if definition is None:
            raise ValueError("Operation is not offered by its declared provider")
        if isinstance(provider, SeasonProvider):
            handler = _season_handler(runtime, definition.name)
        elif isinstance(provider, SoilSensor):
            handler = _soil_handler(runtime, provider)
        elif isinstance(provider, IrrigationSystem):
            handler = _irrigation_handler(runtime, provider)
        elif isinstance(provider, WeatherService):
            if definition.name == "get_current_weather":
                handler = _weather_handler(runtime, provider, forecast=False)
            else:
                handler = _weather_handler(runtime, provider, forecast=True)
        else:
            raise ValueError("Missing operation executor")
        bound.append(BoundOperation(definition, binding.provider_id, handler))
    registry = OperationRegistry(bound)
    if tuple(b.definition.name for b in bound) != runtime.scenario.tools:
        raise ValueError("Exposed operations must match scenario tools in order")
    return registry


def check_backend(registry: OperationRegistry, backend: BiologyBackend, crop: str) -> None:
    """核查作物、本地日步进与操作所需能力，不静默回退。"""
    capabilities = backend.capabilities
    if crop != "soybean" or crop not in capabilities.crops or capabilities.timestep != "local_day":
        raise ValueError("Backend does not support scenario crop or local-day stepping")
    for operation in registry.manifest():
        if not set(operation["required_backend_actions"]) <= set(capabilities.actions):
            raise ValueError("Backend lacks a required operation capability")


def binding_manifest(runtime: Runtime) -> JsonObject:
    """生成对象、操作及生物/物理能力的共同绑定身份。"""
    return {
        "version": 1,
        "objects": [asdict(o) for o in runtime.scenario.objects],
        "operations": runtime.operations.manifest(),
        "biology": asdict(runtime.backend.capabilities),
        "physical": asdict(runtime.physical.capabilities) if runtime.physical else None,
    }
