"""Real Core cleanup intents through the existing scoped transaction seam.

Private Git and synthetic external ports; no native save clock, file admission,
Host, or Worker proof. Intent persistence is distinct from deletion authority.
"""
from dataclasses import replace
from contextlib import closing, ExitStack
from pathlib import Path
import sqlite3

import pytest

from support.effect_boundary import EffectBoundary, effect_boundary, effect_state
from support import offline
from support.offline import Scenario
from support.run_scoped_kernel import ScopedKernel
from agent_run.core.operation_contract import ObserveIO, ReadUnavailable, RequirementsRead
from agent_run.errors import KernelError
from agent_run.model import DeliveryKind, Issue, ReadWait
from agent_run.storage.sqlite import Transaction


@pytest.fixture
def cleanup_scenario(tmp_path, monkeypatch):
    # Keep this file's fixture local; shared code comes from support, not a
    # test module. The existing ScopedKernel seam owns real transactions.
    create = offline.create_kernel
    with ExitStack() as owned:
        connections = []

        def producer(*args, **kwargs):
            original = create(*args, **kwargs)
            db = owned.enter_context(closing(sqlite3.connect(original.storage.path, isolation_level=None)))
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA foreign_keys=ON')
            connections.append(db)
            kernel = ScopedKernel(db, original.storage.run_id)
            kernel.storage.path, kernel.storage.run_id = original.storage.path, original.storage.run_id
            return kernel

        monkeypatch.setattr(offline, 'create_kernel', producer)
        s = Scenario(tmp_path / 'scenario', DeliveryKind.PARENT_ONLY)
        assert len(connections) == 1
        yield s, connections[0]


def test_first_local_intents_need_no_source_or_delivery_read(cleanup_scenario, monkeypatch):
    s, db = cleanup_scenario
    make_ports = s.make_ports
    preparation_reads, physical_reads = [], []

    def ports_for(binding):
        ports = make_ports(binding)
        source, delivery, execute = ports.source, ports.delivery, ports.execute
        active = []

        def audit(kind):
            state = s.kernel.status(s.delivery.key)
            cleanup = state['resource_cleanup']
            if (state['phase'] == 'complete' and cleanup and cleanup['stage'] < 2
                    and cleanup['effect_id'] is None):
                preparation_reads.append((cleanup['stage'], kind))
            if active:
                physical_reads.append((active[-1], kind))

        def read_source(parent):
            audit('source')
            return source(parent)

        def read_delivery(request):
            audit('delivery')
            return delivery(request)

        def write(effect, authority):
            if effect.operation not in {'cleanup_worktree', 'cleanup_local_ref'}:
                return execute(effect, authority)
            active.append(effect.operation)
            try:
                return execute(effect, authority)
            finally:
                active.pop()

        ports.source, ports.delivery, ports.execute = read_source, read_delivery, write
        return ports

    monkeypatch.setattr(s, 'make_ports', ports_for)
    assert s.run(approve=True).kind == 'complete'
    assert preparation_reads == []
    assert physical_reads == [('cleanup_worktree', 'source')] * 3 + [('cleanup_local_ref', 'source')] * 2
    rows = db.execute("SELECT * FROM effects WHERE operation IN ('cleanup_worktree','cleanup_local_ref') ORDER BY rowid").fetchall()
    assert [row['operation'] for row in rows] == ['cleanup_worktree', 'cleanup_local_ref']
    closed = db.execute("SELECT * FROM effects WHERE operation='close' AND status='confirmed'").fetchone()
    workspace = db.execute("SELECT * FROM effects WHERE operation='workspace' AND status='confirmed'").fetchone()
    assert closed is not None and workspace is not None
    for row in rows:
        assert (row['status'], row['attempts'], row['actor']) == ('confirmed', 1, None)
        assert all(row[key] == closed[key] for key in ('source_ref', 'target_ref', 'base_sha', 'base_tree'))
    assert rows[1]['before_sha'] == workspace['before_sha']
    assert not Path(s.delivery.checkout).exists()
    assert s.port.ref_commit(s.delivery.source_ref) is None
    assert s.kernel.status(s.delivery.key)['resource_cleanup']['state'] == 'complete'


@pytest.fixture
def pending_local_cleanup(cleanup_scenario, monkeypatch, request):
    s, db = cleanup_scenario
    operation = getattr(request, 'param', 'cleanup_worktree')
    make_ports, captured = s.make_ports, []

    def ports_for(binding):
        ports = make_ports(binding)
        captured[:] = [ports]
        return ports

    monkeypatch.setattr(s, 'make_ports', ports_for)
    with effect_boundary(s, monkeypatch, operation):
        with pytest.raises(EffectBoundary) as held:
            s.run(approve=True)
    effect, authority = held.value.effect, held.value.authorize
    assert authority is not None
    assert effect_state(s, effect)[:2] == ('prepared', 0)
    assert s.kernel.status(effect.subject)['resource_cleanup']['effect_id'] == effect.effect_id
    reentry = s.kernel.next_work(s.execution)
    assert isinstance(reentry, ObserveIO) and isinstance(reentry.request, RequirementsRead)
    assert reentry.work.cleanup and reentry.work.subject == effect.subject
    return s, db, effect, authority, captured[0]


@pytest.mark.parametrize('pending_local_cleanup', ['cleanup_worktree', 'cleanup_local_ref'], indirect=True)
@pytest.mark.parametrize('missing', [False, True], ids=['present', 'already_absent'])
def test_source_403_keeps_prepared_intent_without_dispatch_or_completion(pending_local_cleanup, monkeypatch, missing):
    s, db, effect, authority, ports = pending_local_cleanup
    git = s.port.git
    if missing:
        if effect.operation == 'cleanup_worktree':
            git.run('worktree', 'remove', effect.payload['checkout'])
        else:
            git.run('update-ref', '-d', effect.source_ref, effect.before_sha)
    before = effect_state(s, effect)
    cleanup = s.kernel.status(effect.subject)['resource_cleanup']
    checkout_exists = Path(s.delivery.checkout).exists()
    local_ref = s.port.ref_commit(effect.source_ref)

    def denied(parent):
        raise ReadUnavailable('github_read_failed', 'Source403')

    monkeypatch.setattr(ports, 'source', denied)
    with pytest.raises(ReadWait) as caught:
        s.port.execute(effect, authority)
    assert caught.value.step.kind == 'waiting'
    assert effect_state(s, effect) == before and before[:2] == ('prepared', 0)
    assert s.kernel.status(effect.subject)['resource_cleanup'] == cleanup
    assert cleanup['state'] == 'pending'
    assert Path(s.delivery.checkout).exists() == checkout_exists
    assert s.port.ref_commit(effect.source_ref) == local_ref


@pytest.mark.parametrize('change', ['stop', 'owner', 'source'])
def test_real_intent_is_not_authority_after_revocation(pending_local_cleanup, monkeypatch, change):
    s, db, effect, authority, ports = pending_local_cleanup
    if change == 'source':
        source = ports.source

        def changed(parent):
            original = source(parent)
            return replace(original, parent=replace(original.parent, sub_issue_numbers=(2,)),
                           issues=(Issue(2, 'New ticket', 'New scope', 'OPEN', frozenset({'ready-for-agent'})),))

        monkeypatch.setattr(ports, 'source', changed)
    else:
        s.kernel.control('stop', 'cleanup-stop')
        if change == 'owner':
            s.kernel.confirm_executor_exit(s.execution)
            s.kernel.control('resume', 'cleanup-resume')
            s.kernel.claim_executor('replacement-owner')
    with pytest.raises(KernelError):
        s.port.execute(effect, authority)
    assert effect_state(s, effect)[:2] == ('prepared' if change == 'source' else 'cancelled', 0)
    assert Path(s.delivery.checkout).exists()
    assert s.kernel.status(effect.subject)['resource_cleanup']['stage'] == 0
    if change == 'source':
        assert s.kernel.source_status()['blocked'] == 'unsupported_scope_change'


@pytest.mark.parametrize('pending_local_cleanup', ['cleanup_worktree', 'cleanup_local_ref'], indirect=True)
def test_resource_replacement_is_preserved_by_the_original_cas(pending_local_cleanup):
    s, db, effect, authority, ports = pending_local_cleanup
    git = s.port.git
    if effect.operation == 'cleanup_worktree':
        git.run('commit', '--allow-empty', '-m', 'Foreign worktree head', cwd=Path(s.delivery.checkout))
        replacement = git.run('rev-parse', 'HEAD', cwd=Path(s.delivery.checkout)).stdout.strip()
        assert replacement != effect.before_sha
    else:
        replacement = s.port.remote_commit(effect.source_ref).sha
        assert replacement != effect.before_sha  # Local marker and published head differ.
        git.run('update-ref', effect.source_ref, replacement, effect.before_sha)
    assert s.port.execute(effect, authority).disposition == 'third'
    assert effect_state(s, effect)[:2] == ('prepared', 0)
    assert s.kernel.status(effect.subject)['resource_cleanup']['state'] == 'pending'
    if effect.operation == 'cleanup_worktree':
        assert git.run('rev-parse', 'HEAD', cwd=Path(s.delivery.checkout)).stdout.strip() == replacement
    else:
        assert s.port.ref_commit(effect.source_ref).sha == replacement


@pytest.mark.parametrize('pending_local_cleanup', ['cleanup_local_ref'], indirect=True)
def test_local_ref_race_keeps_marker_cas_and_the_final_resource_guard(pending_local_cleanup, monkeypatch):
    from agent_run.adapters.git_objects import LocalGit

    s, db, effect, authority, ports = pending_local_cleanup
    replacement = s.port.remote_commit(effect.source_ref).sha
    original, deletions = LocalGit.run, []
    assert replacement != effect.before_sha

    def race(git, *arguments, **options):
        if arguments[:3] == ('update-ref', '--no-deref', '-d'):
            assert arguments[3:] == (effect.source_ref, effect.before_sha)
            deletions.append(arguments)
            original(git, 'update-ref', effect.source_ref, replacement, effect.before_sha)
        return original(git, *arguments, **options)

    monkeypatch.setattr(LocalGit, 'run', race)
    with pytest.raises(KernelError, match='local_effect_boundary_changed'):
        s.port.execute(effect, authority)
    assert len(deletions) == 1
    assert s.port.ref_commit(effect.source_ref).sha == replacement
    assert effect_state(s, effect)[:2] == ('sent', 1)
    assert s.kernel.status(effect.subject)['resource_cleanup']['state'] == 'pending'


@pytest.mark.parametrize('proof', ['close', 'marker'])
def test_first_intent_requires_its_original_transaction_proofs(cleanup_scenario, monkeypatch, proof):
    s, db = cleanup_scenario
    operation = 'cleanup_worktree' if proof == 'close' else 'cleanup_local_ref'
    prepare = s.kernel._prepare_effect
    held = []

    class BeforeIntent(Exception):
        pass

    def hold(tx, run, job, work, world, selected, **options):
        if selected == operation:
            held[:] = [run, job, work]
            raise BeforeIntent
        return prepare(tx, run, job, work, world, selected, **options)

    with monkeypatch.context() as boundary:
        boundary.setattr(s.kernel, '_prepare_effect', hold)
        with pytest.raises(BeforeIntent):
            s.run(approve=True)
    # The prefix is a real producer. Inject only a missing read proof, without
    # manufacturing a phase, Completion, close, marker, or persisted SQL row.
    before = tuple(db.iterdump())
    if proof == 'close':
        run, job, work = held
        with s.kernel.storage.transaction() as tx:
            for wrong, selected in ((replace(work, cleanup=False), operation),
                                    (work, 'cleanup_remote_ref'), (work, 'abandon_worktree'), (work, 'candidate')):
                with pytest.raises(KernelError, match='effect_binding'):
                    prepare(tx, run, job, wrong, None, selected, before=job['base_sha'])
        assert tuple(db.iterdump()) == before
    rows, missing = Transaction.rows, []

    def without_proof(tx, sql, parameters=(), *, limit=128):
        if ("FROM effects WHERE subject=? AND generation=?" in sql
                and f"operation='{('close' if proof == 'close' else 'workspace')}' AND status='confirmed'" in sql):
            missing.append(proof)
            return []
        return rows(tx, sql, parameters, limit=limit)

    with monkeypatch.context() as unavailable:
        unavailable.setattr(Transaction, 'rows', without_proof)
        if proof == 'close':
            with pytest.raises(KernelError, match='missing_or_ambiguous_fact'):
                s.kernel.next_work(s.execution)
        else:
            result = s.kernel.next_work(s.execution)
            assert result.kind == 'paused' and result.reason == 'cleanup_marker_unproven'
    assert missing == [proof] and tuple(db.iterdump()) == before
    assert not db.execute('SELECT id FROM effects WHERE operation=?', (operation,)).fetchall()
