"""Explicit pre-sealed test Host entry; never the official CLI argv contract."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys
from typing import Mapping

# Only this explicitly selected test entry loads test support. Product source
# is never added to sys.path and the installed package is verified below.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from agent_run.core.kernel import Kernel
from agent_run.runtime import executor
from support.installed_runtime import require_current_installation
from support.sqlite_faults import FaultScope, sqlite_fault


def command(database: Path, action: str, runner_lock: Path,
            environment: Mapping[str, str], manifest: Path, digest: str) -> tuple[str, ...]:
    original = executor.command(database, action, runner_lock, environment)
    index = original.index('-m')
    if original[index + 1] != 'agent_run.runtime.executor':
        raise ValueError('Unknown native Host command')
    return (*original[:index], '-I', str(Path(__file__).resolve()),
            '--fault-scope', str(manifest), '--fault-digest', digest, *original[index + 2:])


def main(arguments=None):
    require_current_installation()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--action', required=True)
    parser.add_argument('--runner-lock', type=Path, required=True)
    parser.add_argument('--fault-scope', type=Path, required=True)
    parser.add_argument('--fault-digest', required=True)
    parsed = parser.parse_args(arguments)
    kernel = Kernel(parsed.database)
    request = kernel.host_request(parsed.action)
    scope = FaultScope.load(parsed.fault_scope, parsed.fault_digest,
                            parsed.database, request.run_id, parsed.action)
    import os
    if (request.backend != 'foreground' or request.command != command(
            parsed.database, parsed.action, parsed.runner_lock, os.environ,
            parsed.fault_scope, parsed.fault_digest)):
        raise ValueError('Test Host command does not match the sealed admission')
    with pytest.MonkeyPatch.context() as patch:
        with sqlite_fault(patch, parsed.database, 'commit-interrupt', scope=scope):
            scope.record('factory_loaded')
            result = executor.main(['--database', str(parsed.database), '--action', parsed.action,
                                    '--runner-lock', str(parsed.runner_lock)])
        scope.record('factory_closed')
        return result


if __name__ == '__main__':
    raise SystemExit(main())
