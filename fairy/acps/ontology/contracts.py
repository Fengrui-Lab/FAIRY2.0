"""Immutable object/operation declarations shared by providers and runtime."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from fairy.acps.ontology.farm.spec import finite


class OperationRejected(ValueError):
    """Expected public rejection with a stable, non-sensitive error code."""


@dataclass(frozen=True)
class ObjectSpec:
    object_id: str
    type_id: str
    target_id: str | None = None

    def __post_init__(self):
        for value in (self.object_id, self.type_id):
            if not isinstance(value, str) or not value.strip():
                raise ValueError("Object identifiers must be non-empty strings")
        if self.target_id is not None and (not isinstance(self.target_id, str) or not self.target_id.strip()):
            raise ValueError("Target must be a non-empty identifier")


@dataclass(frozen=True)
class OperationBinding:
    operation: str
    provider_id: str

    def __post_init__(self):
        if any(not isinstance(v, str) or not v.strip() for v in (self.operation, self.provider_id)):
            raise ValueError("Operation bindings require non-empty names")


@dataclass(frozen=True)
class OperationRequest:
    tool: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class OperationResult:
    ok: bool
    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


@dataclass(frozen=True)
class Parameter:
    name: str
    kind: str
    minimum: float | None = None
    maximum: float | None = None
    exclusive_minimum: float | None = None
    unit: str | None = None

    def __post_init__(self):
        if not isinstance(self.name, str) or not self.name.isidentifier():
            raise ValueError("Parameter names must be identifiers")
        if self.kind not in ("number", "integer", "string", "array", "object", "boolean"):
            raise ValueError("Unsupported parameter type")
        bounds = (self.minimum, self.maximum, self.exclusive_minimum)
        if any(v is not None for v in bounds) and self.kind not in ("number", "integer"):
            raise ValueError("Numeric bounds require a numeric type")
        for value in bounds:
            if value is not None:
                finite(value, "bound")
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("Inverted numeric bounds")
        if self.exclusive_minimum is not None and self.maximum is not None and self.exclusive_minimum >= self.maximum:
            raise ValueError("Empty exclusive range")
        if self.kind == "integer":
            from math import ceil, floor
            lower = ceil(self.minimum) if self.minimum is not None else None
            if self.exclusive_minimum is not None:
                exclusive = floor(self.exclusive_minimum) + 1
                lower = exclusive if lower is None else max(lower, exclusive)
            if lower is not None and self.maximum is not None and lower > floor(self.maximum):
                raise ValueError("Integer range contains no values")
        if self.unit is not None and (not isinstance(self.unit, str) or not self.unit.strip()):
            raise ValueError("Unit must be a non-empty string")

    def schema(self):
        result = {"type": self.kind}
        for key, value in (("minimum", self.minimum), ("maximum", self.maximum),
                           ("exclusiveMinimum", self.exclusive_minimum)):
            if value is not None:
                result[key] = value
        if self.unit:
            result["description"] = f"Unit: {self.unit}."
        return result

    def validate(self, value):
        if self.kind in ("number", "integer"):
            if self.kind == "integer" and type(value) is not int:
                raise ValueError("Expected integer")
            finite(value, self.name, self.minimum, self.maximum)
            if self.exclusive_minimum is not None and value <= self.exclusive_minimum:
                raise ValueError("Below exclusive minimum")
        else:
            expected = {"string": str, "array": list, "object": dict, "boolean": bool}[self.kind]
            if type(value) is not expected:
                raise ValueError("Invalid value type")


@dataclass(frozen=True)
class OperationDefinition:
    name: str
    description: str
    parameters: tuple[Parameter, ...]
    results: tuple[Parameter, ...]
    timing: str
    version: int = 1
    required_backend_actions: tuple[str, ...] = ()

    def __post_init__(self):
        if not isinstance(self.name, str) or not self.name.isidentifier():
            raise ValueError("Operation name must be an identifier")
        if not isinstance(self.description, str) or not self.description.strip():
            raise ValueError("Operation description is required")
        if self.timing not in ("read_only", "next_local_midnight", "advance_clock", "scheduled_job"):
            raise ValueError("Unsupported operation time semantics")
        if type(self.version) is not int or self.version < 1:
            raise ValueError("Operation version must be a positive integer")
        if (not isinstance(self.required_backend_actions, tuple)
                or any(not isinstance(v, str) or not v for v in self.required_backend_actions)
                or len(set(self.required_backend_actions)) != len(self.required_backend_actions)):
            raise ValueError("Required actions must be unique names in an immutable tuple")
        for fields in (self.parameters, self.results):
            if (not isinstance(fields, tuple) or any(not isinstance(p, Parameter) for p in fields)
                    or len({p.name for p in fields}) != len(fields)):
                raise ValueError("Operation fields must be unique immutable tuples")

    def validate_arguments(self, arguments):
        try:
            self._validate(self.parameters, arguments)
        except (ValueError, TypeError, OverflowError):
            raise OperationRejected("INVALID_ARGUMENTS") from None

    def validate_result(self, result):
        self._validate(self.results, result)

    @staticmethod
    def _validate(fields, values):
        if not isinstance(values, dict) or set(values) != {p.name for p in fields}:
            raise ValueError("Fields do not match operation declaration")
        for parameter in fields:
            parameter.validate(values[parameter.name])

    @staticmethod
    def _schema(fields):
        return {"type": "object", "properties": {p.name: p.schema() for p in fields},
                "required": [p.name for p in fields], "additionalProperties": False}

    def tool_schema(self):
        return {"type": "function", "function": {"name": self.name,
                "description": self.description, "parameters": self._schema(self.parameters)}}

    def manifest(self):
        return {"name": self.name, "version": self.version, "description": self.description,
                "parameters": self._schema(self.parameters), "result": self._schema(self.results),
                "timing": self.timing, "required_backend_actions": list(self.required_backend_actions)}


def receipt_operation(name,description,parameters=(),read=False,timing=None):
    return OperationDefinition(name,description,parameters,(Parameter("receipt","object"),),
        timing or ("read_only" if read else "scheduled_job"), required_backend_actions=("full_season",))

