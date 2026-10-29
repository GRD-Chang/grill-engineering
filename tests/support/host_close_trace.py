"""First occurrence of fixed test stages; original Host command/calls stay intact."""
import json
import os
from pathlib import Path
import time


def install(path: Path) -> int:
    from agent_run.core.operation_runner import OperationRunner
    from agent_run.notifications.sender import NativeNotifications

    # One private, non-inheritable descriptor for this test Host's lifetime.
    # The OS closes it on exit; no pipe or transport is retained by this trace.
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    except TimeoutError:
        raise
    except OSError:
        descriptor = -1
    started, seen = time.monotonic(), set()

    def record(stage):
        if descriptor < 0 or stage in seen:
            return
        seen.add(stage)  # At most one small record per fixed stage, no retry.
        try:
            os.write(descriptor, (json.dumps({'stage': stage, 'seconds': time.monotonic() - started}) + '\n').encode())
        except BaseException as error:
            if isinstance(error, OSError) and not isinstance(error, TimeoutError):
                return
            raise

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
