"""Explicit, per-session object loading and frozen public operation bindings."""
from dataclasses import asdict, dataclass, replace
from types import MappingProxyType
from typing import Callable
from fairy.acps.ontology.contracts import OperationDefinition, OperationRejected
from fairy.acps.ontology.infrastructure.soil_sensor import SoilSensor
from fairy.acps.ontology.infrastructure.irrigation import IrrigationSystem
from fairy.acps.ontology.service.weather import WeatherService
from fairy.events.operations import ADVANCE_TIME
from fairy.acps.ontology.equipment.operations import SeasonProvider, PROVIDERS


@dataclass(frozen=True)
class BoundOperation:
    definition: OperationDefinition
    provider_id: str
    handler: Callable


class OperationRegistry:
    def __init__(self, bindings):
        items = {}
        for binding in bindings:
            name = binding.definition.name
            if name in items:
                raise ValueError(f"Duplicate operation: {name}")
            items[name] = binding
        self._items = MappingProxyType(items)

    def resolve(self, request):
        if not isinstance(request.tool, str) or request.tool not in self._items:
            raise OperationRejected("UNKNOWN_TOOL")
        binding = self._items[request.tool]
        binding.definition.validate_arguments(request.arguments)
        return binding

    def tool_definitions(self):
        return [b.definition.tool_schema() for b in self._items.values()]

    def manifest(self):
        return [{**b.definition.manifest(), "provider_id": b.provider_id} for b in self._items.values()]


def load_objects(spec):
    """Load approved types only. The plot is passive and offers no operations."""
    objects = {}
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


def bind_operations(runtime, objects):
    """Handlers resolve current session state, including a backend replaced on rollback."""
    bound = []
    for binding in runtime.scenario.operation_bindings:
        if binding.provider_id == "runtime":
            if binding.operation != ADVANCE_TIME.name:
                raise ValueError("Unsupported runtime operation")
            definition = replace(ADVANCE_TIME, parameters=(replace(ADVANCE_TIME.parameters[0], maximum=runtime.scenario.max_advance_hours),))
            bound.append(BoundOperation(definition, "runtime", runtime._advance_time))
            continue
        provider = objects.get(binding.provider_id)
        definition = next((d for d in getattr(provider, "operations", ()) if d.name == binding.operation), None)
        if definition is None:
            raise ValueError("Operation is not offered by its declared provider")
        if isinstance(provider, SeasonProvider):
            handler = lambda args, cause, name=definition.name: runtime._season_operation(name, args, cause)
        elif isinstance(provider, SoilSensor):
            handler = lambda args, cause, p=provider: runtime._observe(p, cause)
        elif isinstance(provider, IrrigationSystem):
            handler = lambda args, cause, p=provider: runtime._irrigate(p, args, cause)
        elif isinstance(provider, WeatherService):
            if definition.name == "get_current_weather":
                handler = lambda args, cause, p=provider: p.current(runtime.now)
            else:
                handler = lambda args, cause, p=provider: p.forecast(runtime.now, args["days"])
        else:
            raise ValueError("Missing operation executor")
        bound.append(BoundOperation(definition, binding.provider_id, handler))
    registry = OperationRegistry(bound)
    if tuple(b.definition.name for b in bound) != runtime.scenario.tools:
        raise ValueError("Exposed operations must match scenario tools in order")
    return registry


def check_backend(registry, backend, crop):
    capabilities = backend.capabilities
    if crop != "soybean" or crop not in capabilities.crops or capabilities.timestep != "local_day":
        raise ValueError("Backend does not support scenario crop or local-day stepping")
    for operation in registry.manifest():
        if not set(operation["required_backend_actions"]) <= set(capabilities.actions):
            raise ValueError("Backend lacks a required operation capability")


def binding_manifest(runtime):
    return {"version": 1, "objects": [asdict(o) for o in runtime.scenario.objects],
            "operations": runtime.operations.manifest(), "biology": asdict(runtime.backend.capabilities),
            "physical": asdict(runtime.physical.capabilities) if runtime.physical else None}
