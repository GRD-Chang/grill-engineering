"""First occurrence of fixed test stages; original Host command/calls stay intact."""
import json
import faulthandler
import math
import os
from pathlib import Path
import re
import time


def arm_snapshot(path: Path, when: str | None) -> int:
    """One test-only C watchdog dump; no signal handler or execution deadline."""
    if when is None:
        return -1
    try:
        delay = float(when) - time.monotonic()
        if not math.isfinite(delay):
            return -1
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    except TimeoutError:
        raise
    except (OSError, ValueError):
        return -1
    # Keep this private descriptor for the process lifetime, including an arm
    # failure: a possibly registered C watchdog must never see a reused fd.
    try:
        faulthandler.dump_traceback_later(max(0.001, delay), repeat=False, file=descriptor, exit=False)
    except TimeoutError:
        raise
    except (OSError, ValueError):
        pass
    return descriptor


def snapshot_frames(path: Path) -> dict:
    """Whitelist frame coordinates only; discard paths, addresses and other text."""
    with path.open('rb') as stream:
        raw = stream.read(32769)
    rows, used, truncated = [], 0, len(raw) > 32768
    for line in raw[:32768].decode('ascii', errors='replace').splitlines():
        match = re.fullmatch(r'  File "([^"]+)", line ([0-9]{1,9}) in ([A-Za-z0-9_<>]{1,80})', line)
        if match:
            filename = match[1].replace('\\', '/').rsplit('/', 1)[-1]
            if not re.fullmatch(r'[A-Za-z0-9_.-]{1,80}', filename):
                filename = '<module>'
            row = f'{filename}:{match[2]} in {match[3]}'
        elif line.startswith(('Thread ', 'Current thread ')):
            row = 'Thread'
        else:
            continue
        used += len(row) + 1
        if used > 12000:
            truncated = True
            break
        rows.append(row)
    return {'frames': rows, 'truncated': truncated}


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
    arm_snapshot(path.with_name('stack.txt'), os.environ.get('ENTRY_HOST_SNAPSHOT_AT'))
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
        try:
            facts['stack'] = snapshot_frames(directory / 'host-return-hook' / 'stack.txt')
        except BaseException as unavailable:
            facts['stack_unavailable'] = type(unavailable).__name__
        error.add_note('Host close boundary: ' + json.dumps(facts))
    finally:
        raise error
