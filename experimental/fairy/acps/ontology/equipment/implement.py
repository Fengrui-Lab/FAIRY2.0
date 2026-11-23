"""Shared identity and work capability of mounted implements."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ImplementSpec:
    """An independently identified implement compatible with one work capability."""

    object_id: str
    capability: str
