"""Executable boundaries for Task creation, inspection and Host composition."""
from __future__ import annotations

import ast
from functools import cache
from pathlib import Path, PurePosixPath


_CONTROL_TYPES = {'Kernel', 'ForegroundHost', 'SystemdHost'}
# These established read-only names also belong to observation data/ports. A
# name alone cannot identify their receiver; actual Kernel types/imports remain
# forbidden. Every other Core method is covered, regardless of its spelling.
_SHARED_READONLY_NAMES = {'status', 'history'}


@cache
def kernel_capabilities() -> frozenset[str]:
    """Read Core's callable surface without importing or constructing it.

    Do not infer authority from method-name prefixes: control, receive, requeue
    and observe_host can all write. New methods are rejected by default, while
    the explicitly shared read-only names keep their legitimate uses.
    """
    source = Path(__file__).resolve().parents[2] / 'src/agent_run/core/kernel.py'
    tree = ast.parse(source.read_text())
    kernel = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'Kernel')
    names = {'inspection_bindings'}
    for member in kernel.body:
        if (isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
                and not (member.name.startswith('__') and member.name.endswith('__'))):
            names.add(member.name)
        elif (isinstance(member, ast.Assign) and isinstance(member.value, ast.Call)
              and isinstance(member.value.func, ast.Name) and member.value.func.id == 'staticmethod'):
            names.update(target.id for target in member.targets if isinstance(target, ast.Name))
    return frozenset(names - _SHARED_READONLY_NAMES)


def observation_violations(relative: str, tree: ast.AST) -> list[str]:
    """Check passed capabilities and concrete types without banning pure helpers."""
    advice = 'use captured InspectionBindings and the runtime.inspection Inspector port'
    forbidden_attributes = kernel_capabilities() | _CONTROL_TYPES
    errors = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in forbidden_attributes:
            errors.append(f'{relative}:{node.lineno}: {node.attr}: {advice}')
        elif isinstance(node, ast.Name) and node.id in _CONTROL_TYPES:
            errors.append(f'{relative}:{node.lineno}: {node.id}: {advice}')
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
              and node.func.id == 'getattr' and len(node.args) > 1
              and isinstance(node.args[1], ast.Constant)
              and isinstance(node.args[1].value, str)
              and node.args[1].value in forbidden_attributes):
            errors.append(f'{relative}:{node.lineno}: {node.args[1].value}: {advice}')
        # Future/string annotations still expose the full controller, even when
        # the code imports its type only elsewhere or has not called it yet.
        if isinstance(node, (ast.arg, ast.AnnAssign)):
            annotation = node.annotation
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            annotation = node.returns
        else:
            continue
        if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
            annotation = ast.parse(annotation.value, mode='eval')
        if annotation is not None:
            for part in ast.walk(annotation):
                if isinstance(part, ast.Name) and part.id in _CONTROL_TYPES:
                    errors.append(f'{relative}:{node.lineno}: {part.id} annotation: {advice}')
                elif isinstance(part, ast.Attribute) and part.attr in _CONTROL_TYPES:
                    errors.append(f'{relative}:{node.lineno}: {part.attr} annotation: {advice}')
    return errors


def imports(tree: ast.AST, module: str) -> list[tuple[int, str]]:
    """Include nested, relative and literal dynamic imports, not just headers."""
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ''
            if node.level:
                base = '.'.join(module.split('.')[:-node.level] + ([base] if base else []))
            found.append((node.lineno, base))
            found.extend((node.lineno, base + '.' + alias.name) for alias in node.names)
        elif isinstance(node, ast.Call) and node.args:
            target = node.func
            dynamic = ((isinstance(target, ast.Name) and target.id in {'__import__', 'import_module'})
                       or isinstance(target, ast.Attribute) and target.attr == 'import_module')
            if dynamic and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                found.append((node.lineno, node.args[0].value))
    return found


def violations(relative: str, source: str) -> list[str]:
    path = PurePosixPath(relative)
    module = 'agent_run.' + '.'.join(path.with_suffix('').parts)
    tree = ast.parse(source)
    rules: list[tuple[tuple[str, ...], str]] = []
    if path.parts[0] == 'storage':
        rules.append((('agent_run.core', 'agent_run.queries'),
                      'Storage only locates and transacts; use runtime.task or queries.run'))
    if relative == 'storage/routing.py' or path.parts[0] == 'core':
        rules.append((('agent_run.adapters.workspace',),
                      'Select the physical Workspace at the runtime application entry'))
    if path.parts[0] == 'core':
        rules.append((('agent_run.runtime.task', 'agent_run.runtime.run_control'),
                      'Core receives explicit creation facts; runtime composes Core'))
    if relative in {'core/task_creation.py', 'runtime/inspection.py'} or path.parts[0] == 'queries':
        rules.append((('agent_run.core.kernel',),
                      'Use Storage and captured InspectionBindings, never a full Kernel'))
    if path.parts[0] == 'queries':
        rules.append((('agent_run.runtime.foreground_host', 'agent_run.adapters.systemd',
                       'agent_run.runtime.host_control', 'agent_run.runtime.run_control'),
                      'Use the read-only runtime.inspection port, not a concrete Host'))
    if path.parts[0] == 'runtime':
        rules.append((('agent_run.cli',), 'Runtime cannot depend on CLI input or presentation'))
    if relative == 'runtime/inspection.py':
        rules.append((('agent_run.storage.sqlite', 'agent_run.storage.routing', 'sqlite3'),
                      'Physical inspection receives captured values, not database access'))
        rules.append((('agent_run.runtime.foreground_host.ForegroundHost',
                       'agent_run.adapters.systemd.SystemdHost'),
                      'Use the runtime.inspection Inspector port, not a concrete Host'))
    if relative in {'cli/lifecycle.py', 'cli/main.py', 'cli/host_exit.py'}:
        rules.append((('agent_run.storage.run_scope', 'agent_run.runtime.foreground_host'),
                      'CLI delegates Host coordination to runtime.run_control'))
        rules.append((('agent_run.adapters.systemd.SystemdHost',),
                      'Construct concrete Host managers in runtime.host_control'))
    errors = []
    for line, dependency in imports(tree, module):
        for forbidden, advice in rules:
            if any(dependency == prefix or dependency.startswith(prefix + '.') for prefix in forbidden):
                errors.append(f'{relative}:{line}: {dependency}: {advice}')
    if path.parts[0] == 'queries' or relative in {'runtime/inspection.py', 'core/task_creation.py'}:
        errors.extend(observation_violations(relative, tree))
    if relative in {'cli/lifecycle.py', 'cli/main.py'}:
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in {'dispatch_host', 'admit_host', 'control_host', 'transaction'}:
                errors.append(f'{relative}:{node.lineno}: {node.attr}: delegate to runtime.run_control')
    if relative == 'runtime/inspection.py':
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in {'storage', 'transaction', '_from_storage', 'admit_host'}:
                errors.append(f'{relative}:{node.lineno}: {node.attr}: inspect captured bindings only')
    return errors
