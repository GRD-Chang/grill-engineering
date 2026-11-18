"""First occurrence of fixed test stages; original Host command/calls stay intact."""
from agent_run.runtime import host_control
import json
import os
from pathlib import Path
import sys
import time
from support.native_cpu_facts import snapshot as cpu_snapshot


def _recorder(path: Path, *, cli=False):
    # One private, non-inheritable descriptor for this test Host's lifetime.
    # The OS closes it on exit; no pipe or transport is retained by this trace.
    cpu_start = cpu_snapshot(cgroup=True) if cli else None
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    except (TimeoutError, InterruptedError):
        raise
    except OSError:
        descriptor = -1
    started, seen = time.monotonic(), set()

    def record(stage, **fields):
        if descriptor < 0 or stage in seen:
            return
        seen.add(stage)  # At most one small record per fixed stage, no retry.
        try:
            row = {'stage': stage, 'seconds': time.monotonic() - started}
            if cli:
                row.update(pid=os.getpid(), monotonic_ns=time.monotonic_ns(),
                           cpu_start=cpu_start, cpu_snapshot=cpu_snapshot(), **fields)
            os.write(descriptor, (json.dumps(row) + '\n').encode())
        except BaseException as error:
            if isinstance(error, OSError) and not isinstance(error, (TimeoutError, InterruptedError)):
                return
            raise

    return descriptor, record


def install(path: Path) -> int:
    from agent_run.core.operation_runner import OperationRunner
    from agent_run.notifications.sender import NativeNotifications

    descriptor, record = _recorder(path)

    run_once, close = OperationRunner.run_once, NativeNotifications.close

    def observed_run(self, *, agent_capacity=1):
        step = run_once(self, agent_capacity=agent_capacity)
        if step.kind in {'progress', 'waiting', 'paused', 'approval', 'complete', 'quiescent', 'preparing'}:
            record('run_once_return_' + step.kind)
        return step

    def observed_close(self, *args, **kwargs):
        try:
            record('notification_close_enter')
        except BaseException as entry_error:
            # Cleanup still runs once. Preserve the first diagnostic control
            # exception, chaining a simultaneous close failure as its cause.
            try:
                close(self, *args, **kwargs)
            except BaseException as close_error:
                raise entry_error from close_error
            raise
        result = close(self, *args, **kwargs)
        record('notification_close_return')
        return result

    OperationRunner.run_once, NativeNotifications.close = observed_run, observed_close
    return descriptor  # Controlled tests close it explicitly; Host owns its process lifetime.


def install_cli(path: Path) -> int:
    """Fixed normal-return boundaries; active exceptions bypass new probes."""
    from agent_run.cli import main as cli_main
    from agent_run.runtime import host_control
    from agent_run.runtime.foreground_host import ForegroundHost

    descriptor, record = _recorder(path, cli=True)
    launch = ForegroundHost.launch
    submit, notify = host_control.launch_admitted, host_control.notify_committed
    main = cli_main.main

    def observed_launch(self, kernel, request, *args, **kwargs):
        result = launch(self, kernel, request, *args, **kwargs)
        record('foreground_launch_return', run=request.run_id, nonce=request.nonce)
        return result

    def observed_main(*args, **kwargs):
        result = main(*args, **kwargs)
        record('cli_return', result=result if type(result) is int else None)
        return result

    def observed_submit(*args, **kwargs):
        if sys.exception() is not None:
            return submit(*args, **kwargs)
        result = submit(*args, **kwargs)
        record('submit_return')
        return result

    def observed_notify(*args, **kwargs):
        if sys.exception() is not None:
            return notify(*args, **kwargs)
        record('notify_enter')
        result = notify(*args, **kwargs)
        record('notify_return')
        return result

    ForegroundHost.launch = observed_launch
    host_control.launch_admitted, host_control.notify_committed = observed_submit, observed_notify
    cli_main.main = observed_main
    return descriptor


def raise_with_trace(error: BaseException, directory: Path, children) -> None:
    """Always rethrow this failure; diagnostics cannot resume or replace it."""
    try:
        facts = {'child_poll': [child.poll() for child in children]}
        try:
            with (directory / 'host-return-hook' / 'close-trace.jsonl').open('rb') as stream:
                facts['trace'] = stream.read(4096).decode('utf-8', errors='replace')
        except BaseException as unavailable:
            facts['trace_unavailable'] = type(unavailable).__name__
        error.add_note('Host close boundary: ' + json.dumps(facts))
    finally:
        raise error
