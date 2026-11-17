"""Explicit Run paths and the existing carrier digest, without a Host/process."""

from support.repository_fixture import create_kernel
from contextlib import closing
from dataclasses import replace
import hashlib
import json
import os
import time

import pytest

from agent_run.runtime.host_control import _runtime_directory
from agent_run.core.kernel import Kernel
from agent_run.errors import KernelError
from agent_run.model import Commit, Delivery, DeliveryKind, HostLaunch, ParentIssue, Requirements, RunSpec
from agent_run.runtime import handoff
from agent_run.storage.run_paths import run_paths
from agent_run.storage.run_scope import RunTransaction
from agent_run.storage.sqlite import Transaction
from support.offline import fixture_inputs
from support.repository_scope import connection, seed
from support.run_scoped_kernel import ScopedKernel


def test_same_database_uses_explicit_run_artifact_and_carrier_partitions(tmp_path):
    database = tmp_path / 'shared.sqlite3'
    with closing(connection(database)) as db:
        db.execute('BEGIN')
        seed(db, 'A'); seed(db, 'B')
        db.commit()
        kernels = {run: ScopedKernel(db, run) for run in ('A', 'B')}
        found = {}
        for run, kernel in kernels.items():
            kernel.storage.path = database
            found[run] = kernel.run_paths(expected_run=run)
            key = hashlib.sha256(run.encode()).hexdigest()
            assert found[run].run_id == run and found[run].host_partition == key
            assert found[run].artifacts == tmp_path / 'runs' / key
            # Journal ownership was already Run-hashed; a shared root is valid.
            assert found[run].journal_root == tmp_path
        assert found['A'].artifacts != found['B'].artifacts
        assert not (tmp_path / 'runs').exists()  # Read-only derivation creates nothing.
        environment = {'XDG_RUNTIME_DIR': str(tmp_path / 'runtime')}
        paths = {run: _runtime_directory('same-action', environment,
            run_partition=found[run].host_partition) for run in ('A', 'B')}
        assert paths['A'] != paths['B']
        for path in paths.values():
            assert path.is_dir() and path.stat().st_mode & 0o777 == 0o700


def test_path_resolution_refuses_foreign_expected_run_and_unselected_owner(tmp_path):
    database = tmp_path / 'B' / 'shared.sqlite3'
    database.parent.mkdir()
    with closing(connection(database)) as db:
        db.execute('BEGIN'); seed(db, 'A'); seed(db, 'B'); db.commit()
        before = tuple(db.iterdump())
        owner = Transaction(db)
        with pytest.raises(KernelError, match='run_scope_required'):
            run_paths(owner, database)
        with pytest.raises(KernelError, match='run_path_binding'):
            run_paths(RunTransaction(owner, 'A'), database, expected_run='B')
        assert tuple(db.iterdump()) == before


def test_current_layout_keeps_paths_but_reads_identity_from_header_not_directory(tmp_path):
    base = Commit('a' * 40, 'b' * 40)
    delivery = Delivery('parent:1', DeliveryKind.PARENT_ONLY, 1, 'r', base,
        'refs/heads/work', 'refs/heads/main')
    spec = RunSpec('recorded-run', 'example/project', 1, 'r', 'g', (delivery,),
        language='zh', repository_root=str(tmp_path / 'checkout'), inputs=fixture_inputs())
    path = tmp_path / 'directory-is-not-the-run-id' / 'kernel.sqlite3'
    kernel = create_kernel(path, spec, requirements=Requirements(ParentIssue(1, 'Parent', 'Requirement'), ()))
    result = kernel.run_paths(expected_run='recorded-run')
    assert result.run_id == 'recorded-run' and result.host_partition == hashlib.sha256(b'recorded-run').hexdigest()
    assert result.artifacts == path.parent / 'runs' / result.host_partition
    assert result.journal_root == path.parent
    with pytest.raises(KernelError, match='run_path_binding'):
        kernel.run_paths(expected_run=path.parent.name)


@pytest.mark.parametrize('partition', ['', '..', '../escape', 'A' * 64, 'z' * 64, 'a' * 63])
def test_invalid_carrier_partition_is_rejected_before_directory_creation(tmp_path, partition):
    with pytest.raises(ValueError, match='Run carrier partition'):
        _runtime_directory('same-action', {'XDG_RUNTIME_DIR': str(tmp_path / 'runtime')},
            run_partition=partition)
    assert not (tmp_path / 'runtime').exists()


def test_existing_carrier_digest_refuses_same_launch_fields_bound_to_another_run(tmp_path):
    lock = tmp_path / 'runner.lock'
    lock.write_bytes(b'')
    lock.chmod(0o600)
    credential = tmp_path / 'environment.json'
    request = HostLaunch('A', 1, 'same-owner', 'same-nonce', 'unit.service',
        ('/controlled/python',), credential)
    receipt = {'nonce': request.nonce, 'request_digest': handoff._digest(request),
        'boot_id': handoff._BOOT.read_text().strip(), 'pid_namespace': os.readlink('/proc/self/ns/pid'),
        'runner': handoff._runner_witness(lock), 'deadline': time.monotonic() + 5}
    credential.write_text(json.dumps({'version': 1, 'runner_lock': str(lock.resolve()),
        'receipt': receipt, 'environment': {'CONTROLLED_FIXTURE_ONLY': '1'}}))
    credential.chmod(0o600)
    before = credential.read_bytes()
    environment, found = handoff._read_carrier(request, credential, lock)
    assert environment == {'CONTROLLED_FIXTURE_ONLY': '1'} and found == receipt
    with pytest.raises(handoff.HandoffError, match='bound elsewhere'):
        handoff._read_carrier(replace(request, run_id='B'), credential, lock)
    assert credential.read_bytes() == before
