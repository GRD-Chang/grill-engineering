"""Control/configuration domain on one real SQLite database, without processes.

Relational fixtures supply persisted prerequisites, not physical Host/Worker or
external Review evidence. The real Core control methods and original transaction
owner perform every tested mutation. No public protocol or Task router switches.
"""
from contextlib import closing
from dataclasses import asdict, replace
import json
import sqlite3

import pytest

from agent_run.core import kernel as core
from agent_run.errors import KernelError
from agent_run.model import Checks, Commit, Delivery, DeliveryKind, Policy, PullRequest, Purpose, World, wire_json
from agent_run.storage.run_scope import RunTransaction
from agent_run.storage.sqlite import Transaction
from support.offline import fixture_inputs
from support.repository_scope import add_subject, connection, insert, seed
from support.run_scoped_kernel import ScopedKernel

SUBJECT = 'parent:1'
BASE = Commit('a' * 40, 'b' * 40)
WORLD = World('r', 'g', 'r', BASE, BASE)


@pytest.fixture
def scenario():
    with closing(connection()) as db:
        db.execute('BEGIN IMMEDIATE')
        owner = Transaction(db)
        for run in ('A', 'B'):
            seed(db, run)
            tx = RunTransaction(owner, run)
            inputs = fixture_inputs()
            profile = tx.put(SUBJECT, 'profile', wire_json(inputs.profiles))
            db.execute('UPDATE profile_revisions SET material_id=? WHERE run_id=?', (profile, run))
            frozen = asdict(inputs)
            del frozen['profiles']
            material = tx.put(SUBJECT, 'creation_inputs', wire_json(frozen))
            insert(db, 'run_inputs', {'run_id': run, 'subject': SUBJECT, 'material_id': material})
            db.execute('UPDATE runs SET policy=?,epoch=1 WHERE id=?',
                (wire_json(core.Kernel._policy_values(Policy())), run))
            db.execute('UPDATE subjects SET workspace_sha=? WHERE run_id=?', (BASE.sha, run))
            for purpose in ('requirements', 'integration', 'ci'):
                insert(db, 'budgets', {'run_id': run, 'subject': SUBJECT, 'purpose': purpose,
                    'window': 1, 'development_limit': 3, 'review_limit': 3, 'review_required': 1})
            insert(db, 'resync_allowances', {'run_id': run, 'subject': SUBJECT, 'generation': 1, 'allowance': 3})
            for table in ('work_queue', 'integration_queue'):
                insert(db, table, {'run_id': run, 'subject': SUBJECT, 'ready': 1})
        db.commit()
        yield db, {run: ScopedKernel(db, run) for run in ('A', 'B')}


def rows(db, run):
    tables = [row[0] for row in db.execute("SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    return {name: [tuple(row) for row in db.execute(
        f"SELECT * FROM {name} WHERE {'id' if name == 'runs' else 'run_id'}=? ORDER BY rowid", (run,))]
        for name in tables if name != 'meta'}


def effect(db, run, *, identity='same-effect', status='prepared', operation='inspect_resources'):
    tx = RunTransaction(Transaction(db), run)
    payload = tx.put(SUBJECT, 'effect_fixture', wire_json({'fixture_run': run}))
    insert(db, 'effects', {'run_id': run, 'id': identity, 'subject': SUBJECT, 'operation': operation,
        'status': status, 'executor': 'owner-' + run, 'epoch': 1, 'parent_revision': 'r', 'graph_revision': 'g',
        'revision': 'r', 'generation': 1, 'source_ref': 'refs/heads/work', 'target_ref': 'refs/heads/main',
        'before_sha': BASE.sha, 'base_sha': BASE.sha, 'base_tree': BASE.tree,
        'payload_id': payload, 'actor': 'controlled-actor', 'attempts': 2})


def attempt(db, run, *, role='development', purpose='requirements'):
    tx = RunTransaction(Transaction(db), run)
    context = tx.put(SUBJECT, 'context_fixture', wire_json({'fixture_run': run}))
    insert(db, 'attempts', {'run_id': run, 'id': 'same-attempt', 'subject': SUBJECT, 'generation': 1,
        'ordinal': 1, 'role': role, 'purpose': purpose, 'window': 1, 'parent_revision': 'r',
        'graph_revision': 'g', 'revision': 'r', 'base_sha': BASE.sha, 'candidate_sha': BASE.sha,
        'candidate_tree': BASE.tree, 'expected_tree': BASE.tree, 'completion_revision': 'no-tickets',
        'context_id': context, 'status': 'allocated'})
    return context


def receipt(db, kernel, action):
    with kernel.storage.transaction(write=False) as tx:
        row = tx.one('SELECT subject,receipt_id FROM actions WHERE run_id=? AND id=?',
            (tx.run_id, action))
        return json.loads(tx.get(row['receipt_id'], row['subject'] or SUBJECT))


@pytest.mark.parametrize('subject', [None, SUBJECT])
def test_stop_fences_only_selected_run_cancels_prepared_and_preserves_unknown(scenario, subject):
    db, kernels = scenario
    for run in ('A', 'B'):
        effect(db, run, status='sent')
        effect(db, run, identity='prepared-link', operation='issue_link')
    db.execute("UPDATE runs SET executor='same-owner',integration_holder=?", (SUBJECT,))
    before_b = rows(db, 'B')
    assert kernels['A'].control('stop', 'same-action', subject=subject) == 'applied'
    assert rows(db, 'B') == before_b
    assert db.execute("SELECT status FROM effects WHERE run_id='A' AND id='same-effect'").fetchone()[0] == 'sent'
    assert db.execute("SELECT status FROM effects WHERE run_id='A' AND id='prepared-link'").fetchone()[0] == 'cancelled'
    if subject is None:
        assert db.execute("SELECT stopped,epoch FROM runs WHERE id='A'").fetchone()[:] == (1, 2)
        assert db.execute("SELECT status FROM retired_executors WHERE run_id='A'").fetchone()[0] == 'pending'
    else:
        assert db.execute("SELECT stopped,epoch FROM runs WHERE id='A'").fetchone()[:] == (0, 1)
        assert db.execute("SELECT code FROM gates WHERE run_id='A'").fetchone()[0] == 'operator_stopped'
    # Unknown sent work is not reclassified as exited or released by Stop.
    assert db.execute("SELECT integration_holder FROM runs WHERE id='A'").fetchone()[0] == SUBJECT
    assert db.execute('PRAGMA foreign_key_check').fetchone() is None


def test_same_action_id_is_independent_and_replay_is_read_only(scenario):
    db, kernels = scenario
    for run in ('A', 'B'):
        assert kernels[run].control('stop', 'same-action', subject=SUBJECT) == 'applied'
    before = {run: rows(db, run) for run in ('A', 'B')}
    assert kernels['A'].control('stop', 'same-action', subject=SUBJECT) == 'applied'
    assert {run: rows(db, run) for run in ('A', 'B')} == before
    with pytest.raises(KernelError, match='action_identity_changed'):
        kernels['A'].control('stop', 'same-action')
    assert {run: rows(db, run) for run in ('A', 'B')} == before
    assert receipt(db, kernels['A'], 'same-action')['action'] == 'same-action'


def test_stop_write_failure_rolls_back_action_material_and_fence(scenario):
    db, kernels = scenario
    effect(db, 'A')
    db.execute("CREATE TRIGGER controlled_stop_failure BEFORE UPDATE ON effects "
        "BEGIN SELECT RAISE(ABORT,'Controlled Stop write failure'); END")
    before = {run: rows(db, run) for run in ('A', 'B')}
    with pytest.raises(sqlite3.IntegrityError, match='Controlled Stop write failure'):
        kernels['A'].control('stop', 'stop-failure', subject=SUBJECT)
    assert kernels['A'].owners[-1].failed
    assert {run: rows(db, run) for run in ('A', 'B')} == before


def test_run_resume_feedback_and_attempt_binding_ignore_sibling_gates_and_dependencies(scenario):
    db, kernels = scenario
    db.execute('BEGIN IMMEDIATE')
    for run in ('A', 'B'):
        attempt(db, run)
        db.execute('UPDATE subjects SET current_attempt=? WHERE run_id=?', ('same-attempt', run))
        tx = RunTransaction(Transaction(db), run)
        material = tx.put(SUBJECT, 'run_wait_fixture', b'{}')
        insert(db, 'waits', {'run_id': run, 'kind': 'requirements', 'scope_id': material,
            'started_at': 90., 'deadline': 190.})
    add_subject(db, 'B', 'unfinished-predecessor')
    insert(db, 'dependencies', {'run_id': 'B', 'subject': SUBJECT, 'predecessor': 'unfinished-predecessor'})
    insert(db, 'gates', {'run_id': 'B', 'subject': SUBJECT, 'code': 'operator_stopped'})
    db.execute("UPDATE runs SET stopped=1,blocked='supervision_timeout'")
    db.commit()
    before_b = rows(db, 'B')
    assert kernels['A'].control('resume', 'resume-A', message='Explicit bounded feedback') == 'applied'
    assert rows(db, 'B') == before_b
    assert db.execute("SELECT resume_action FROM attempts WHERE run_id='A'").fetchone()[0] == 'resume-A'
    assert db.execute("SELECT count(*) FROM waits WHERE run_id='A'").fetchone()[0] == 0
    scope = receipt(db, kernels['A'], 'resume-A')['feedback_scope']
    assert scope == {SUBJECT: {'generation': 1, 'attempt': 'same-attempt', 'role': 'development'}}


def test_resume_ci_budget_renews_only_its_window_and_keeps_cumulative_review_use(scenario):
    db, kernels = scenario
    db.execute("UPDATE subjects SET purpose='ci'")
    db.execute("UPDATE budgets SET developments_used=3,reviews_used=2 WHERE purpose='ci'")
    for run in ('A', 'B'):
        insert(db, 'gates', {'run_id': run, 'subject': SUBJECT, 'code': 'ci_development_budget_exhausted', 'purpose': 'ci'})
    before_b = rows(db, 'B')
    original = tuple(db.execute("SELECT * FROM budgets WHERE run_id='A' AND purpose='requirements'").fetchone())
    assert kernels['A'].control('resume', 'resume-ci', subject=SUBJECT) == 'applied'
    assert rows(db, 'B') == before_b
    assert db.execute("SELECT window,developments_used,reviews_used FROM budgets WHERE run_id='A' AND purpose='ci' AND current=1").fetchone()[:] == (2, 0, 2)
    assert tuple(db.execute("SELECT * FROM budgets WHERE run_id='A' AND purpose='requirements'").fetchone()) == original


def checks_timeout(db, kernel, run, *, kind='required_checks', deadline=90.):
    with kernel.storage.transaction() as tx:
        header = tx.one('SELECT * FROM runs WHERE id=?', (run,))
        scope = kernel._automatic_scope(tx, header, SUBJECT, False)
        scope_id = tx.put(SUBJECT, 'required_checks_scope', wire_json(scope))
        timeout = tx.put(SUBJECT, 'supervision_timeout', wire_json({
            'kind': kind, 'scope_id': scope_id, 'started_at': 80., 'deadline': deadline}))
        tx.execute('INSERT INTO waits(run_id,subject,kind,scope_id,started_at,deadline) VALUES (?,?,?,?,?,?)',
            (run, SUBJECT, kind, scope_id, 80., deadline))
        tx.execute('INSERT INTO gates(run_id,subject,code,purpose,evidence) VALUES (?,?,?,?,?)',
            (run, SUBJECT, 'supervision_timeout', 'ci', timeout))
    return scope_id, timeout


def test_explicit_checks_policy_checkpoint_preserves_old_scope_evidence_and_other_budgets(scenario):
    db, kernels = scenario
    db.execute("UPDATE subjects SET phase='checks',purpose='ci'")
    ids = {run: checks_timeout(db, kernels[run], run) for run in ('A', 'B')}
    before_b = rows(db, 'B')
    budget = list(db.execute("SELECT * FROM budgets WHERE run_id='A'"))
    old = db.execute('SELECT body FROM material WHERE run_id=? AND id=?', ('A', ids['A'][1])).fetchone()[0]
    assert kernels['A'].control('resume', 'resume-checks', subject=SUBJECT,
        policy_overrides={'required_checks_wait_seconds': 7200}) == 'applied'
    assert rows(db, 'B') == before_b
    with kernels['A'].storage.transaction(write=False) as tx:
        assert kernels['A']._subject_policy(tx, kernels['A']._job(tx, SUBJECT)).required_checks_wait_seconds == 7200
    assert list(db.execute("SELECT * FROM budgets WHERE run_id='A'")) == budget
    assert db.execute('SELECT body FROM material WHERE run_id=? AND id=?', ('A', ids['A'][1])).fetchone()[0] == old
    assert db.execute("SELECT count(*) FROM waits WHERE run_id='A'").fetchone()[0] == 0


@pytest.mark.parametrize('kind,deadline', [('requirements', 90.), ('required_checks', 110.)])
def test_nonchecks_or_unexpired_timeout_does_not_authorize_policy_changes(scenario, kind, deadline):
    db, kernels = scenario
    db.execute("UPDATE subjects SET phase='checks',purpose='ci'")
    checks_timeout(db, kernels['A'], 'A', kind=kind, deadline=deadline)
    before = {run: rows(db, run) for run in ('A', 'B')}
    with pytest.raises(KernelError, match='policy_override_boundary'):
        kernels['A'].control('resume', 'invalid-checkpoint', subject=SUBJECT,
            policy_overrides={'required_checks_wait_seconds': 7200})
    assert {run: rows(db, run) for run in ('A', 'B')} == before


def host_gate(db, run, *, exited, proof_nonce=None):
    tx = RunTransaction(Transaction(db), run)
    request = tx.put(SUBJECT, 'host_request_fixture', b'{}')
    proof = tx.put(SUBJECT, 'worker_host_exit', wire_json({'host_nonce': proof_nonce or 'nonce-' + run}))
    insert(db, 'actions', {'run_id': run, 'id': 'same-host-action', 'kind': 'run', 'status': 'applied'})
    insert(db, 'hosts', {'run_id': run, 'epoch': 1, 'owner': 'same-owner', 'nonce': 'nonce-' + run,
        'action_id': 'same-host-action', 'subject': SUBJECT, 'request_id': request,
        'backend': 'foreground', 'allocated_at': 80., 'status': 'exited' if exited else 'unknown',
        'usage_released': int(exited)})
    insert(db, 'gates', {'run_id': run, 'subject': SUBJECT, 'code': 'execution_failed', 'evidence': proof})


def test_physically_retired_recorded_gate_scope_does_not_borrow_sibling_unknown_host(scenario):
    db, kernels = scenario
    host_gate(db, 'A', exited=True)
    host_gate(db, 'B', exited=False)
    before_b = rows(db, 'B')
    assert kernels['A'].resume_subject() is None
    assert kernels['A'].control('resume', 'resume-host') == 'applied'
    assert rows(db, 'B') == before_b
    assert db.execute("SELECT count(*) FROM gates WHERE run_id='A'").fetchone()[0] == 0


@pytest.mark.parametrize('exited,proof_nonce', [(False, None), (True, 'nonce-B')])
def test_unknown_host_or_foreign_exit_evidence_cannot_remove_gates(scenario, exited, proof_nonce):
    db, kernels = scenario
    host_gate(db, 'A', exited=exited, proof_nonce=proof_nonce)
    before = {run: rows(db, run) for run in ('A', 'B')}
    with pytest.raises(KernelError, match='no_resumable_boundary'):
        kernels['A'].control('resume', 'unproven-host-resume')
    assert {run: rows(db, run) for run in ('A', 'B')} == before


def test_same_invocation_output_ids_do_not_let_resume_consume_sibling_exit(scenario):
    db, kernels = scenario
    db.execute('BEGIN IMMEDIATE')
    for run in ('A', 'B'):
        context = attempt(db, run)
        tx = RunTransaction(Transaction(db), run)
        process = tx.put(SUBJECT, 'synthetic_process_identity', wire_json({'fixture_run': run}))
        insert(db, 'threads', {'run_id': run, 'id': 'same-thread', 'subject': SUBJECT,
            'role': 'development', 'profile_revision': 1})
        insert(db, 'invocations', {'run_id': run, 'id': 'same-invocation', 'attempt': 'same-attempt',
            'thread': 'same-thread', 'executor': 'same-owner', 'epoch': 1, 'status': 'running',
            'allocated_at': 80., 'deadline_seconds': 30., 'deadline_at': 110.})
        insert(db, 'invocation_outputs', {'run_id': run, 'invocation': 'same-invocation', 'ordinal': 0,
            'context_id': context, 'lease': 'lease-' + run, 'process_identity': process if run == 'A' else None,
            'dispatch_state': 'exited' if run == 'A' else 'running', 'allocated_at': 80., 'mode': 'start'})
        db.execute('UPDATE attempts SET current_invocation=? WHERE run_id=?', ('same-invocation', run))
        db.execute('UPDATE subjects SET current_attempt=? WHERE run_id=?', ('same-attempt', run))
        insert(db, 'gates', {'run_id': run, 'subject': SUBJECT, 'code': 'worker_start_unknown'})
    db.commit()
    before_b = rows(db, 'B')
    assert kernels['A'].control('resume', 'resume-output', subject=SUBJECT) == 'applied'
    assert rows(db, 'B') == before_b
    assert db.execute("SELECT status,deadline_at FROM invocations WHERE run_id='A'").fetchone()[:] == ('interrupted', 110.)
    before = {run: rows(db, run) for run in ('A', 'B')}
    with pytest.raises(KernelError, match='worker_exit_unproven'):
        kernels['B'].control('resume', 'resume-output', subject=SUBJECT)
    assert {run: rows(db, run) for run in ('A', 'B')} == before


def test_abandon_clears_only_its_queues_and_freezes_only_its_generations_and_intents(scenario):
    db, kernels = scenario
    db.execute("UPDATE runs SET executor='same-owner'")
    effect(db, 'A')
    effect(db, 'B', operation='merge', status='sent')
    db.execute("UPDATE subjects SET merge_sha=? WHERE run_id='B'", ('c' * 40,))
    before_b = rows(db, 'B')
    assert kernels['A'].control('abandon', 'abandon-A', resource_inspections=()) == 'pending'
    assert rows(db, 'B') == before_b
    value = receipt(db, kernels['A'], 'abandon-A')['abandonment']
    assert value['execution'] == {'run_id': 'A', 'owner': 'same-owner', 'epoch': 1}
    assert value['generations'] == [[SUBJECT, 1]] and value['unsettled'] == ['same-effect']
    for table in ('work_queue', 'integration_queue'):
        assert db.execute(f"SELECT count(*) FROM {table} WHERE run_id='A'").fetchone()[0] == 0
    assert db.execute("SELECT status FROM effects WHERE run_id='A'").fetchone()[0] == 'prepared'


def test_unknown_final_merge_still_refuses_abandon_without_any_mutation(scenario):
    db, kernels = scenario
    effect(db, 'A', operation='merge', status='sent')
    before = {run: rows(db, run) for run in ('A', 'B')}
    with pytest.raises(KernelError, match='final_merge_unsettled'):
        kernels['A'].control('abandon', 'unsafe-abandon', resource_inspections=())
    assert {run: rows(db, run) for run in ('A', 'B')} == before


def test_configuration_revision_and_replay_do_not_rebind_sibling_or_existing_threads(scenario):
    db, kernels = scenario
    for run in ('A', 'B'):
        insert(db, 'threads', {'run_id': run, 'id': 'same-thread', 'subject': SUBJECT,
            'role': 'development', 'profile_revision': 1})
    before_b = rows(db, 'B')
    with kernels['A'].storage.transaction(write=False) as tx:
        original_profile = kernels['A']._profile_document(tx, 1)
        original_inputs = kernels['A']._inputs(tx, tx.one("SELECT * FROM runs WHERE id='A'"))
    assert kernels['A'].configure_profiles('same-config', overrides={'development_model': 'frozen-A'}) == 2
    assert rows(db, 'B') == before_b
    assert db.execute("SELECT profile_revision FROM threads WHERE run_id='A'").fetchone()[0] == 1
    before_a = rows(db, 'A')
    assert kernels['A'].configure_profiles('same-config', overrides={'development_model': 'frozen-A'}) == 2
    assert rows(db, 'A') == before_a
    with pytest.raises(KernelError, match='action_identity_changed'):
        kernels['A'].configure_profiles('same-config', overrides={'development_model': 'other'})
    assert rows(db, 'A') == before_a
    assert kernels['B'].configure_profiles('same-config', overrides={'development_model': 'frozen-B'}) == 2
    assert rows(db, 'A') == before_a
    for run in ('A', 'B'):
        with kernels[run].storage.transaction(write=False) as tx:
            assert kernels[run]._profile_document(tx, 2)['profiles']['development']['model'] == 'frozen-' + run
    with kernels['A'].storage.transaction(write=False) as tx:
        assert kernels['A']._profile_document(tx, 1) == original_profile
        assert kernels['A']._inputs(tx, tx.one("SELECT * FROM runs WHERE id='A'")) == original_inputs


def approval(db, run, *, result='pass', final=False):
    db.execute('BEGIN IMMEDIATE')
    context = attempt(db, run, role='review', purpose='integration')
    checks = Checks(11, BASE.sha, result, (('quality', result),))
    tx = RunTransaction(Transaction(db), run)
    material = tx.put(SUBJECT, 'required_checks', wire_json(asdict(checks)))
    insert(db, 'checks', {'run_id': run, 'id': 'same-check', 'subject': SUBJECT,
        'head_sha': BASE.sha, 'pr_number': 11, 'material_id': material})
    db.execute("UPDATE attempts SET status='returned',outcome='pass' WHERE run_id=?", (run,))
    db.execute("UPDATE subjects SET kind=?,phase='approval',purpose='integration',candidate_sha=?,candidate_tree=?,expected_tree=?,review_attempt='same-attempt',publication_sha=?,publication_tree=?,integration_target=?,pr_number=11,checks_id='same-check' WHERE run_id=?",
        ('final_run' if final else 'parent_only', BASE.sha, BASE.tree, BASE.tree, BASE.sha, BASE.tree, BASE.sha, run))
    db.commit()
    pr = PullRequest(11, 'example/project', 'refs/heads/work', 'refs/heads/main', BASE, BASE,
        'Approved subject', 'Frozen publication', actor='controlled-actor', head_repository='example/project')
    return replace(WORLD, pr=pr, checks=checks)


def test_approve_creates_only_local_grant_and_final_action_with_identical_ids(scenario, monkeypatch):
    db, kernels = scenario
    worlds = {run: approval(db, run) for run in ('A', 'B')}
    monkeypatch.setattr(core, 'identity', lambda: 'same-grant')
    before_b = rows(db, 'B')
    assert kernels['A'].control('approve', 'same-approve', subject=SUBJECT, world=worlds['A']) == 'pending'
    assert rows(db, 'B') == before_b
    assert db.execute("SELECT phase,grant_id FROM subjects WHERE run_id='A'").fetchone()[:] == ('merge', 'same-grant')
    assert db.execute("SELECT final_action FROM runs WHERE id='A'").fetchone()[0] == 'same-approve'
    before_a = rows(db, 'A')
    assert kernels['B'].control('approve', 'same-approve', subject=SUBJECT, world=worlds['B']) == 'pending'
    assert rows(db, 'A') == before_a
    assert db.execute('SELECT count(*) FROM grants').fetchone()[0] == 2
    assert db.execute('PRAGMA foreign_key_check').fetchone() is None
    with pytest.raises(KernelError, match='final_approval_noncancellable'):
        kernels['A'].control('stop', 'must-not-revoke-approved-merge')
    assert rows(db, 'A') == before_a


def test_approve_failure_enters_existing_checks_path_without_grant_or_budget_reset(scenario):
    db, kernels = scenario
    world = approval(db, 'A', result='fail')
    before_b = rows(db, 'B')
    budgets = list(db.execute("SELECT * FROM budgets WHERE run_id='A'"))
    assert kernels['A'].control('approve', 'approve-ci-failed', subject=SUBJECT, world=world) == 'updating'
    assert rows(db, 'B') == before_b
    assert db.execute("SELECT phase,grant_id FROM subjects WHERE run_id='A'").fetchone()[:] == ('checks', None)
    assert db.execute('SELECT count(*) FROM grants').fetchone()[0] == 0
    assert list(db.execute("SELECT * FROM budgets WHERE run_id='A'")) == budgets


def test_base_advance_returns_only_selected_approval_to_existing_integration(scenario):
    db, kernels = scenario
    world = approval(db, 'A')
    target = Commit('c' * 40, 'd' * 40)
    world = replace(world, target=target, ancestry=((BASE.sha, target.sha),))
    before_b = rows(db, 'B')
    assert kernels['A'].control('approve', 'approve-new-base', subject=SUBJECT, world=world) == 'updating'
    assert rows(db, 'B') == before_b
    assert db.execute("SELECT phase,purpose,grant_id FROM subjects WHERE run_id='A'").fetchone()[:] == ('integration', 'integration', None)
    assert db.execute('SELECT count(*) FROM grants').fetchone()[0] == 0


def test_revise_final_only_renews_its_own_cycle_and_retains_original_review(scenario):
    db, kernels = scenario
    world = approval(db, 'A', final=True)
    before_b = rows(db, 'B')
    assert kernels['A'].control('revise', 'revise-A', subject=SUBJECT, world=world, message='One explicit revision') == 'applied'
    assert rows(db, 'B') == before_b
    assert db.execute("SELECT phase,purpose,repair_source FROM subjects WHERE run_id='A'").fetchone()[:] == ('development', 'integration', 'human_revision')
    assert db.execute("SELECT window FROM budgets WHERE run_id='A' AND purpose='integration' AND current=1").fetchone()[0] == 2
    assert db.execute("SELECT outcome FROM attempts WHERE run_id='A'").fetchone()[0] == 'pass'


def test_requeue_generation_and_source_revision_do_not_clear_sibling_gates(scenario):
    db, kernels = scenario
    for run in ('A', 'B'):
        db.execute("UPDATE runs SET blocked='requeue_required',observed_parent_revision=? WHERE id=?", ('next-' + run, run))
        insert(db, 'gates', {'run_id': run, 'subject': SUBJECT, 'code': 'requeue_required'})
    replacement = Delivery(SUBJECT, DeliveryKind.PARENT_ONLY, 1, 'next-A', BASE,
        'refs/heads/new-generation', 'refs/heads/main', checkout='/fixture/new-generation')
    world = replace(WORLD, parent_revision='next-A', revision='next-A')
    before_b = rows(db, 'B')
    assert kernels['A'].control('requeue', 'requeue-A', subject=SUBJECT, world=world, replacement=replacement) == 'applied'
    assert rows(db, 'B') == before_b
    assert db.execute("SELECT generation,revision,phase FROM subjects WHERE run_id='A'").fetchone()[:] == (2, 'next-A', 'workspace')
    assert db.execute("SELECT blocked,parent_revision FROM runs WHERE id='A'").fetchone()[:] == (None, 'next-A')
    assert db.execute('PRAGMA foreign_key_check').fetchone() is None


@pytest.mark.parametrize('boundary', ['requeue_run', 'requeue_job', 'retirement_run', 'retirement_job',
    'host_run', 'timeout_run', 'timeout_job', 'renew_job'])
def test_passed_foreign_records_are_refused_before_control_reads_or_writes(scenario, boundary):
    db, kernels = scenario
    before = {run: rows(db, run) for run in ('A', 'B')}
    with pytest.raises(KernelError, match='control_binding'):
        with kernels['A'].storage.transaction() as tx:
            own_run = tx.one("SELECT * FROM runs WHERE id='A'")
            foreign_run = tx.one("SELECT * FROM runs WHERE id='B'")
            own_job = tx.one("SELECT * FROM subjects WHERE run_id='A'")
            foreign_job = tx.one("SELECT * FROM subjects WHERE run_id='B'")
            run = foreign_run if boundary.endswith('_run') else own_run
            job = foreign_job if boundary.endswith('_job') else own_job
            if boundary.startswith('requeue'):
                replacement = Delivery(SUBJECT, DeliveryKind.PARENT_ONLY, 1, 'r', BASE, 'refs/heads/new', 'refs/heads/main')
                kernels['A']._validate_requeue(tx, run, job, replacement, WORLD)
            elif boundary.startswith('retirement'):
                kernels['A']._recorded_retirement(tx, run, job, WORLD)
            elif boundary == 'host_run':
                kernels['A']._host_resume_gates(tx, run)
            elif boundary.startswith('timeout'):
                kernels['A']._checks_timeout_matches(tx, run, job, 'must-not-read')
            else:
                kernels['A']._renew(tx, job, Purpose.CI.value)
    assert {run: rows(db, run) for run in ('A', 'B')} == before


def test_control_view_keeps_owner_read_budget_and_poison(scenario, monkeypatch):
    db, kernels = scenario
    original = kernels['A']._control
    def exhausted(tx, *args, **kwargs):
        tx.rows_read = 65536
        return original(tx, *args, **kwargs)
    monkeypatch.setattr(kernels['A'], '_control', exhausted)
    before = {run: rows(db, run) for run in ('A', 'B')}
    with pytest.raises(KernelError, match='query_limit'):
        kernels['A'].control('stop', 'budget-must-not-reset')
    assert kernels['A'].owners[-1].rows_read == 65536
    assert {run: rows(db, run) for run in ('A', 'B')} == before


def test_caught_control_sql_failure_still_poisons_original_owner_and_rolls_back(scenario, monkeypatch):
    db, kernels = scenario
    original = kernels['A']._control
    def caught(tx, *args, **kwargs):
        result = original(tx, *args, **kwargs)
        try:
            tx.execute('INSERT INTO missing_control_fixture_table VALUES (1)')
        except sqlite3.OperationalError:
            pass
        return result
    monkeypatch.setattr(kernels['A'], '_control', caught)
    before = {run: rows(db, run) for run in ('A', 'B')}
    with pytest.raises(KernelError, match='transaction_aborted'):
        kernels['A'].control('stop', 'poisoned-control')
    assert kernels['A'].owners[-1].failed
    assert {run: rows(db, run) for run in ('A', 'B')} == before


def test_resume_subject_selection_never_borrows_a_sibling_gate_or_ticket(scenario):
    db, kernels = scenario
    insert(db, 'gates', {'run_id': 'B', 'subject': SUBJECT, 'code': 'operator_stopped'})
    with pytest.raises(KernelError, match='control_subject_ambiguous'):
        kernels['A'].resume_subject()
    insert(db, 'gates', {'run_id': 'A', 'subject': SUBJECT, 'code': 'operator_stopped'})
    assert kernels['A'].resume_subject() == SUBJECT
    add_subject(db, 'B', 'only-B-ticket')
    db.execute("UPDATE subjects SET kind='ticket',issue=2 WHERE run_id='B' AND id='only-B-ticket'")
    with pytest.raises(KernelError, match='control_subject_ambiguous'):
        kernels['A'].resume_subject(ticket=2)
    assert kernels['B'].resume_subject(ticket=2) == 'only-B-ticket'
