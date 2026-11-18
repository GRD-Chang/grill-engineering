"""Foreground Host admission transport; the existing executor owns all work.

The calling CLI is the single-domain supervisor. Its shared Runner lease is
held throughout settlement; a durable non-secret physical record protects the
remaining gap if that supervisor is killed. Nothing here selects business work.
"""
from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass, replace
import errno
import json
import math
import os
from pathlib import Path
import select
import shutil
import socket
import struct
import stat
import sys
import time
from typing import Any, Literal, Mapping

from agent_run.adapters.foreground import (DomainUnknown, NamespaceProcess, ProcessPin, observer_anchor,
    read_record, record_root, recover_init, subreaper, write_record)
from agent_run.core.kernel import Kernel
from agent_run.errors import KernelError, bounded_error
from agent_run.model import Execution, HostLaunch, HostObservation
from agent_run.contracts.host import valid_foreground_domain
from agent_run.runtime.handoff import _digest, _encode, _environment, _runner_witness
from agent_run.runtime.package_lease import RunnerLease


_CHANNEL = 'AGENT_RUN_FOREGROUND_CHANNEL'
_LEASE = 'AGENT_RUN_FOREGROUND_LEASE'


@dataclass(frozen=True)
class HostInspection:
    """Evidence for this exact request, never a current-Run or execution grant."""
    request: HostLaunch
    observation: HostObservation
    source: Literal['local', 'recorded_exit', 'unavailable']


@dataclass(frozen=True)
class WorkerInspection:
    execution: Execution
    invocation_id: str
    output_ordinal: int
    disposition: Literal['running', 'exited', 'unknown']
    source: Literal['local', 'recorded_exit', 'host_exit', 'unavailable']


def _unavailable(request: HostLaunch) -> HostInspection:
    return HostInspection(request, HostObservation('unknown', None, backend='foreground'), 'unavailable')


def _observation(request: HostLaunch, record: Mapping[str, Any], disposition: Literal['running', 'exited'], *, released: bool = False) -> HostObservation:
    domain = record['domain']
    executor = domain['executor']
    return HostObservation(disposition, None, boot_id=domain['observer']['boot_id'],
        main_pid=None if executor is None else executor['pid'],
        start_token=None if executor is None else executor['start_token'],
        exit_status=record.get('executor_exit_code'), ended_at=record.get('ended_at'),
        evidence=_encode(record['receipt']).decode(), usage_ready=disposition == 'running',
        usage_released=released, backend='foreground', domain=domain,
        namespace_exit_status=record.get('namespace_exit_code'), exit_evidence=record.get('exit_basis', ''))


def _validate_record(request: HostLaunch, record: Mapping[str, Any]) -> None:
    """One protection-record contract for Host and local Worker observations."""
    phase, domain = record.get('phase'), record.get('domain')
    if (not isinstance(phase, str) or phase not in {'pending', 'running', 'exited'}
            or not isinstance(domain, dict) or not valid_foreground_domain(domain, request.nonce)):
        raise DomainUnknown('Foreground protection state is invalid')
    if phase == 'exited':
        ended = record.get('ended_at')
        try:
            valid = (record.get('exit_basis') in {'retained_namespace_pidfd', 'same_observer_init_exit'}
                and isinstance(ended, (int, float)) and not isinstance(ended, bool)
                and math.isfinite(ended) and ended > 0
                and all(value is None or type(value) is int for value in
                        (record.get('executor_exit_code'), record.get('namespace_exit_code'))))
        except OverflowError:
            valid = False
        if not valid:raise DomainUnknown('Foreground exit evidence is invalid')


def _bound_record(request: HostLaunch, lock: Path, database: Path) -> tuple[Path, dict[str, Any]]:
    path = record_root(lock) / (request.nonce + '.json')
    record = read_record(path)
    receipt = record['receipt']
    if (receipt['request_digest'] != _digest(request) or receipt['nonce'] != request.nonce
            or receipt['runner'] != _runner_witness(lock)
            or record['binding'] != [request.run_id, request.owner, request.epoch, request.nonce]
            or record.get('database') != str(database.resolve())):
        raise DomainUnknown('Foreground record belongs to another launch or Runner')
    _validate_record(request, record)
    return path, record


def _inspect_record(request: HostLaunch, original: HostObservation | None,
             record: Mapping[str, Any]) -> HostInspection:
    """The caller has authenticated and validated this private record."""
    domain = record['domain']
    if original is not None and original.domain is not None and original.domain != domain:
        return _unavailable(request)
    if record['phase'] == 'exited':
        return HostInspection(request, _observation(request, record, 'exited'), 'recorded_exit')
    disposition = recover_init(domain)
    if disposition == 'exited':
        # Discovery proves domain closure, never an exact end timestamp.
        return HostInspection(request, replace(_observation(request, record, 'exited'),
            ended_at=None, exit_status=None, namespace_exit_status=None,
            exit_evidence='same_observer_init_exit'), 'local')
    if (record['phase'] == 'running' and disposition == 'running' and original is not None
            and original.usage_ready and original.domain == domain and domain['executor'] is not None):
        pin = ProcessPin.open(domain['executor']['pid'], domain['executor'])
        try:
            if pin.observe() == 'running' and recover_init(domain) == 'running':
                return HostInspection(request, replace(_observation(request, record, 'running'),
                    ended_at=None, exit_status=None, namespace_exit_status=None), 'local')
        finally:pin.close()
    return _unavailable(request)


def _settle(kernel: Kernel, request: HostLaunch, path: Path, record: dict[str, Any]) -> HostObservation:
    # A durable exact exit receipt precedes Core retirement. Failure leaves the
    # management barrier intact; a later observer may retry this same settlement.
    _validate_record(request, record)
    observation = _observation(request, record, 'exited', released=True)
    if kernel.observe_host(request, observation) != 'exited':
        raise DomainUnknown('Core did not accept the original foreground exit')
    _remove_record(path)
    return observation


def _remove_record(path: Path) -> None:
    path.unlink(missing_ok=True)
    _sync_record_directory(path.parent)


def _sync_record_directory(root: Path) -> None:
    before = root.lstat()
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        actual = os.fstat(directory)
        if (not stat.S_ISDIR(actual.st_mode) or actual.st_uid != os.geteuid() or actual.st_mode & 0o077
                or (actual.st_dev, actual.st_ino) != (before.st_dev, before.st_ino)):
            raise DomainUnknown('Foreground protection directory changed')
        os.fsync(directory)
        after = root.lstat()
        if (after.st_dev, after.st_ino) != (actual.st_dev, actual.st_ino):
            raise DomainUnknown('Foreground protection directory changed')
    finally:os.close(directory)


def _confirm_record_removed(path: Path) -> None:
    """Complete the original directory sync without deleting or recreating a record."""
    def absent() -> None:
        try:path.lstat()
        except OSError as error:
            if error.errno != errno.ENOENT:raise
        else:raise DomainUnknown('Foreground protection record still exists')

    before = path.parent.lstat()
    absent()
    _sync_record_directory(path.parent)
    absent()
    after = path.parent.lstat()
    if (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino):
        raise DomainUnknown('Foreground protection directory changed')


class ForegroundHost:
    backend = 'foreground'

    def __init__(self, *, environment: Mapping[str, str]) -> None:
        self.environment = dict(environment)

    def preflight(self) -> None:
        if shutil.which('bwrap', path=self.environment.get('PATH')) is None or not hasattr(os, 'pidfd_open'):
            raise DomainUnknown('Foreground execution requires bubblewrap and Linux pidfd')
        observer_anchor()

    def observe(self, kernel: Kernel, request: HostLaunch, lock: Path) -> HostObservation:
        if request.backend != 'foreground':raise KernelError('host_backend_changed')
        original = kernel.host_usage(kernel.host_action(request))
        try:
            if original is not None and original.disposition == 'exited' and original.usage_released:
                try:path, record = _bound_record(request, lock, kernel.storage.path)
                except FileNotFoundError:
                    _confirm_record_removed(record_root(lock) / (request.nonce + '.json'))
                    return original
                if (record['phase'] != 'exited'
                        or _observation(request, record, 'exited', released=True) != original):
                    raise DomainUnknown('Physical closure does not match committed exit settlement')
                _remove_record(path)
                return original
            path, record = _bound_record(request, lock, kernel.storage.path)
            if record.get('domain') is None:
                raise DomainUnknown('Original namespace identity was not durably established')
            if record['phase'] != 'exited':
                observed = _inspect_record(request, original, record).observation
                if observed.disposition != 'exited':return observed
                record.update(phase='exited', ended_at=kernel.clock(), executor_exit_code=None,
                              namespace_exit_code=None, exit_basis='same_observer_init_exit')
                write_record(path, record)
            return _settle(kernel, request, path, record)
        except (OSError, ValueError, KeyError, DomainUnknown):
            return HostObservation('unknown', None, backend='foreground')

    def reconcile_closed(self, kernel: Kernel, lock: Path) -> None:
        """Finish only an already committed exit whose final unlink was lost."""
        try:root = record_root(lock)
        except FileNotFoundError:return
        run_id = kernel.run_paths().run_id
        for path in root.glob('*.json'):
            record = read_record(path)
            if record.get('database') != str(kernel.storage.path.resolve()):continue
            binding = record.get('binding')
            if (not isinstance(binding, list) or len(binding) != 4
                    or not isinstance(binding[0], str) or not binding[0]):
                raise DomainUnknown('Foreground protection has no exact Run binding')
            # A shared database is not a Run identity. Preserve sibling records
            # for their own closure and the existing global management barrier.
            if binding[0] != run_id:continue
            action = record['action']
            original = kernel.host_usage(action)
            if original is not None and original.disposition == 'exited' and original.usage_released:
                if self.observe(kernel, kernel.host_request(action), lock).disposition != 'exited':
                    raise DomainUnknown('Committed foreground exit still has unclosed protection')
        _sync_record_directory(root)

    def launch(self, kernel: Kernel, request: HostLaunch, *, lock: Path, lease: RunnerLease) -> HostObservation:
        self.preflight()
        if request.backend != 'foreground':raise KernelError('host_backend_changed')
        held, selected = os.fstat(lease.fileno()), lock.stat()
        if (held.st_dev, held.st_ino) != (selected.st_dev, selected.st_ino):
            raise DomainUnknown('The original Runner lease was not retained')
        path = record_root(lock, create=True) / (request.nonce + '.json')
        anchor = observer_anchor()
        receipt = {'request_digest': _digest(request), 'nonce': request.nonce,
                   'runner': _runner_witness(lock), 'boot_id': anchor['boot_id'],
                   'pid_namespace': anchor['pid_namespace']}
        record: dict[str, Any] = {'binding': [request.run_id, request.owner, request.epoch, request.nonce],
                                 'receipt': receipt, 'phase': 'pending', 'domain': None,
                                 'database': str(kernel.storage.path.resolve()), 'action': kernel.host_action(request)}
        write_record(path, record, create=True)
        if not kernel.dispatch_host(request):
            path.unlink()  # No process creation was attempted.
            return HostObservation('unknown', None, backend='foreground')
        kernel.observe_host(request, HostObservation('unknown', None, evidence=_encode(receipt).decode(),
                                                     usage_pending=True, backend='foreground'))
        left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        left.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
        domain: NamespaceProcess | None = None
        executor: ProcessPin | None = None
        settled: HostObservation | None = None
        with subreaper():
            try:
                environment = _environment(self.environment)
                environment[_CHANNEL] = str(right.fileno())
                environment[_LEASE] = str(lease.fileno())
                domain = NamespaceProcess(request.command, environment, pass_fds=(right.fileno(), lease.fileno()))
                right.close()
                assert domain.init is not None
                # Persist original init before allowing the executor bootstrap.
                assert domain.monitor_pin is not None
                record['monitor'] = domain.monitor_pin.identity.wire()
                record['init'] = domain.init.identity.wire()
                record['observer'] = anchor
                record['domain'] = {'nonce': request.nonce, 'observer': anchor,
                                    'init': domain.init.identity.wire(), 'executor': None}
                write_record(path, record)
                domain.release()
                if not select.select([left], [], [], 5)[0]:raise DomainUnknown('Executor handshake expired')
                body, ancillary, flags, _ = left.recvmsg(16384, socket.CMSG_SPACE(struct.calcsize('3i')))
                credentials = [struct.unpack('3i', data) for level, kind, data in ancillary
                               if level == socket.SOL_SOCKET and kind == socket.SCM_CREDENTIALS]
                if (not body or flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC)
                        or len(credentials) != 1 or credentials[0][1] != os.geteuid()):
                    raise DomainUnknown('Unbound foreground executor sender')
                executor = ProcessPin.open(credentials[0][0])
                hello = json.loads(body)
                if (hello != {'request_digest': _digest(request), 'pid': executor.identity.namespace_pids[-1],
                              'namespace': executor.identity.pid_namespace}
                        or executor.identity.pid_namespace != domain.init.identity.pid_namespace):
                    raise DomainUnknown('Executor is outside the original foreground namespace')
                record.update(phase='running', domain={'nonce': request.nonce, 'observer': anchor,
                    'init': domain.init.identity.wire(), 'executor': executor.identity.wire()})
                write_record(path, record)
                running = _observation(request, record, 'running')
                if kernel.observe_host(request, running) != 'running':raise DomainUnknown('Host readiness rejected')
                kernel.authorize_host(request)
                if (_runner_witness(lock) != receipt['runner']
                        or (os.fstat(lease.fileno()).st_dev, os.fstat(lease.fileno()).st_ino) != (held.st_dev, held.st_ino)):
                    raise DomainUnknown('The original Runner lease changed before release')
                left.sendall(b'1')  # EOF, socket closure and mere readability are not permission.
                assert domain.monitor_pin is not None
                assert domain.process is not None and domain.process.stderr is not None
                while domain.monitor_pin.observe() != 'exited':
                    select.select([domain.monitor_pin.descriptor, domain.process.stderr], [], [], 1)
                    diagnostic = domain.diagnostic()
                    if diagnostic:print(bounded_error(diagnostic), file=sys.stderr)
                domain.finish(5)
                executor.wait_exit(3)
            finally:
                original = sys.exception()
                errors: list[BaseException] = []
                left.close()
                right.close()
                if domain is not None:
                    try:domain.close()
                    except BaseException as error:errors.append(error)
                    # close records a retained-init proof even if another
                    # cleanup step failed. Neither a quiet close nor Popen's
                    # return code is accepted as namespace-exit evidence.
                    if (domain.result is not None and domain.result.get('namespace_exited') is True
                            and record['domain'] is not None):
                        try:
                            record.update(phase='exited', ended_at=kernel.clock(),
                                executor_exit_code=(domain.result['executor_exit_code']
                                    if record['domain']['executor'] is not None else None),
                                namespace_exit_code=domain.result['namespace_exit_code'],
                                exit_basis='retained_namespace_pidfd')
                            write_record(path, record)
                            settled = _settle(kernel, request, path, record)
                        except BaseException as error:errors.append(error)
                if executor is not None:
                    try:executor.close()
                    except BaseException as error:errors.append(error)
                if errors:
                    first = original if original is not None else errors.pop(0)
                    for cleanup_error in errors:first.add_note('Foreground cleanup: ' + str(cleanup_error))
                    if original is None:raise first
        if settled is None:raise DomainUnknown('Foreground namespace exit remains unproven')
        return settled


def execute_foreground(kernel: Kernel, action: str) -> int:
    """Consume only the inherited one-shot channel, then use the original loop."""
    request = kernel.host_request(action)
    if request.backend != 'foreground':raise KernelError('host_backend_changed')
    descriptor = int(os.environ.pop(_CHANNEL))
    lease_descriptor = int(os.environ.pop(_LEASE))
    with socket.socket(fileno=descriptor) as channel:
        channel.settimeout(5)
        channel.sendall(_encode({'request_digest': _digest(request), 'pid': os.getpid(),
                                'namespace': os.readlink('/proc/self/ns/pid')}))
        if channel.recv(2) != b'1':raise DomainUnknown('Foreground bootstrap was not authorized')
    observation = kernel.host_usage(action)
    if observation is None or observation.domain is None or not observation.usage_ready:
        raise DomainUnknown('Original foreground usage readiness is unavailable')
    expected = observation.domain['executor']
    receipt = json.loads(observation.evidence)
    held = os.fstat(lease_descriptor)
    if ((held.st_dev, held.st_ino) != (receipt['runner']['device'], receipt['runner']['inode'])
            or _runner_witness(Path(receipt['runner']['lock'])) != receipt['runner']):
        raise DomainUnknown('Executor did not inherit the admitted Runner lease')
    from agent_run.runtime.process_identity import _process_start_token
    if (os.getpid(), _process_start_token(os.getpid()), os.readlink('/proc/self/ns/pid')) != (
            expected['namespace_pids'][-1], expected['start_token'], expected['pid_namespace']):
        raise DomainUnknown('This executor is not the admitted foreground process')
    kernel.authorize_host(request)
    from agent_run.runtime.executor import run
    return run(kernel, Execution(request.run_id, request.owner, request.epoch), dict(os.environ))
