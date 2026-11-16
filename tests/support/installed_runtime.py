"""Refuse source fallback and stale packages before collecting product tests."""
from __future__ import annotations

from importlib.util import find_spec
from pathlib import Path
import sys

import pytest


def require_current_installation() -> None:
    instruction = "Run make test-bootstrap in this checkout's dedicated venv."
    spec = find_spec("agent_run")
    if (spec is None or spec.origin is None or spec.submodule_search_locations is None
            or len(spec.submodule_search_locations) != 1):
        raise pytest.UsageError("Product tests require the installed agent_run package. " + instruction)
    entry = Path(spec.origin)
    installed = Path(spec.submodule_search_locations[0])
    if (not installed.is_absolute() or ".." in installed.parts or entry != installed / "__init__.py"):
        raise pytest.UsageError("Installed package entry and search location must agree. " + instruction)
    if any(path.is_symlink() for path in (entry, installed, *installed.parents)):
        raise pytest.UsageError("Product tests require regular installed runtime paths. " + instruction)
    installed = installed.resolve()
    source = Path(__file__).resolve().parents[2] / "src" / "agent_run"
    if not installed.is_relative_to(Path(sys.prefix).resolve()) or installed == source.resolve():
        raise pytest.UsageError("Product tests refuse a source/editable agent_run package. " + instruction)
    if any(path.is_symlink() for path in installed.rglob("*")):
        raise pytest.UsageError("Product tests require regular installed runtime files. " + instruction)

    def contents(root: Path) -> dict[Path, bytes]:
        return {path.relative_to(root): path.read_bytes() for path in root.rglob("*")
                if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"}

    if contents(installed) != contents(source):
        raise pytest.UsageError("Installed agent_run differs from this checkout's runtime files. " + instruction)
