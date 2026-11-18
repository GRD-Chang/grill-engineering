"""Keep pure rendering and read-only installation capabilities at their owners."""
from __future__ import annotations

import ast
from importlib.util import resolve_name
from pathlib import Path

import pytest


SOURCE = Path(__file__).resolve().parents[1] / "src" / "agent_run"


def _imports(source: str, module: str, *, package: bool = False) -> set[str]:
    """Include function-local imports, aliases and relative package imports."""
    result: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            name = node.module or ""
            if node.level:
                name = resolve_name("." * node.level + name,
                                    module if package else module.rpartition(".")[0])
            result.add(name)
            result.update(f"{name}.{alias.name}" for alias in node.names)
    return result


def _graph() -> dict[str, set[str]]:
    graph = {}
    for path in SOURCE.rglob("*.py"):
        parts = path.relative_to(SOURCE.parent).with_suffix("").parts
        package = parts[-1] == "__init__"
        module = ".".join(parts[:-1] if package else parts)
        graph[module] = _imports(path.read_text(encoding="utf-8"), module, package=package)
    return graph


def _dependency_path(graph: dict[str, set[str]], start: str, forbidden: str) -> list[str]:
    pending = [(start, [start])]
    visited = set()
    while pending:
        module, path = pending.pop()
        if module in visited:
            continue
        visited.add(module)
        for imported in sorted(graph.get(module, ())):
            if imported == forbidden or imported.startswith(forbidden + "."):
                return [*path, imported]
            if imported in graph:
                pending.append((imported, [*path, imported]))
    return []


def test_pure_prompt_renderers_cannot_reach_personal_configuration():
    graph = _graph()
    roots = [name for name in graph if name.startswith("agent_run.core.prompts")]
    roots.append("agent_run.contracts.prompt")
    for root in roots:
        path = _dependency_path(graph, root, "agent_run.config")
        assert not path, (
            f"{' -> '.join(path)}: resolve language and resources explicitly at "
            "cli.prompts/config.creation; pure renderers use contracts.prompt.resource "
            "with the frozen snapshot"
        )


def test_installation_diagnostics_use_public_readonly_owners():
    graph = _graph()
    for caller, forbidden in (
        ("setup", "doctor"), ("setup", "installer"), ("doctor", "installer"),
        ("diagnostics", "doctor"), ("diagnostics", "setup"), ("diagnostics", "installer"),
        ("layout", "doctor"), ("layout", "setup"), ("layout", "installer"),
    ):
        path = _dependency_path(graph, f"agent_run.install.{caller}", f"agent_run.install.{forbidden}")
        assert not path, (
            f"{' -> '.join(path)}: use install.diagnostics.run_bounded_probe "
            "and install.layout.read_active; installer owns activation and recovery"
        )


@pytest.mark.parametrize("source,module,package,expected", [
    ("def render():\n from agent_run.config.resources import resolve_resources as load\n",
     "agent_run.core.prompts.role", False, "agent_run.config.resources"),
    ("def probe():\n from . import doctor as diagnostic\n",
     "agent_run.install.setup", False, "agent_run.install.doctor"),
    ("import agent_run.install.installer as activation\n",
     "agent_run.install.doctor", False, "agent_run.install.installer"),
    ("from ...config import resources\n",
     "agent_run.core.prompts", True, "agent_run.config.resources"),
])
def test_dependency_check_includes_local_relative_and_aliased_imports(source, module, package, expected):
    assert expected in _imports(source, module, package=package)


def test_dependency_check_follows_helpers_instead_of_only_renderer_imports():
    graph = {
        "renderer": {"helper"},
        "helper": {"agent_run.config.resources.resolve_resources"},
    }
    assert _dependency_path(graph, "renderer", "agent_run.config") == [
        "renderer", "helper", "agent_run.config.resources.resolve_resources",
    ]
