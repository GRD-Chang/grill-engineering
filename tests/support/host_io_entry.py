"""Pre-sealed diagnostic Host variant; not the official entry E2E contract."""
from __future__ import annotations
import argparse
from functools import partial
import math
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from agent_run.runtime import executor
from agent_run.core.kernel import Kernel
from support.installed_runtime import require_current_installation
from support.host_io_trace import HostIOTrace, observe_host

_native_command = executor.command


def command(database, action, lock, environment, *, run_id, root, sample_at):
    if not root.is_absolute() or not math.isfinite(sample_at) or sample_at <= 0:
        raise ValueError('Exact diagnostic location and sample time required')
    original = _native_command(database, action, lock, environment, run_id=run_id)
    index = original.index('-m')
    if original[index + 1] != 'agent_run.runtime.executor':
        raise ValueError('Unknown native Host command')
    return (*original[:index], '-I', str(Path(__file__).resolve()), '--trace-host',
            '--trace-root', str(root), '--sample-at', repr(sample_at), *original[index + 2:])


def main(arguments=None):
    require_current_installation()
    arguments = list(sys.argv[1:] if arguments is None else arguments)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--trace-host', action='store_true')
    parser.add_argument('--trace-root', type=Path, required=True)
    parser.add_argument('--sample-at', type=float, required=True)
    parser.add_argument('--database', type=Path)
    parser.add_argument('--action')
    parser.add_argument('--run-id')
    parser.add_argument('--runner-lock', type=Path)
    separator = arguments.index('--') if '--' in arguments else len(arguments)
    parsed = parser.parse_args(arguments[:separator])
    if not parsed.trace_root.is_absolute() or not math.isfinite(parsed.sample_at) or parsed.sample_at <= 0:
        raise ValueError('Exact diagnostic location and sample time required')
    if not parsed.trace_host:
        cli = arguments[separator + 1:]
        if not cli or cli[0] not in {'resume', 'approve'}:
            raise ValueError('This diagnostic variant only observes original Resume/Approve')
        if root := os.environ.get('ENTRY_HOST_COST_ROOT'):
            from support.host_close_trace import install_cli
            install_cli(Path(root) / ('cli-' + str(os.getpid()) + '.jsonl'))
        from agent_run.cli.main import main as public_main
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(executor, 'command', partial(command, root=parsed.trace_root, sample_at=parsed.sample_at))
            return public_main(cli)
    if parsed.database is None or parsed.action is None or parsed.run_id is None or parsed.runner_lock is None:
        raise ValueError('Exact admitted Host identity required')
    request = Kernel(parsed.database, run_id=parsed.run_id).host_request(parsed.action)
    expected = command(parsed.database, parsed.action, parsed.runner_lock, os.environ,
                       run_id=parsed.run_id, root=parsed.trace_root, sample_at=parsed.sample_at)
    if request.backend != 'foreground' or request.command != expected or request.run_id != parsed.run_id:
        raise ValueError('Diagnostic command does not match the sealed admission')
    from support.host_cost_trace import install_from_environment
    install_from_environment()
    trace = HostIOTrace(parsed.trace_root, parsed.action, parsed.run_id, parsed.sample_at)
    try:
        with pytest.MonkeyPatch.context() as patch:
            observe_host(patch, trace, parsed.database)
            trace.timer.start()
            return executor.main(['--database', str(parsed.database), '--action', parsed.action,
                '--runner-lock', str(parsed.runner_lock), '--run-id=' + parsed.run_id])
    finally:
        trace.close()


if __name__ == '__main__':
    raise SystemExit(main())
