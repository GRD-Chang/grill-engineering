"""Keep extracted delivery capabilities independent of the full controller."""
from __future__ import annotations

import ast
from pathlib import Path
import sys


CAPABILITIES = (
    'core/repair.py', 'core/delivery_state.py', 'core/delivery_checks.py',
    'core/attempt_continuation.py', 'core/attempt_lifecycle.py',
    'contracts/review_evidence.py',
)
FORBIDDEN = ('agent_run.core.kernel', 'agent_run.core.operation_runner')


def violations(root: Path) -> list[str]:
    errors = []
    for relative in CAPABILITIES:
        path = root / relative
        for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ''
                if node.level:
                    package = ['agent_run', *Path(relative).parent.parts]
                    module = '.'.join([*package[:len(package) - node.level + 1], module]).rstrip('.')
                names = [module, *(f'{module}.{alias.name}' for alias in node.names)]
                if module == 'agent_run.core' and any(alias.name == '*' for alias in node.names):
                    names.append('agent_run.core.kernel')
            if any(name == forbidden or name.startswith(forbidden + '.')
                   for name in names for forbidden in FORBIDDEN):
                errors.append(f'{relative}:{node.lineno}: capability cannot import Kernel or OperationRunner; '
                              'pass transaction facts and return a delivery/continuation plan')
    runner = root / 'core/operation_runner.py'
    for node in ast.walk(ast.parse(runner.read_text(), filename=str(runner))):
        if isinstance(node, ast.Attribute) and node.attr == '_wait_for_read':
            errors.append(f'core/operation_runner.py:{node.lineno}: use Kernel.wait_for_read '
                          'with the original ReadFence/work token and Execution')
    return errors


def main() -> int:
    root = Path(__file__).resolve().parents[2] / 'src/agent_run'
    errors = violations(root)
    if errors:
        print('\n'.join(errors), file=sys.stderr)
        return 1
    print('Core capability boundaries passed')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
