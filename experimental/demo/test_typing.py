"""Static contracts are tested with valid and intentionally invalid consumers."""

from pathlib import Path
import subprocess
import sys
import pytest

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).parent / "typing_contracts"


def check(name):
    pytest.importorskip("mypy")
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "mypy",
            "--config-file",
            str(ROOT / "experimental/pyproject.toml"),
            str(FIXTURES / name),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )


def test_valid_consumers_keep_concrete_factories_and_structured_results():
    result = check("positive.py")
    assert result.returncode == 0, result.stdout + result.stderr


def test_wrong_backends_weather_and_agent_privilege_are_rejected():
    result = check("negative.py")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "Found 6 errors" in result.stdout
    assert "[arg-type]" in result.stdout
    assert "[attr-defined]" in result.stdout
    assert "[typeddict-item]" in result.stdout
