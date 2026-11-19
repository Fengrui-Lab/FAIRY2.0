"""Portable identity of the actual packaged executable sources and data."""

from __future__ import annotations
from typing import TYPE_CHECKING, TypedDict
from importlib.resources.abc import Traversable
import hashlib
from importlib.resources import files
from fairy.events.records import fingerprint


class SourceIdentity(TypedDict):
    """当前打包源码与数据的SHA-256清单；不代表科学校准。"""

    algorithm: str
    fingerprint: str
    files: dict[str, str]


def source_identity() -> SourceIdentity:
    """计算当前打包源码/数据清单及SHA-256，拒绝将不同实现当作同一实验。"""
    manifest: dict[str, str] = {}

    def visit(node: Traversable, path: str) -> None:
        for child in sorted(node.iterdir(), key=lambda item: item.name):
            name = path + "/" + child.name
            if child.is_dir() and child.name != "__pycache__":
                visit(child, name)
            elif child.is_file() and child.name.endswith((".py", ".json", ".yaml")):
                manifest[name] = hashlib.sha256(child.read_bytes()).hexdigest()

    for package in ("fairy", "evaluate", "interface"):
        visit(files(package), package)
    return {
        "algorithm": "sha256-packaged-sources-v1",
        "fingerprint": fingerprint(manifest),
        "files": manifest,
    }
