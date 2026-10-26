"""Source/selection domain on real SQLite, not public creation or a Host.

Only complete frozen input/source prerequisites are supplied as relational data.
Subsequent observation, refresh and scheduler operations are existing Core APIs.
"""
from contextlib import closing
from dataclasses import asdict, replace

import pytest

from agent_run.core import kernel as core
from agent_run.core.operation_contract import DeliveryFacts, ObserveIO, ReadUnavailable, Work
from agent_run.errors import KernelError
from agent_run.model import Commit, EffectResult, Execution, ParentIssue, Policy, Requirements, World, wire_json
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
            world, fence = kernel.accept_delivery(observation, DeliveryFacts(BASE, BASE) if facts is None else facts)
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


@pytest.mark.parametrize('bound_execution', [True, False])
def test_foreign_source_fence_cannot_refresh_same_numbered_parent_in_selected_run(scenario, bound_execution):
    db, kernels = scenario
    request = kernels['B'].begin_observation(SUBJECT, execution=_execution('B') if bound_execution else None)
    before = {run: _rows(db, run) for run in ('A', 'B')}
    with pytest.raises(KernelError):
        kernels['A'].accept_requirements(request, _source('A', 'must not commit'))
    assert {run: _rows(db, run) for run in ('A', 'B')} == before
