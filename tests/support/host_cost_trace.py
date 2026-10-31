"""One native-test run's synchronous costs; no arguments, bodies or sampling thread."""
from contextlib import contextmanager, nullcontext
from functools import wraps
import json
import os
from pathlib import Path
import stat
import sqlite3
import sys
import threading
import time
from support.native_cpu_facts import snapshot as cpu_snapshot
from time import thread_time_ns


class HostCosts:
    def __init__(self, root):
        self.cpu_start = cpu_snapshot(cgroup=True)
        self.directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            info = os.fstat(self.directory)
            if info.st_uid != os.geteuid() or info.st_mode & 0o077:
                raise ValueError('Private test directory required')
        except BaseException as original:
            descriptor, self.directory = self.directory, -1
            try:
                os.close(descriptor)
            except BaseException as cleanup:
                # Watchdogs and interruptions outrank ordinary diagnostic errors.
                # If both are control exceptions, retain the first one.
                if (isinstance(original, (TimeoutError, InterruptedError)) or not isinstance(original, Exception)
                        or isinstance(cleanup, Exception) and not isinstance(cleanup, (TimeoutError, InterruptedError))):
                    raise original from cleanup
                raise cleanup from original
            raise
        self.name = str(time.monotonic_ns()) + '.json'
        self.started = time.monotonic_ns()
        self.lock, self.publish_lock, self.local = threading.Lock(), threading.Lock(), threading.local()
        self.totals, self.active, self.cpu_totals = {}, {}, {}
        self.errors = self.flushes = self.flush_ns = 0
        self.skipped_flushes = 0
        self.reader_totals, self.guard_calls = {}, 0
        self.request_identity = self.execution_identity = None
        self.identity_conflict = False
        self.runtime = {'python': sys.version.split()[0], 'sqlite': sqlite3.sqlite_version}

    @contextmanager
    def entry(self, category):
        previous = getattr(self.local, 'category', 'other')
        self.local.category = category
        try:
            if category == 'guard/preparation':
                with self.lock:
                    self.guard_calls += 1
            yield
        finally:
            self.local.category = previous

    @staticmethod
    def _reader_owner(tx):
        from agent_run.storage.run_scope import RunTransaction
        from agent_run.storage.sqlite import Transaction
        while isinstance(tx, RunTransaction):
            tx = vars(tx)['_transaction']
        return tx if isinstance(tx, Transaction) else None

    @contextmanager
    def reader(self, original):
        """Observe one public read context; no query, scope or budget calls."""
        frame = getattr(self.local, 'reader', None)
        outer = frame is None
        if outer:
            started, cpu = time.monotonic_ns(), thread_time_ns()
            frame = {'category': getattr(self.local, 'category', 'other'), 'depth': 0,
                     'max_depth': 0, 'owners': {}, 'missing': {},
                     'trace': int(sys.gettrace() is not None), 'profile': int(sys.getprofile() is not None)}
            self.local.reader = frame
        frame['depth'] += 1
        frame['max_depth'] = max(frame['max_depth'], frame['depth'])
        failed = ended = False
        try:
            with original as tx:
                yield tx
            ended = True
            # The original context has exited successfully. Keep each owner only
            # until the outer observation ends, including shared Run views.
            owner = self._reader_owner(tx)
            if owner is None:
                frame['missing']['owner_unavailable'] = frame['missing'].get('owner_unavailable', 0) + 1
            else:
                frame['owners'][id(owner)] = owner
        except BaseException:
            failed = True
            reason = 'observation_failed' if ended else 'context_failed'
            frame['missing'][reason] = frame['missing'].get(reason, 0) + 1
            raise
        finally:
            frame['depth'] -= 1
            if outer:
                self.local.reader = None
                try:
                    elapsed, used_cpu = time.monotonic_ns() - started, thread_time_ns() - cpu
                    rows = size = owners = 0
                    if not failed:
                        for owner in frame['owners'].values():
                            fields = vars(owner)
                            r, b = fields.get('rows_read'), fields.get('bytes_read')
                            if type(r) is not int or type(b) is not int or min(r, b) < 0:
                                frame['missing']['counters_unavailable'] = frame['missing'].get('counters_unavailable', 0) + 1
                            else:
                                owners += 1
                                rows += r
                                size += b
                    with self.lock:
                        total = self.reader_totals.setdefault(frame['category'],
                            {'calls': 0, 'failed': 0, 'wall_ns': 0, 'thread_ns': 0, 'max_depth': 0,
                             'owners': 0, 'rows': 0, 'bytes': 0, 'missing': {}, 'trace_entries': 0, 'profile_entries': 0})
                        for key, value in (('calls', 1), ('failed', int(failed)), ('wall_ns', elapsed),
                                           ('thread_ns', used_cpu), ('owners', owners), ('rows', rows), ('bytes', size),
                                           ('trace_entries', frame['trace']), ('profile_entries', frame['profile'])):
                            total[key] += value
                        total['max_depth'] = max(total['max_depth'], frame['max_depth'])
                        for reason, count in frame['missing'].items():
                            total['missing'][reason] = total['missing'].get(reason, 0) + count
                except BaseException:
                    if not failed:
                        raise
                finally:
                    frame['owners'].clear()

    def host_request(self, action, request):
        identity = (action, request.run_id, request.owner, request.epoch, request.nonce)
        with self.lock:
            if self.request_identity is not None and self.request_identity != identity:
                self.identity_conflict = True
            if self.request_identity is None:
                self.request_identity = identity

    def execution(self, execution):
        identity = (execution.run_id, execution.owner, execution.epoch)
        with self.lock:
            if self.execution_identity is not None and self.execution_identity != identity:
                self.identity_conflict = True
            if self.execution_identity is None:
                self.execution_identity = identity

    def _reader_snapshot(self):
        # Caller holds the existing aggregate lock. Association is not a nonce
        # handshake proof; missing/conflicting observations never select a winner.
        request, execution = self.request_identity, self.execution_identity
        associated = (not self.identity_conflict and request is not None and execution is not None
                      and request[1:4] == execution)
        return {'runtime': self.runtime, 'guard_calls': self.guard_calls,
                'coverage': {'guard/preparation': ['Kernel.guard_agent'],
                             'state/supervise': ['OperationRunner.state', 'OperationRunner.supervise'],
                             'operation/accept': ['OperationRunner.run_once'], 'Status': [], 'other': []},
                'totals': {key: {**value, 'missing': value['missing'].copy()} for key, value in self.reader_totals.items()},
                'association': {'request': request, 'execution': execution, 'conflict': self.identity_conflict,
                                'request_matches_execution': associated, 'nonce_handshake_proven': False},
                'scope': 'Outermost reader only per thread; parallel wall overlaps. Rows/bytes are final cumulative counters of successful owners only; missing is explicit. Tags cover listed entries, not all work. Status is untagged. Trace/profile describe entry only. Host request matching is association, not physical nonce proof.'}

    @contextmanager
    def span(self, phase, *, publish=False):
        started = time.monotonic_ns()
        cpu_started = thread_time_ns()
        row = [phase, started, 0, cpu_started, 0]
        with self.lock:
            stack = getattr(self.local, 'stack', [])
            stack.append(row)
            self.local.stack = stack
            self.active[threading.get_ident()] = stack
            self.totals.setdefault(phase, [0, 0, 0, 0, 0])[0] += 1
            self.cpu_totals.setdefault(phase, [0, 0])
        failed = False
        try:
            if publish:
                self.flush()
            yield
        except BaseException:
            failed = True
            raise
        finally:
            # An original exception always wins over a diagnostic failure.
            try:
                elapsed = time.monotonic_ns() - started
                cpu_elapsed = thread_time_ns() - cpu_started
                with self.lock:
                    total = self.totals[phase]
                    total[1] += 1
                    total[2] += int(failed)
                    total[3] += elapsed
                    total[4] += max(0, elapsed - row[2])
                    self.cpu_totals[phase][0] += cpu_elapsed
                    self.cpu_totals[phase][1] += max(0, cpu_elapsed - row[4])
                    stack.pop()
                    if stack:
                        stack[-1][2] += elapsed
                        stack[-1][4] += cpu_elapsed
                    else:
                        self.active.pop(threading.get_ident(), None)
                if publish:
                    self.flush()
            except BaseException:
                if not failed:
                    raise

    def flush(self):
        started = time.monotonic_ns()
        if not self.publish_lock.acquire(blocking=False):
            with self.lock:
                self.skipped_flushes += 1
            return
        errors = 0
        failure = None
        try:
            cpu_now = cpu_snapshot()
            with self.lock:
                now = time.monotonic_ns()
                snapshot = {'started_ns': self.started, 'snapshot_ns': now, 'status': 'snapshot',
                    'columns': ['started', 'completed', 'raised', 'inclusive_ns', 'exclusive_ns'],
                    'totals': {phase: row.copy() for phase, row in self.totals.items()},
                    'cpu_columns': ['inclusive_thread_ns', 'exclusive_thread_ns'],
                    'cpu_totals': {phase: row.copy() for phase, row in self.cpu_totals.items()},
                    'cpu_start': self.cpu_start, 'cpu_snapshot': cpu_now,
                    'readers': self._reader_snapshot(),
                    'active': [[{'phase': phase, 'started_ns': begin, 'elapsed_ns': now - begin}
                                for phase, begin, _, _, _ in stack] for stack in self.active.values()],
                    'measurement_errors': self.errors, 'flushes': self.flushes, 'flush_ns': self.flush_ns,
                    'skipped_flushes': self.skipped_flushes}
            # Serialization and every file operation are outside the aggregate
            # lock. Concurrent publishers skip instead of holding business work.
            payload = json.dumps(snapshot, separators=(',', ':')).encode()
            if len(payload) > 16384:
                errors += 1
                payload = json.dumps({'started_ns': self.started, 'snapshot_ns': now, 'status': 'oversized',
                    'measurement_errors': snapshot['measurement_errors'] + errors,
                    'skipped_flushes': snapshot['skipped_flushes'],
                    'totals_omitted': len(snapshot['totals']), 'active_omitted': len(snapshot['active'])},
                    separators=(',', ':')).encode()
            fd = -1
            temporary = self.name + '.tmp'
            try:
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                             0o600, dir_fd=self.directory)
                offset = 0
                while offset < len(payload):
                    written = os.write(fd, payload[offset:])
                    if written <= 0:
                        raise OSError('Cost write made no progress')
                    offset += written
                descriptor, fd = fd, -1  # close may release it even when it reports failure.
                os.close(descriptor)
                os.replace(temporary, self.name, src_dir_fd=self.directory, dst_dir_fd=self.directory)
            except (TimeoutError, InterruptedError) as error:
                failure = error
                raise
            except OSError:
                errors += 1
            except BaseException as error:
                failure = error
                raise
            finally:
                try:
                    if fd >= 0:
                        descriptor, fd = fd, -1
                        try:
                            os.close(descriptor)
                        except (TimeoutError, InterruptedError):
                            raise
                        except OSError:
                            errors += 1
                    try:
                        os.unlink(temporary, dir_fd=self.directory)
                    except (TimeoutError, InterruptedError):
                        raise
                    except OSError as error:
                        errors += not isinstance(error, FileNotFoundError)
                except BaseException as cleanup:
                    if failure is not None:
                        if (isinstance(failure, (TimeoutError, InterruptedError)) or not isinstance(failure, Exception)
                                or isinstance(cleanup, Exception) and not isinstance(cleanup, (TimeoutError, InterruptedError))):
                            raise failure from cleanup
                        raise cleanup from failure
                    raise
        except BaseException as error:
            failure = error
            raise
        finally:
            try:
                try:
                    with self.lock:
                        self.errors += errors
                        self.flushes += 1
                        self.flush_ns += time.monotonic_ns() - started
                finally:
                    self.publish_lock.release()
            except BaseException as cleanup:
                if failure is not None:
                    if (isinstance(failure, (TimeoutError, InterruptedError)) or not isinstance(failure, Exception)
                            or isinstance(cleanup, Exception) and not isinstance(cleanup, (TimeoutError, InterruptedError))):
                        raise failure from cleanup
                    raise cleanup from failure
                raise


def observe_host(setattr_, costs):
    """Time existing calls; totals overlap only through explicitly reported nesting."""
    from agent_run.adapters.remote import RemoteEffects
    from agent_run.adapters import github_commands
    from agent_run.core.operation_runner import OperationRunner
    from agent_run.runtime import codex, git_command, process_io
    from agent_run.storage.sqlite import Storage
    from agent_run.core.kernel import Kernel
    from agent_run.adapters.local_git import LocalEffects

    def wrap(owner, name, phase, *, publish=False, category=None):
        original = getattr(owner, name)
        @wraps(original)
        def call(*args, **kwargs):
            label = phase(*args, **kwargs) if callable(phase) else phase
            with costs.entry(category) if category else nullcontext():
                with costs.span(label, publish=publish):
                    return original(*args, **kwargs)
        setattr_(owner, name, call)

    for name, phase in (('source', 'source'), ('delivery', 'delivery'),
                        ('source_observation', 'source'), ('complete_observation', 'delivery')):
        wrap(RemoteEffects, name, phase, publish=True)
    wrap(OperationRunner, 'run_once', 'operation', publish=True, category='operation/accept')
    for name in ('state', 'supervise'):
        wrap(OperationRunner, name, 'core.' + name, category='state/supervise')
    guard = Kernel.guard_agent
    @wraps(guard)
    def guarded(*args, **kwargs):
        with costs.entry('guard/preparation'):
            return guard(*args, **kwargs)
    setattr_(Kernel, 'guard_agent', guarded)
    request = Kernel.host_request
    @wraps(request)
    def requested(self, action_id):
        result = request(self, action_id)
        costs.host_request(action_id, result)
        return result
    setattr_(Kernel, 'host_request', requested)
    operations = set(LocalEffects.OPERATIONS) | set(Kernel.REMOTE)
    for name in ('execute', 'reconcile'):
        wrap(RemoteEffects, name, lambda self, effect, *_a, _name=name, **_kw:
             'effect.' + _name + '.' + (effect.operation if effect.operation in operations else 'other'))
    # sitecustomize runs before runpy loads the official executor as __main__.
    # Importing it here would execute a second module and miss its real globals.
    observed = set()
    def observe_executor():
        for module in (sys.modules.get('agent_run.runtime.executor'), sys.modules.get('__main__')):
            if (module is not None and getattr(getattr(module, '__spec__', None), 'name', None)
                    == 'agent_run.runtime.executor' and id(module) not in observed):
                observed.add(id(module))
                wrap(module._Executor, 'agent', 'worker', publish=True)
                wrap(module, 'wait', 'host.future_wait', publish=True)
    init = OperationRunner.__init__
    @wraps(init)
    def initialized(*args, **kwargs):
        observe_executor()
        result = init(*args, **kwargs)
        costs.execution(args[0].execution)
        return result
    setattr_(OperationRunner, '__init__', initialized)
    observe_executor()  # The two existing pre-sealed entries already imported it.
    wrap(git_command, '_require_waitable_children', 'git.waitable_probe')
    wrap(github_commands, '_run_bounded_command',
         lambda arguments, **_kw: 'command.git_remote' if arguments[0] == 'git' else 'command.gh')
    wrap(codex, '_execute', 'codex.execute', publish=True)

    # Existing modules bind these exact functions at import time. Replace only
    # those aliases; no Popen, pipe ownership, command construction or reaper patch.
    for original, phase in ((git_command.run_git, 'command.git_local'), (process_io.pump, 'pipe.pump')):
        @wraps(original)
        def call(*args, _original=original, _phase=phase, **kwargs):
            with costs.span(_phase):
                return _original(*args, **kwargs)
        for module in tuple(sys.modules.values()):
            if module is not None and getattr(module, '__name__', '').startswith('agent_run.'):
                for name, value in tuple(vars(module).items()):
                    if value is original:
                        setattr_(module, name, call)

    for name in ('transaction', '_transaction'):
        original = getattr(Storage, name)
        @contextmanager
        @wraps(original)
        def transaction(*args, _original=original, _name=name, **kwargs):
            kind = 'writer' if kwargs.get('write', True) else 'reader'
            if _name == '_transaction':
                kind = 'metadata' if kwargs.get('select_run') is False else 'business_' + kind
            with costs.span('sqlite.' + kind):
                original_context = _original(*args, **kwargs)
                observed_context = costs.reader(original_context) if kind == 'reader' else original_context
                with observed_context as tx:
                    yield tx
        setattr_(Storage, name, transaction)
    wrap(Storage, '_save_metadata', lambda self, request, identity, cancelled, operation, *_a, **_kw:
         'fifo.' + (operation if operation in {'register', 'reconcile', 'discard'} else 'other'))
    pause = Storage._save_pause
    @wraps(pause)
    def waited(*args, **kwargs):
        with costs.span('fifo.retry_wait'):
            return pause(*args, **kwargs)
    setattr_(Storage, '_save_pause', staticmethod(waited))


def install_from_environment():
    root = os.environ.get('ENTRY_HOST_COST_ROOT')
    if root:
        try:
            costs = HostCosts(root)
        except (TimeoutError, InterruptedError):
            raise
        except OSError:
            return
        observe_host(setattr, costs)
        costs.flush()


def read_costs(directory):
    """Private, bounded snapshots only; atomic replace prevents torn JSON reads."""
    rows = []
    root = directory / 'host-costs'
    observed, paths, total = time.monotonic_ns(), [], 0
    for path in root.glob('*.json'):
        total += 1
        paths.append(path)
        paths.sort(key=lambda item: int(item.stem), reverse=True)
        del paths[4:]  # At most five names in memory; read only the latest four Hosts.
    cli_paths, cli_total, cli_rows = [], 0, []
    for path in root.glob('cli-*.jsonl'):
        cli_total += 1
        cli_paths.append((path.stat().st_mtime_ns, path))
        cli_paths.sort(reverse=True)
        del cli_paths[4:]
    for path, cli in [(path, False) for path in paths] + [(path, True) for _, path in cli_paths]:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        failure = None
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid()
                    or info.st_mode & 0o077 or info.st_size > 16384):
                raise ValueError('Unsafe cost evidence')
            if cli:
                content = os.read(fd, 8193)
                cli_rows.append({'file': path.name, 'truncated': len(content) > 8192,
                                 'complete_line': content.endswith(b'\n'),
                                 'trace': content[:8192].decode('utf-8', errors='replace')})
            else:
                rows.append(json.loads(os.read(fd, 16385)))
        except BaseException as error:
            failure = error
            raise
        finally:
            try:
                os.close(fd)
            except BaseException as cleanup:
                if failure is not None:
                    if (isinstance(failure, (TimeoutError, InterruptedError)) or not isinstance(failure, Exception)
                            or isinstance(cleanup, Exception) and not isinstance(cleanup, (TimeoutError, InterruptedError))):
                        raise failure from cleanup
                    raise cleanup from failure
                raise
    read_at = time.monotonic_ns()
    for row in rows:
        row['snapshot_age_ns'] = read_at - row['snapshot_ns']
    return {'failure_observed_ns': observed, 'snapshot_age_observed_ns': read_at,
            'hosts': rows, 'omitted_hosts': max(0, total - len(paths)),
            'reader_cpu': cpu_snapshot(cgroup=True),
            'cli_returns': cli_rows, 'omitted_cli_returns': max(0, cli_total - len(cli_paths)),
            'scope': 'CPU totals count only the calling thread, not child processes. Process CPU spans all Host threads; waited_children counts only reaped children. Fixed /sys/fs/cgroup paths expose mount-root counters; they do not establish current-process or job scope. Matching file identity supports differences for the same object only; do not assign those differences to a story. Thread schedstat is sampled only on the publishing thread. Function-call wall times; inclusive spans overlap, exclusive spans exclude same-thread children; concurrent threads overlap. Active is the last synchronous snapshot. Startup before installation and shutdown after the last flush are unmeasured. Age requires comparable process monotonic clocks; negative age is not comparable.'}


def note_costs(error, directory, *, arrivals_ns=(), process=None):
    # Called at the original failure, before slower DB diagnostics and cleanup.
    try:
        if os.environ.get('ENTRY_HOST_COST_ROOT') != str(directory / 'host-costs'):
            return
        observed = time.monotonic_ns()
        child = None if process is None else {'pid': process.pid, 'returncode': process.poll(),
                                             'observed_ns': observed}
        value = read_costs(directory)
        value['barrier_arrivals_ns'] = list(arrivals_ns)[:2]
        value['child_before_diagnostics'] = child
        error.add_note('Native Host costs: ' + json.dumps(value, separators=(',', ':')))
    except BaseException:
        pass  # The explicitly supplied original failure remains primary.


def report_costs(record_property, directory):
    if os.environ.get('ENTRY_HOST_COST_ROOT') != str(directory / 'host-costs'):
        return
    try:
        value = read_costs(directory)
    except (TimeoutError, InterruptedError):
        raise
    except (OSError, ValueError) as unavailable:
        value = {'unavailable': type(unavailable).__name__}
    record_property('native_host_costs', json.dumps(value, separators=(',', ':')))
