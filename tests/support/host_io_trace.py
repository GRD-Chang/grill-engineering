"""Bounded observation for one explicitly selected test Host, never a scheduler."""
from collections import deque
from contextlib import contextmanager
from functools import wraps
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys
import threading
import time


class HostIOTrace:
    def __init__(self, root: Path, action: str, run: str, sample_at: float):
        import re
        if not re.fullmatch(r'[a-zA-Z0-9_-]{1,128}', action):
            raise ValueError('Exact test action required')
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            info = os.fstat(directory)
            if info.st_uid != os.geteuid() or info.st_mode & 0o077:
                raise ValueError('Private diagnostic directory required')
            self.fd = os.open(action + '.json', os.O_CREAT | os.O_EXCL | os.O_WRONLY |
                              os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=directory)
        finally:
            os.close(directory)
        self.run, self.action, self.sample_at = run, action, sample_at
        self.rows, self.current = deque(maxlen=32), {}
        self.local, self.lock = threading.local(), threading.Lock()
        self.sample, self.errors = None, 0
        self.timer = threading.Timer(max(0, sample_at-time.monotonic()), self.sample_once)
        self.timer.name, self.timer.daemon = 'test-io-sample', True

    @contextmanager
    def span(self, phase, *, effect=None, invocation=None):
        stack, row, boundary = [], None, 'end'
        try:
            stack = getattr(self.local, 'stack', [])
            inherited = stack[-1] if stack else {}
            row = {'run': self.run, 'effect': effect or inherited.get('effect'),
                   'invocation': invocation or inherited.get('invocation'),
                   'thread': threading.get_ident(), 'phase': phase, 'monotonic_ns': time.monotonic_ns()}
            with self.lock:
                stack.append(row)
                self.local.stack = stack
                self.current[row['thread']] = stack[-16:].copy()
                self.rows.append(row | {'boundary': 'begin'})
        except BaseException:
            self.errors += 1
        try:
            yield
        except BaseException:
            boundary = 'raise'
            raise
        finally:
            try:
                if row is not None:
                    with self.lock:
                        self.rows.append(row | {'boundary': boundary, 'monotonic_ns': time.monotonic_ns()})
                        if stack and stack[-1] is row:
                            stack.pop()
                        if stack:
                            self.current[row['thread']] = stack[-16:].copy()
                        else:
                            self.current.pop(row['thread'], None)
            except BaseException:
                self.errors += 1

    def snapshot(self):
        with self.lock:
            now = time.monotonic_ns()
            return {'run': self.run, 'action': self.action, 'monotonic_ns': now, 'sample_at_ns': int(self.sample_at * 1e9),
                    'sample_status': ('sampled' if self.sample.get('complete') else 'sample_incomplete') if self.sample is not None else
                        'not_due' if now < self.sample_at * 1e9 else 'not_sampled', 'ring': list(self.rows),
                    'current': {str(key): value.copy() for key, value in self.current.items()},
                    'stack_sample': self.sample, 'measurement_errors': self.errors}

    def flush(self):
        try:
            payload = json.dumps(self.snapshot(), separators=(',', ':')).encode()
            if len(payload) > 65536:
                self.errors += 1
                return
            # Only the sampling thread writes while the Host runs. Final flush
            # happens after its bounded join; no trace lock crosses file I/O.
            os.lseek(self.fd, 0, os.SEEK_SET)
            os.ftruncate(self.fd, 0)
            offset = 0
            while offset < len(payload):
                written = os.write(self.fd, payload[offset:])
                if written <= 0:
                    raise OSError('Diagnostic write made no progress')
                offset += written
        except BaseException:
            self.errors += 1

    def sample_once(self):
        try:
            with self.lock:
                if self.sample is not None:
                    return
                self.sample = {'monotonic_ns': time.monotonic_ns(), 'threads': [], 'complete': False}
            frames = sys._current_frames()
            threads = []
            for thread in threading.enumerate():
                if thread is not threading.main_thread() and not thread.name.startswith('agent-run'):
                    continue
                frame, stack = frames.get(thread.ident), []
                while frame is not None and len(stack) < 12:
                    stack.append({'file': Path(frame.f_code.co_filename).name[:128],
                                  'function': frame.f_code.co_name[:128], 'line': frame.f_lineno})
                    frame = frame.f_back
                threads.append({'thread': thread.ident, 'name': thread.name, 'frames': stack})
            with self.lock:
                self.sample['threads'] = threads[:8]
                self.sample['current'] = {str(key): value.copy() for key, value in self.current.items()}
                self.sample['complete'] = True
            self.flush()
        except BaseException:
            self.errors += 1

    def close(self):
        try:
            self.timer.cancel()
            if self.timer.ident is not None:
                self.timer.join(timeout=1)
            if not self.timer.is_alive():
                self.flush()
                os.close(self.fd)
        except BaseException:
            self.errors += 1


def read_traces(root: Path, run: str | None):
    """Only this fixture's bounded regular files; no process or database probe."""
    result = []
    try:
        paths = sorted(root.glob('*.json'))[:3]
    except BaseException as error:
        return [{'unavailable': type(error).__name__}]
    for path in paths:
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
            try:
                info = os.fstat(descriptor)
                if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid()
                        or info.st_mode & 0o077 or info.st_size > 65536):
                    raise ValueError('Unsafe trace')
                value = json.loads(os.read(descriptor, 65537))
                if value['run'] != run or value['action'] != path.stem:
                    raise ValueError('Foreign trace')
                result.append(value)
            finally:
                os.close(descriptor)
        except BaseException as error:
            result.append({'unavailable': type(error).__name__})
    if len(json.dumps(result).encode()) > 65536:
        return [{'unavailable': 'aggregate_byte_limit'}]
    return result


def observe_host(patch, trace: HostIOTrace, database: Path):
    """Wrap original calls once; no new SQL, probe, retry or decision."""
    from agent_run.core.operation_runner import OperationRunner
    from agent_run.adapters.remote import RemoteEffects
    from agent_run.adapters.local_git import LocalEffects
    from agent_run.adapters import cleanup
    from agent_run.runtime.codex import _CodexCapture
    from agent_run.storage.sqlite import Storage
    from agent_run.runtime import git_command

    def wrap(owner, name, label, identities=lambda *_a, **_kw: {}):
        original = getattr(owner, name)
        @wraps(original)
        def call(*args, **kwargs):
            try:
                fields = identities(*args, **kwargs)
                phase = label(*args, **kwargs) if callable(label) else label
            except BaseException:
                trace.errors += 1
                fields = {}
                phase = 'observation.unavailable'
            with trace.span(phase, **fields):
                return original(*args, **kwargs)
        patch.setattr(owner, name, call)
    wrap(OperationRunner, 'run_once', 'run_once')
    for owner in (RemoteEffects, LocalEffects):
        for name in ('execute', 'reconcile'):
            wrap(owner, name, lambda _self, effect, *_a, _name=name, **_kw: 'adapter.' + _name + '.' + effect.operation,
                 lambda _self, effect, *_a, **_kw: {'effect': effect.effect_id})
    wrap(_CodexCapture, '_close', 'capture.close',
         lambda capture: {'invocation': capture.call.invocation_id if capture.call is not None else None})
    original_hold = cleanup._hold_ref
    @contextmanager
    def hold_ref(*args, **kwargs):
        with trace.span('git.update-ref.lock'):
            with original_hold(*args, **kwargs):
                yield
    patch.setattr(cleanup, '_hold_ref', hold_ref)

    original_transaction = Storage._transaction
    @contextmanager
    def transaction(storage, **kwargs):
        old = getattr(trace.local, 'sqlite_kind', None)
        trace.local.sqlite_kind = 'metadata' if kwargs.get('select_run') is False else 'business' if kwargs['write'] else 'read'
        try:
            with original_transaction(storage, **kwargs) as tx:
                yield tx
        finally:
            trace.local.sqlite_kind = old
    patch.setattr(Storage, '_transaction', transaction)

    class Connection(sqlite3.Connection):
        kind = 'read'
        def commit(self):
            with trace.span('sqlite.' + self.kind + '.commit'):
                return super().commit()
        def close(self):
            with trace.span('sqlite.' + self.kind + '.close'):
                return super().close()
    connect = sqlite3.connect
    def observed_connect(path, *args, **kwargs):
        if str(path).split('?', 1)[0] not in {str(database), database.as_uri()} or 'factory' in kwargs or len(args) >= 5:
            return connect(path, *args, **kwargs)
        connection = connect(path, *args, factory=Connection, **kwargs)
        connection.kind = getattr(trace.local, 'sqlite_kind', None) or 'read'
        if str(path).endswith('?mode=ro') and connection.kind != 'read':
            connection.kind += '.read'
        return connection
    patch.setattr(sqlite3, 'connect', observed_connect)

    original_git = git_command.run_git
    @wraps(original_git)
    def git(arguments, *args, **kwargs):
        index = 1
        while index < len(arguments) and arguments[index].startswith('-'):
            index += 2 if arguments[index] in {'-C', '-c', '--git-dir', '--work-tree'} else 1
        verb = arguments[index] if index < len(arguments) else 'unknown'
        allowed = {'config', 'rev-parse', 'fetch', 'push', 'merge-tree', 'worktree', 'update-ref',
                   'cat-file', 'show-ref', 'rev-list', 'status', 'diff', 'log', 'merge-base', 'show'}
        with trace.span('git.' + (verb if verb in allowed else 'other')):
            return original_git(arguments, *args, **kwargs)
    for module in tuple(sys.modules.values()):
        if module is not None and getattr(module, '__name__', '').startswith('agent_run.'):
            if vars(module).get('run_git') is original_git:
                patch.setattr(module, 'run_git', git)
