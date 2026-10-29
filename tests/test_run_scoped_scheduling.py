"""Source/selection domain on real SQLite, not public creation or a Host.

Only complete frozen input/source prerequisites are supplied as relational data.
Subsequent observation, refresh and scheduler operations are existing Core APIs.
"""
from contextlib import closing, contextmanager
from dataclasses import asdict, replace
import sqlite3

import pytest

from agent_run.core import kernel as core
from agent_run.core.operation_contract import DeliveryFacts, ObserveIO, ReadUnavailable, Work
from agent_run.errors import KernelError
from agent_run.model import Commit, EffectResult, Execution, ParentIssue, Policy, ReadWait, Requirements, World, wire_json
from agent_run.storage.run_scope import RunTransaction
from agent_run.storage.sqlite import Transaction
from support.offline import fixture_inputs
from support.repository_scope import connection, insert, seed
from support.run_scoped_kernel import ScopedKernel


SUBJECT = "parent:1"
BASE = Commit("a" * 40, "b" * 40)


def _source(run, version="initial"):
    return Requirements(ParentIssue(1, "Parent " + run, version + " body " + run), ())


@pytest.fixture
def scenario():
    with closing(connection()) as db:
        db.execute("BEGIN")
        owner = Transaction(db)
        inputs = fixture_inputs()
        for run in ("A", "B"):
            seed(db, run)
            tx = RunTransaction(owner, run)
            source = _source(run)
            parent, graph = core.Kernel._source_revisions(core.Kernel.__new__(core.Kernel), source)
            profile = tx.put(SUBJECT, "profile", wire_json(inputs.profiles))
            db.execute("UPDATE profile_revisions SET material_id=? WHERE run_id=?", (profile, run))
            values = asdict(inputs)
            del values["profiles"]
            material = tx.put(SUBJECT, "creation_inputs", wire_json(values))
            insert(db, "run_inputs", {"run_id": run, "subject": SUBJECT, "material_id": material})
            policy = Policy(development_deadline_seconds=7., review_deadline_seconds=9.)
            db.execute("UPDATE runs SET policy=?,executor='same-owner',epoch=1,parent_revision=?,graph_revision=?,accepted_parent_revision=? WHERE id=?",
                (wire_json(core.Kernel._policy_values(policy)), parent, graph, parent, run))
            db.execute("UPDATE subjects SET revision=?,checkout=?,workspace_sha=? WHERE run_id=?", (parent, "/fixture/" + run, BASE.sha, run))
            body = wire_json({"parent": asdict(source.parent), "issues": []})
            insert(db, "source_observations", {"run_id": run, "parent_revision": parent, "graph_revision": graph, "body": body})
            sequence = db.execute("SELECT sequence FROM source_observations WHERE run_id=?", (run,)).fetchone()[0]
            db.execute("UPDATE runs SET source_sequence=?,observed_parent_revision=?,observed_graph_revision=? WHERE id=?", (sequence, parent, graph, run))
            insert(db, "requirement_versions", {"run_id": run, "issue": 1, "revision": parent, "title": source.parent.title, "body": source.parent.body})
            for purpose in ("requirements", "integration", "ci"):
                insert(db, "budgets", {"run_id": run, "subject": SUBJECT, "purpose": purpose, "window": 1,
                    "development_limit": 3, "review_limit": 3, "review_required": 1})
            insert(db, "resync_allowances", {"run_id": run, "subject": SUBJECT, "generation": 1, "allowance": 3})
        db.commit()
        yield db, {run: ScopedKernel(db, run) for run in ("A", "B")}


def _execution(run):
    return Execution(run, "same-owner", 1)


def _rows(db, run):
    names = [row[0] for row in db.execute("SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    return {name: [tuple(row) for row in db.execute(
        f"SELECT * FROM {name} WHERE {'id' if name == 'runs' else 'run_id'}=? ORDER BY rowid", (run,))]
        for name in names if name != "meta"}


def test_next_work_without_choice_reselects_using_one_snapshot(scenario, monkeypatch):
    db, kernels = scenario
    kernel = kernels['A']
    db.execute("UPDATE runs SET blocked='unsupported_scope_change' WHERE id='A'")
    choice, calls = kernel._operation_choice, []

    def selected(*args, **kwargs):
        calls.append(True)
        return choice(*args, **kwargs)

    monkeypatch.setattr(kernel, '_operation_choice', selected)
    before = len(kernel.owners)
    assert kernel.next_work(_execution('A')).kind == 'quiescent'
    assert calls == [True, True] and len(kernel.owners) == before + 1
    assert kernel.operation_state().disposition == 'quiescent'


def test_next_work_reselects_in_same_snapshot_when_read_retry_becomes_due(scenario, monkeypatch):
    _, kernels = scenario
    kernel, now = kernels['A'], [100.]
    kernel.clock = lambda: now[0]
    request = kernel.begin_observation(SUBJECT, execution=_execution('A'))
    waiting = kernel._wait_for_read(request.fence, _execution('A'), 'controlled_read', 'Original read unavailable')
    assert waiting.retry_at == 105.
    now[0] = 104.999
    select, calls = kernel._operation_choice, []

    def choice(*args, **kwargs):
        result = select(*args, **kwargs)
        calls.append(result)
        if len(calls) == 1:
            assert result is None
            now[0] = 105.
        return result

    monkeypatch.setattr(kernel, '_operation_choice', choice)
    before = len(kernel.owners)
    result = kernel.next_work(_execution('A'))
    assert result.kind == 'ready' and result.retry_at is None
    assert len(calls) == 2 and calls[1] is not None
    assert len(kernel.owners) == before + 1


def test_zero_physical_capacity_keeps_passive_readiness_in_same_snapshot(scenario, monkeypatch):
    db, kernels = scenario
    kernel = kernels['A']
    insert(db, 'work_queue', {'run_id': 'A', 'subject': SUBJECT, 'ready': 1})
    choices, select = [], kernel._operation_choice

    def choice(*args, **kwargs):
        choices.append(True)
        return select(*args, **kwargs)

    monkeypatch.setattr(kernel, '_operation_choice', choice)
    assert kernel.next_work(_execution('A'), agent_capacity=0).kind == 'ready'
    assert choices == [True, True]
    assert not db.execute("SELECT scope_id FROM waits WHERE run_id='A'").fetchall()


def test_shared_facts_keep_the_original_operation_token(scenario):
    _, kernels = scenario
    kernel = kernels['A']
    with kernel.storage.transaction(write=False) as tx:
        run, job = kernel._owned(tx, SUBJECT, _execution('A'))
        snapshot = core._OperationSnapshot(kernel, tx, run)
        snapshot.subject(SUBJECT, job)
        assert kernel._operation_token(tx, SUBJECT) == kernel._operation_token(tx, SUBJECT, snapshot=snapshot)


@pytest.mark.parametrize('change', ['gate', 'source', 'epoch'])
def test_snapshot_ends_at_same_transaction_mutation_and_new_snapshot_sees_it(scenario, change):
    db, kernels = scenario
    kernel = kernels['A']
    with kernel.storage.transaction() as tx:
        run, job = kernel._owned(tx, SUBJECT, _execution('A'))
        snapshot = core._OperationSnapshot(kernel, tx, run)
        read = snapshot.subject(SUBJECT, job)
        assert read.gates == ()
        if change == 'gate':
            kernel._gate(tx, SUBJECT, 'human_blocker')
        elif change == 'source':
            kernel._refresh(tx, _source('A', 'new snapshot'))
        else:
            tx.execute("UPDATE runs SET epoch=epoch+1 WHERE id='A'")
        with pytest.raises(KernelError, match='operation_snapshot_changed'):
            kernel._subject_wait(tx, run, job, read=read)
        current_run = tx.one("SELECT * FROM runs WHERE id='A'")
        fresh = core._OperationSnapshot(kernel, tx, current_run).subject(SUBJECT)
        if change == 'gate':
            assert [gate['code'] for gate in fresh.gates] == ['human_blocker']
        else:
            key = 'source_sequence' if change == 'source' else 'epoch'
            assert current_run[key] > run[key]
    with kernel.storage.transaction(write=False) as new_tx:
        with pytest.raises(KernelError, match='operation_snapshot_changed'):
            snapshot.require(new_tx)


def test_snapshot_rejects_closed_or_poisoned_owner(scenario):
    _, kernels = scenario
    kernel = kernels['A']
    with kernel.storage.transaction(write=False) as tx:
        run, job = kernel._owned(tx, SUBJECT, _execution('A'))
        snapshot = core._OperationSnapshot(kernel, tx, run)
        read = snapshot.subject(SUBJECT, job)
    with pytest.raises(KernelError, match='operation_snapshot_changed'):
        kernel._subject_wait(tx, run, job, read=read)
    with pytest.raises(KernelError, match='transaction_aborted'):
        with kernel.storage.transaction(write=False) as tx:
            run, job = kernel._owned(tx, SUBJECT, _execution('A'))
            snapshot = core._OperationSnapshot(kernel, tx, run)
            try:
                tx.execute('SELECT absent_column FROM runs')
            except sqlite3.OperationalError:
                pass
            with pytest.raises(KernelError, match='operation_snapshot_changed'):
                snapshot.require(tx)


def test_source_observation_refresh_and_next_work_do_not_read_or_drive_sibling_run(scenario):
    db, kernels = scenario
    before_b = _rows(db, "B")
    request = kernels["A"].begin_observation(SUBJECT, execution=_execution("A"))
    observation = kernels["A"].accept_requirements(request, _source("A", "updated"))
    world, fence = kernels["A"].accept_delivery(observation, DeliveryFacts(BASE, BASE))
    assert world.parent_revision != kernels["B"].source_status()["parent_revision"]
    assert _rows(db, "B") == before_b

    next_io = kernels["A"].next_work(_execution("A"))
    assert isinstance(next_io, ObserveIO) and next_io.work.execution.run_id == "A"
    observed = kernels["A"].accept_requirements(next_io.request, _source("A", "updated"))
    facts = kernels["A"].accept_delivery(observed, DeliveryFacts(BASE, BASE))
    prepared = kernels["A"].accept_work(next_io, facts).preparation
    assert prepared is not None and prepared.execution.run_id == "A"
    assert prepared.context["parent_revision"] == world.parent_revision
    assert db.execute("SELECT count(*) FROM invocations").fetchone()[0] == 0
    assert _rows(db, "B") == before_b

@pytest.mark.parametrize("callback", ["accept", "fail"])
def test_foreign_work_cannot_commit_audit_before_the_selected_run_binding_is_checked(scenario, callback):
    db, kernels = scenario
    with kernels["A"].storage.transaction() as tx:
        run, job = kernels["A"]._owned(tx, SUBJECT, _execution("A"))
        world = World(run["parent_revision"], run["graph_revision"], job["revision"], BASE, BASE)
        work = Work(_execution("A"), "pending", SUBJECT, False, True, None, None)
        request = kernels["A"]._prepare_effect(tx, run, job, work, world, "workspace", before=BASE.sha)
    assert kernels["A"]._dispatch(request.effect, work.execution, world, token=request.token)
    foreign_work = replace(work, execution=_execution("B"))
    foreign_request = replace(request, work=foreign_work)
    before = {run: _rows(db, run) for run in ("A", "B")}
    with pytest.raises(KernelError):
        if callback == "accept":
            kernels["A"].accept_work(foreign_request, EffectResult("target", commit=BASE))
        else:
            kernels["A"].fail_work(foreign_work, ReadUnavailable("read_failed", "controlled", write_returncode=23), request=foreign_request)
    assert {run: _rows(db, run) for run in ("A", "B")} == before


def _ticket_source(run, *, closed=False, body='original'):
    from agent_run.model import Blocker, Issue
    return Requirements(ParentIssue(1, 'Parent ' + run, 'Frozen background', (2, 3)), (
        Issue(2, 'Ticket two ' + run, body, 'CLOSED' if closed else 'OPEN', frozenset({'ready-for-agent'})),
        Issue(3, 'Ticket three ' + run, 'Dependent', 'OPEN', frozenset({'ready-for-agent'}), (Blocker(2, 'CLOSED' if closed else 'OPEN'),)),
    ))


@pytest.fixture
def ticket_scenario():
    """Staged frozen roots, then real add-delivery/profile/initial-refresh writes."""
    from agent_run.model import Delivery, DeliveryKind
    with closing(connection()) as db:
        db.execute('BEGIN')
        owner = Transaction(db)
        inputs, policy = fixture_inputs(), Policy(development_deadline_seconds=7., review_deadline_seconds=9.)
        kernels = {run: ScopedKernel(db, run) for run in ('A', 'B')}
        for run in ('A', 'B'):
            source = _ticket_source(run)
            parent, graph = kernels[run]._source_revisions(source)
            insert(db, 'runs', {'id': run, 'repository': 'example/project', 'parent_issue': 1,
                'parent_revision': parent, 'graph_revision': graph, 'accepted_parent_revision': parent,
                'concurrency': 1, 'policy': wire_json(core.Kernel._policy_values(policy)), 'executor': 'same-owner',
                'epoch': 1, 'language': 'en', 'repository_root': '/private/owned', 'created_at': 100.})
            tx = RunTransaction(owner, run)
            for number in (2, 3):
                issue = source.issues[number - 2]
                revision = kernels[run]._fingerprint({'title': issue.title, 'body': issue.body})
                kernels[run]._add_delivery(tx, run, policy, Delivery('ticket:' + str(number), DeliveryKind.TICKET,
                    number, revision, BASE, 'refs/heads/ticket-' + str(number), 'refs/heads/run',
                    checkout='/fixture/' + run + '/ticket-' + str(number)))
            insert(db, 'final_target', {'run_id': run, 'id': 'final', 'source_ref': 'refs/heads/run',
                'target_ref': 'refs/heads/main', 'checkout': '/fixture/' + run + '/final',
                'owned_root': '/fixture/' + run, 'common_git_dir': '/fixture/' + run + '/git'})
            assert kernels[run]._store_profiles(tx, 'ticket:2', inputs.profiles, {'operation': 'fixture'}) == 1
            values = asdict(inputs)
            del values['profiles']
            material = tx.put('ticket:2', 'creation_inputs', wire_json(values))
            insert(db, 'run_inputs', {'run_id': run, 'subject': 'ticket:2', 'material_id': material})
            kernels[run]._refresh(tx, source, initial=True)
        db.commit()
        yield db, kernels


def _once(kernel, run, source, *, facts=None):
    result = kernel.next_work(_execution(run))
    if isinstance(result, ObserveIO):
        observation = kernel.accept_requirements(result.request, source)
        actual = kernel.accept_delivery(observation, DeliveryFacts(BASE, BASE) if facts is None else facts)
        result = kernel.accept_work(result, actual)
    return result


def test_reused_facts_do_not_cache_a_clock_dependent_wait(scenario):
    from support.core_operations import prepare_scripted_agent

    _, kernels = scenario
    kernel, now = kernels['A'], [100.]
    kernel.clock = lambda: now[0]
    call = prepare_scripted_agent(kernel, _once(kernel, 'A', _source('A'))).call
    assert call is not None
    with kernel.storage.transaction(write=False) as tx:
        run, job = kernel._owned(tx, SUBJECT, _execution('A'))
        read = core._OperationSnapshot(kernel, tx, run).subject(SUBJECT, job)
        assert read.waiting.reason == 'agent_running'
        now[0] = call.deadline_at
        assert read.waiting is None


def test_no_choice_reselection_shares_the_same_continuation_facts(scenario, monkeypatch):
    from support.core_operations import prepare_scripted_agent

    _, kernels = scenario
    kernel = kernels['A']
    call = prepare_scripted_agent(kernel, _once(kernel, 'A', _source('A'))).call
    assert call is not None
    load, reads = core.load_continuation, []

    def counted(tx, attempt):
        reads.append((tx, attempt))
        return load(tx, attempt)

    monkeypatch.setattr(core, 'load_continuation', counted)
    before = len(kernel.owners)
    result = kernel.next_work(_execution('A'))
    assert result.kind == 'waiting'
    assert len(kernel.owners) == before + 1
    assert len(reads) == 1 and reads[0][1] == call.attempt_id


def test_ticket_frontier_dependencies_and_queues_do_not_take_sibling_closed_state(ticket_scenario):
    db, kernels = ticket_scenario
    kernels['B'].refresh(lambda: _ticket_source('B', closed=True))
    before_b = _rows(db, 'B')
    status = kernels['A'].source_status()
    assert status['frontier'] == ['ticket:2']
    assert status['remaining']['ticket:3'] == 'blocked_by_incomplete_tickets'
    assert kernels['B'].source_status()['remaining']['ticket:2'] == 'ticket_closed'
    assert kernels['A'].operation_state().disposition == 'ready'
    preparation = _once(kernels['A'], 'A', _ticket_source('A')).preparation
    assert preparation is not None and preparation.subject == 'ticket:2'
    assert kernels['A'].operation_state().preparing == (preparation.reservation_id,)
    assert kernels['B'].operation_state().preparing == ()
    assert _rows(db, 'B') == before_b
    assert db.execute('PRAGMA foreign_key_check').fetchone() is None


def test_refresh_of_one_run_during_other_source_read_does_not_supersede_it(scenario):
    db, kernels = scenario
    def read():
        kernels['B'].refresh(lambda: _source('B', 'changed-during-read'))
        return _source('A', 'updated')
    status = kernels['A'].refresh(read)
    assert status['blocked'] is None
    assert status['observed_parent_revision'] != kernels['B'].source_status()['observed_parent_revision']
    assert db.execute("SELECT count(*) FROM source_observations WHERE run_id='A'").fetchone()[0] == 2
    assert db.execute("SELECT count(*) FROM source_observations WHERE run_id='B'").fetchone()[0] == 2


def test_started_scope_refresh_revokes_only_its_run_without_renewing_budget_or_preparation(ticket_scenario):
    db, kernels = ticket_scenario
    preparation = _once(kernels['A'], 'A', _ticket_source('A')).preparation
    assert preparation is not None
    before_b = _rows(db, 'B')
    budgets = [tuple(r) for r in db.execute("SELECT * FROM budgets WHERE run_id='A' ORDER BY subject,purpose")]
    policy = db.execute("SELECT policy FROM runs WHERE id='A'").fetchone()[0]
    status = kernels['A'].refresh(lambda: _ticket_source('A', body='changed requirement'))
    assert status['blocked'] == 'requeue_required'
    assert kernels['A'].operation_state().cancel == (preparation.reservation_id,)
    assert preparation.preparation_deadline == 700.
    assert [tuple(r) for r in db.execute("SELECT * FROM budgets WHERE run_id='A' ORDER BY subject,purpose")] == budgets
    assert db.execute("SELECT policy FROM runs WHERE id='A'").fetchone()[0] == policy
    assert _rows(db, 'B') == before_b


@pytest.mark.parametrize('table', ['source_observations', 'requirement_versions', 'issue_facts', 'dependency_facts', 'history'])
def test_refresh_write_failure_keeps_original_owner_poison_and_rolls_back_all_facts(ticket_scenario, table):
    import sqlite3
    db, kernels = ticket_scenario
    before = {run: _rows(db, run) for run in ('A', 'B')}
    db.set_authorizer(lambda operation, target, *_: sqlite3.SQLITE_DENY
        if operation == sqlite3.SQLITE_INSERT and target == table else sqlite3.SQLITE_OK)
    try:
        with pytest.raises(sqlite3.DatabaseError, match='not authorized'):
            kernels['A'].refresh(lambda: _ticket_source('A', body='new requirement'))
    finally:
        db.set_authorizer(None)
    assert kernels['A'].owners[-1].failed
    assert {run: _rows(db, run) for run in ('A', 'B')} == before


@pytest.mark.parametrize('callback', ['accept', 'fail'])
def test_same_run_late_audit_survives_owner_retirement_without_granting_execution(scenario, callback):
    db, kernels = scenario
    with kernels['A'].storage.transaction() as tx:
        run, job = kernels['A']._owned(tx, SUBJECT, _execution('A'))
        world = World(run['parent_revision'], run['graph_revision'], job['revision'], BASE, BASE)
        work = Work(_execution('A'), 'pending', SUBJECT, False, True, None, None)
        request = kernels['A']._prepare_effect(tx, run, job, work, world, 'workspace', before=BASE.sha)
    assert kernels['A']._dispatch(request.effect, work.execution, world, token=request.token)
    db.execute("UPDATE runs SET executor=NULL,epoch=2 WHERE id='A'")
    before_b = _rows(db, 'B')
    if callback == 'accept':
        with pytest.raises(KernelError, match='executor_fenced'):
            kernels['A'].accept_work(request, EffectResult('target', commit=BASE))
        assert db.execute("SELECT count(*) FROM history WHERE run_id='A' AND kind='effect_readback'").fetchone()[0] == 1
    else:
        result = kernels['A'].fail_work(work, ReadUnavailable('read_failed', 'controlled', write_returncode=23), request=request)
        assert result.reason == 'executor_fenced'
        assert db.execute("SELECT count(*) FROM effect_failures WHERE run_id='A'").fetchone()[0] == 1
    assert db.execute("SELECT status,receipt_id FROM effects WHERE run_id='A'").fetchone()[:] == ('sent', None)
    assert _rows(db, 'B') == before_b


def test_foreign_observation_queue_is_rejected_without_consuming_its_pending_members(scenario):
    from agent_run.core.operation_contract import _ObservationPass, _SentRead
    db, kernels = scenario
    member = _SentRead('effect', SUBJECT, 1, 1)
    observation = _ObservationPass(_execution('B'), 'action-B', [member])
    before = {run: _rows(db, run) for run in ('A', 'B')}
    with pytest.raises(KernelError, match='observation_binding'):
        kernels['A'].operation_state(observation)
    assert observation.pending == [member]
    assert {run: _rows(db, run) for run in ('A', 'B')} == before


def _return(kernel, preparation, result):
    """Synthetic physical facts only; no actual credentials, Worker or Host."""
    call = kernel.admit_agent(preparation).call
    assert call is not None and kernel.dispatch_agent(call)
    kernel.bind_thread(call, 'controlled-backend-' + call.thread_id)
    process = {'pid': 21, 'pgid': 21, 'start_token': 'synthetic-' + call.invocation_id,
        'host_pid': 10, 'host_start_token': 'synthetic-host'}
    kernel.agent_started(call, process)
    kernel.agent_source_ready(call)
    kernel.receive(call, wire_json(result))
    kernel.agent_exited(call, process, 0)
    return call


def _until_preparation(kernel, run, source, role, candidate, *, facts=None):
    from agent_run.core.operation_contract import EffectIO
    for _ in range(16):
        result = _once(kernel, run, source, facts=facts)
        if getattr(result, 'preparation', None) is not None:
            assert result.preparation.role == role
            return result.preparation
        if isinstance(result, EffectIO):
            observation = kernel.accept_requirements(kernel.begin_effect_observation(result.effect), source)
            observed = DeliveryFacts(BASE, BASE) if facts is None else facts
            observed = replace(observed, checks_requested=observation.query.read_checks,
                               checks=observed.checks if observation.query.read_checks else None)
            world, fence = kernel.accept_delivery(observation, observed)
            kernel.authorize_effect_observation(result.effect, world, fence, prepared=True, inspections=None)
            if result.effect.operation == 'candidate':
                receipt = EffectResult('target', commit=candidate)
            elif result.effect.operation == 'validation_checkout':
                receipt = EffectResult('target', commit=Commit(result.effect.payload['reviewed_commit'], candidate.tree))
            elif result.effect.operation == 'validation_cleanup':
                receipt = EffectResult('target')
            elif result.effect.operation == 'integrate':
                receipt = EffectResult('target', commit=candidate, expected_tree=candidate.tree)
            else:
                raise AssertionError('Unexpected physical request: ' + result.effect.operation)
            assert kernel.accept_work(result, receipt).kind == 'progress'
        else:
            assert result.kind == 'progress', result
    raise AssertionError('Expected bounded preparation')


def test_current_review_findings_and_incremental_range_survive_scoped_source_scheduling(ticket_scenario):
    from agent_run.model import Role
    from support.offline import review_result
    db, kernels = ticket_scenario
    source = _ticket_source('A')
    before_b = _rows(db, 'B')
    first_candidate, second_candidate = Commit('c' * 40, 'd' * 40), Commit('e' * 40, 'f' * 40)
    development = _until_preparation(kernels['A'], 'A', source, Role.DEVELOPMENT, first_candidate)
    _return(kernels['A'], development, {'result_kind': 'development', 'summary': 'First scoped candidate', 'human_blockers': None})
    review = _until_preparation(kernels['A'], 'A', source, Role.REVIEW, first_candidate)
    assert review.context['review_context']['review_range'] == {'base_sha': BASE.sha, 'candidate_sha': first_candidate.sha}
    _return(kernels['A'], review, review_result('fail'))
    repair = _until_preparation(kernels['A'], 'A', source, Role.DEVELOPMENT, first_candidate)
    assert repair.context['acceptance_artifact'] == review_result('fail')
    assert repair.purpose == development.purpose and repair.thread_id == development.thread_id
    assert repair.context['review_budget_context']['remaining_review_attempts'] == 2
    _return(kernels['A'], repair, {'result_kind': 'development', 'summary': 'Repair of original Findings', 'human_blockers': None})
    next_review = _until_preparation(kernels['A'], 'A', source, Role.REVIEW, second_candidate)
    assert next_review.context['review_context']['review_range'] == {
        'base_sha': first_candidate.sha, 'candidate_sha': second_candidate.sha}
    assert next_review.context['review_context']['facts']['previous_acceptance_artifact'] == review_result('fail')
    assert db.execute("SELECT developments_used,reviews_used,window FROM budgets WHERE run_id='A' AND subject='ticket:2' AND purpose='requirements' AND current=1").fetchone()[:] == (2, 1, 1)
    assert _rows(db, 'B') == before_b


def test_base_append_returns_parent_to_integration_and_complete_review_without_touching_sibling(scenario):
    from agent_run.model import Role
    from support.offline import review_result
    db, kernels = scenario
    source = _source('A')
    before_b = _rows(db, 'B')
    first_candidate = Commit('c' * 40, 'd' * 40)
    development = _until_preparation(kernels['A'], 'A', source, Role.DEVELOPMENT, first_candidate)
    _return(kernels['A'], development, {'result_kind': 'development', 'summary': 'First candidate', 'human_blockers': None})
    review = _until_preparation(kernels['A'], 'A', source, Role.REVIEW, first_candidate)
    _return(kernels['A'], review, review_result('pass'))
    appended, integrated = Commit('e' * 40, 'f' * 40), Commit('1' * 40, '2' * 40)
    facts = DeliveryFacts(appended, BASE, ancestry=((BASE.sha, appended.sha),))
    integration_review = _until_preparation(kernels['A'], 'A', source, Role.REVIEW, integrated, facts=facts)
    assert integration_review.purpose.value == 'integration'
    assert integration_review.context['review_context']['review_range'] == {
        'base_sha': appended.sha, 'candidate_sha': integrated.sha}
    assert integration_review.context['expected_tree'] == integrated.tree
    assert db.execute("SELECT integrated_sha,integration_target,phase,purpose FROM subjects WHERE run_id='A'").fetchone()[:] == (
        integrated.sha, appended.sha, 'review', 'integration')
    assert db.execute("SELECT developments_used,reviews_used,window FROM budgets WHERE run_id='A' AND purpose='requirements' AND current=1").fetchone()[:] == (1, 1, 1)
    assert db.execute("SELECT developments_used,reviews_used,window FROM budgets WHERE run_id='A' AND purpose='integration' AND current=1").fetchone()[:] == (0, 0, 1)
    assert _rows(db, 'B') == before_b


def test_unchanged_source_is_a_noop_even_when_sibling_has_the_latest_observation(scenario):
    db, kernels = scenario
    before = {run: _rows(db, run) for run in ('A', 'B')}
    assert db.execute('SELECT run_id FROM source_observations ORDER BY sequence DESC LIMIT 1').fetchone()[0] == 'B'
    kernels['A'].refresh(lambda: _source('A'))
    assert {run: _rows(db, run) for run in ('A', 'B')} == before


@pytest.mark.parametrize('has_wait', [False, True])
def test_successful_read_completion_checks_current_facts_without_a_writer(scenario, monkeypatch, has_wait):
    db, kernels = scenario
    kernel, execution = kernels['A'], _execution('A')
    kernel.clock = lambda: 100.25
    request = kernel.begin_observation(SUBJECT, execution=execution)
    if has_wait:
        kernel._wait_for_read(request.fence, execution, 'controlled_read', 'Original read unavailable')
    before = {run: _rows(db, run) for run in ('A', 'B')}
    transaction, writes = kernel.storage.transaction, []

    @contextmanager
    def readonly_completion(**options):
        writes.append(options.get('write', True))
        assert options.get('write') is False, 'Healthy read must not acquire a save ticket'
        db.execute('PRAGMA query_only=ON')
        try:
            with transaction(**options) as tx:
                yield tx
        finally:
            db.execute('PRAGMA query_only=OFF')

    monkeypatch.setattr(kernel.storage, 'transaction', readonly_completion)
    assert kernel._finish_operation_read(request.fence, execution) is None
    assert writes == [False]
    assert {run: _rows(db, run) for run in ('A', 'B')} == before


def test_expired_read_completion_records_original_fractional_window_under_ordered_writer(scenario, monkeypatch):
    db, kernels = scenario
    kernel, execution = kernels['A'], _execution('A')
    now = [100.25]
    kernel.clock = lambda: now[0]
    request = kernel.begin_observation(SUBJECT, execution=execution)
    kernel._wait_for_read(request.fence, execution, 'controlled_read', 'Original read unavailable')
    original = dict(db.execute("SELECT * FROM waits WHERE run_id='A'").fetchone())
    before_b = _rows(db, 'B')
    transaction, writes = kernel.storage.transaction, []

    @contextmanager
    def recorded(**options):
        writes.append((options.get('write', True), options.get('ordered', True)))
        with transaction(**options) as tx:
            yield tx

    monkeypatch.setattr(kernel.storage, 'transaction', recorded)
    now[0] = 700.25
    step = kernel._finish_operation_read(request.fence, execution)
    assert step is not None and step.reason == 'supervision_timeout'
    assert writes == [(False, True), (True, True)]
    window = dict(db.execute("SELECT * FROM waits WHERE run_id='A'").fetchone())
    assert window == original and (window['started_at'], window['deadline']) == (100.25, 700.25)
    assert db.execute("SELECT code FROM gates WHERE run_id='A'").fetchone()[0] == 'supervision_timeout'
    evidence = db.execute("SELECT m.body FROM material m JOIN gates g ON m.run_id=g.run_id AND m.id=g.evidence WHERE g.run_id='A'").fetchone()[0]
    import json
    detail = json.loads(evidence)
    assert (detail['started_at'], detail['deadline'], detail['latest_observation']) == (
        100.25, 700.25, original['evidence'])
    assert _rows(db, 'B') == before_b


@pytest.mark.parametrize('change', ['stop', 'source', 'owner', 'removed_wait', 'replaced_wait'])
def test_read_timeout_escalation_rechecks_changes_after_read_snapshot(scenario, monkeypatch, change):
    db, kernels = scenario
    kernel, execution = kernels['A'], _execution('A')
    kernel.clock = lambda: 100.25
    request = kernel.begin_observation(SUBJECT, execution=execution)
    kernel._wait_for_read(request.fence, execution, 'controlled_read', 'Original read unavailable')
    kernel.clock = lambda: 700.25
    transaction, changed = kernel.storage.transaction, False
    before_b = _rows(db, 'B')

    @contextmanager
    def interleaved(**options):
        nonlocal changed
        with transaction(**options) as tx:
            yield tx
        if not options.get('write', True) and not changed:
            assert not db.in_transaction
            changed = True
            if change == 'stop':
                kernel.control('stop', 'stop-between-read-and-timeout')
            elif change == 'source':
                kernel.refresh(lambda: _source('A', 'changed-before-timeout'))
            elif change == 'owner':
                db.execute("UPDATE runs SET executor='successor',epoch=epoch+1 WHERE id='A'")
            elif change == 'removed_wait':
                db.execute("DELETE FROM waits WHERE run_id='A'")
            else:
                db.execute("UPDATE waits SET started_at=700.25,deadline=1300.25 WHERE run_id='A'")

    monkeypatch.setattr(kernel.storage, 'transaction', interleaved)
    if change in {'stop', 'owner', 'source'}:
        with pytest.raises(KernelError, match='automatic_step_superseded' if change == 'source' else 'executor_fenced'):
            kernel._finish_operation_read(request.fence, execution)
    else:
        assert kernel._finish_operation_read(request.fence, execution) is None
        rows = db.execute("SELECT deadline FROM waits WHERE run_id='A'").fetchall()
        assert [row[0] for row in rows] == ([] if change == 'removed_wait' else [1300.25])
    assert changed and not db.execute("SELECT 1 FROM gates WHERE run_id='A' AND code='supervision_timeout'").fetchall()
    assert _rows(db, 'B') == before_b


@pytest.mark.parametrize('failure_at', ['open', 'close'])
def test_read_completion_storage_failure_preserves_original_error_without_writer(scenario, monkeypatch, failure_at):
    db, kernels = scenario
    kernel, execution = kernels['A'], _execution('A')
    kernel.clock = lambda: 100.25
    request = kernel.begin_observation(SUBJECT, execution=execution)
    kernel._wait_for_read(request.fence, execution, 'controlled_read', 'Original read unavailable')
    kernel.clock = lambda: 700.25
    transaction, writes = kernel.storage.transaction, []
    before = {run: _rows(db, run) for run in ('A', 'B')}
    failure = OSError('Original reader storage failure')

    @contextmanager
    def failed_exit(**options):
        writes.append(options.get('write', True))
        if failure_at == 'open':
            raise failure
        with transaction(**options) as tx:
            yield tx
        raise failure

    monkeypatch.setattr(kernel.storage, 'transaction', failed_exit)
    with pytest.raises(OSError) as caught:
        kernel._finish_operation_read(request.fence, execution)
    assert caught.value is failure
    assert writes == [False]
    assert {run: _rows(db, run) for run in ('A', 'B')} == before


@pytest.mark.parametrize('bound_execution', [True, False])
def test_foreign_source_fence_cannot_refresh_same_numbered_parent_in_selected_run(scenario, bound_execution):
    db, kernels = scenario
    request = kernels['B'].begin_observation(SUBJECT, execution=_execution('B') if bound_execution else None)
    before = {run: _rows(db, run) for run in ('A', 'B')}
    with pytest.raises(KernelError):
        kernels['A'].accept_requirements(request, _source('A', 'must not commit'))
    assert {run: _rows(db, run) for run in ('A', 'B')} == before


def test_source_consumption_checks_original_read_deadline_after_writer_admission(scenario, monkeypatch):
    db, kernels = scenario
    kernel, execution = kernels['A'], _execution('A')
    now = [100.25]
    kernel.clock = lambda: now[0]
    request = kernel.begin_observation(SUBJECT, execution=execution)
    kernel._wait_for_read(request.fence, execution, 'controlled_read', 'Original read unavailable')
    original = dict(db.execute("SELECT * FROM waits WHERE run_id='A'").fetchone())
    source = db.execute("SELECT source_sequence,observed_parent_revision FROM runs WHERE id='A'").fetchone()[:]
    before_b = _rows(db, 'B')
    transaction = kernel.storage.transaction
    now[0] = 700.20

    @contextmanager
    def delayed_writer(**options):
        if options.get('write', True):
            now[0] = 700.25
        with transaction(**options) as tx:
            yield tx

    monkeypatch.setattr(kernel.storage, 'transaction', delayed_writer)
    with pytest.raises(ReadWait) as expired:
        kernel.accept_requirements(request, _source('A', 'returned-before-save-admission'))
    assert expired.value.step.reason == 'supervision_timeout'
    assert db.execute("SELECT source_sequence,observed_parent_revision FROM runs WHERE id='A'").fetchone()[:] == source
    assert dict(db.execute("SELECT * FROM waits WHERE run_id='A'").fetchone()) == original
    assert db.execute("SELECT code FROM gates WHERE run_id='A'").fetchone()[0] == 'supervision_timeout'
    assert not db.execute("SELECT id FROM attempts WHERE run_id='A'").fetchall()
    assert _rows(db, 'B') == before_b


@pytest.mark.parametrize('has_wait', [False, True])
def test_observation_consumption_checks_and_uses_one_transaction(scenario, monkeypatch, has_wait):
    db, kernels = scenario
    kernel, execution = kernels['A'], _execution('A')
    kernel.clock = lambda: 100.25
    request = kernel.begin_observation(SUBJECT, execution=execution)
    if has_wait:
        kernel._wait_for_read(request.fence, execution, 'controlled_read', 'Original read unavailable')
    before = {run: _rows(db, run) for run in ('A', 'B')}
    transaction, modes = kernel.storage.transaction, []

    @contextmanager
    def recorded(**options):
        write = options.get('write', True)
        modes.append((write, options.get('ordered', True)))
        if not write:
            db.execute('PRAGMA query_only=ON')
        try:
            with transaction(**options) as tx:
                yield tx
        finally:
            db.execute('PRAGMA query_only=OFF')

    monkeypatch.setattr(kernel.storage, 'transaction', recorded)
    observation = kernel.accept_requirements(request, _source('A'))
    assert modes == [(False, True)]
    modes.clear()
    world, fence = kernel.accept_delivery(observation, DeliveryFacts(BASE, BASE))
    assert fence == observation.fence and world.source == BASE
    assert modes == [(False, True)]
    assert {run: _rows(db, run) for run in ('A', 'B')} == before


@pytest.mark.parametrize('writer_failure', [None, 'open', 'before_commit', 'after_commit'])
def test_delivery_timeout_commits_before_wait_and_preserves_writer_failure(scenario, monkeypatch, writer_failure):
    db, kernels = scenario
    kernel, execution = kernels['A'], _execution('A')
    kernel.clock = lambda: 100.25
    request = kernel.begin_observation(SUBJECT, execution=execution)
    observation = kernel.accept_requirements(request, _source('A'))
    kernel._wait_for_read(observation.fence, execution, 'controlled_read', 'Original read unavailable')
    original = dict(db.execute("SELECT * FROM waits WHERE run_id='A'").fetchone())
    before_b = _rows(db, 'B')
    transaction, modes = kernel.storage.transaction, []
    failure = OSError('Original timeout writer failure')
    kernel.clock = lambda: 700.25

    @contextmanager
    def recorded(**options):
        write = options.get('write', True)
        modes.append((write, options.get('ordered', True)))
        if write and writer_failure == 'open':
            raise failure
        with transaction(**options) as tx:
            yield tx
            if write and writer_failure == 'before_commit':
                raise failure
        if write and writer_failure == 'after_commit':
            raise failure

    monkeypatch.setattr(kernel.storage, 'transaction', recorded)
    if writer_failure:
        with pytest.raises(OSError) as caught:
            kernel.accept_delivery(observation, DeliveryFacts(BASE, BASE))
        assert caught.value is failure
    else:
        with pytest.raises(ReadWait) as expired:
            kernel.accept_delivery(observation, DeliveryFacts(BASE, BASE))
        assert expired.value.step.reason == 'supervision_timeout'
    if writer_failure in {None, 'after_commit'}:
        assert db.execute("SELECT code FROM gates WHERE run_id='A'").fetchone()[0] == 'supervision_timeout'
    else:
        assert not db.execute("SELECT code FROM gates WHERE run_id='A'").fetchall()
    assert modes == [(False, True), (True, True)]
    assert dict(db.execute("SELECT * FROM waits WHERE run_id='A'").fetchone()) == original
    assert _rows(db, 'B') == before_b


@pytest.mark.parametrize('change', ['stop', 'source', 'owner', 'removed_wait', 'replaced_wait'])
def test_delivery_consumption_rechecks_timeout_under_its_writer(scenario, monkeypatch, change):
    db, kernels = scenario
    kernel, execution = kernels['A'], _execution('A')
    kernel.clock = lambda: 100.25
    request = kernel.begin_observation(SUBJECT, execution=execution)
    observation = kernel.accept_requirements(request, _source('A'))
    kernel._wait_for_read(observation.fence, execution, 'controlled_read', 'Original read unavailable')
    kernel.clock = lambda: 700.25
    transaction, changed, modes = kernel.storage.transaction, False, []
    before_b = _rows(db, 'B')

    @contextmanager
    def interleaved(**options):
        nonlocal changed
        modes.append(options.get('write', True))
        with transaction(**options) as tx:
            yield tx
        if not options.get('write', True) and not changed:
            assert not db.in_transaction
            changed = True
            if change == 'stop':
                kernel.control('stop', 'stop-before-delivery-timeout')
            elif change == 'source':
                kernel.refresh(lambda: _source('A', 'changed-before-delivery-timeout'))
            elif change == 'owner':
                db.execute("UPDATE runs SET executor='successor',epoch=epoch+1 WHERE id='A'")
            elif change == 'removed_wait':
                db.execute("DELETE FROM waits WHERE run_id='A'")
            else:
                db.execute("UPDATE waits SET started_at=700.25,deadline=1300.25 WHERE run_id='A'")

    monkeypatch.setattr(kernel.storage, 'transaction', interleaved)
    if change in {'stop', 'owner', 'source'}:
        with pytest.raises(KernelError, match='automatic_step_superseded' if change == 'source' else 'executor_fenced'):
            kernel.accept_delivery(observation, DeliveryFacts(BASE, BASE))
    else:
        world, fence = kernel.accept_delivery(observation, DeliveryFacts(BASE, BASE))
        assert world.source == BASE and fence == observation.fence
        assert modes == [False, True]
        rows = db.execute("SELECT deadline FROM waits WHERE run_id='A'").fetchall()
        assert [row[0] for row in rows] == ([] if change == 'removed_wait' else [1300.25])
    assert changed and not db.execute("SELECT 1 FROM gates WHERE run_id='A' AND code='supervision_timeout'").fetchall()
    assert _rows(db, 'B') == before_b


@pytest.mark.parametrize('failure_at', ['open', 'close', 'consumer'])
def test_delivery_consumption_failure_does_not_retry_the_consumer_or_save(scenario, monkeypatch, failure_at):
    db, kernels = scenario
    kernel, execution = kernels['A'], _execution('A')
    kernel.clock = lambda: 100.25
    request = kernel.begin_observation(SUBJECT, execution=execution)
    observation = kernel.accept_requirements(request, _source('A'))
    kernel._wait_for_read(observation.fence, execution, 'controlled_read', 'Original read unavailable')
    if failure_at != 'consumer':
        kernel.clock = lambda: 700.25
    transaction, modes = kernel.storage.transaction, []
    before = {run: _rows(db, run) for run in ('A', 'B')}
    failure = OSError('Original observation storage failure')

    @contextmanager
    def failed(**options):
        modes.append(options.get('write', True))
        if failure_at == 'open':
            raise failure
        with transaction(**options) as tx:
            yield tx
        if failure_at == 'close':
            raise failure

    monkeypatch.setattr(kernel.storage, 'transaction', failed)
    if failure_at == 'consumer':
        with pytest.raises(ReadUnavailable, match='Required Checks demand'):
            kernel.accept_delivery(observation, DeliveryFacts(BASE, BASE, checks_requested=False))
    else:
        with pytest.raises(OSError) as caught:
            kernel.accept_delivery(observation, DeliveryFacts(BASE, BASE))
        assert caught.value is failure
    assert modes == [False]
    assert {run: _rows(db, run) for run in ('A', 'B')} == before
