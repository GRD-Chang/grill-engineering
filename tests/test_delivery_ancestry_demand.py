"""Directed read demand; real private Git plus the existing scoped Core seam.

No native Host, Worker, save-clock or external GitHub service is exercised.
"""
from collections import Counter
from contextlib import closing, ExitStack
from dataclasses import replace
import sqlite3

import pytest

from agent_run.core.operation_contract import DeliveryFacts, DeliveryRead, ReadUnavailable, requested_ancestry
from agent_run.core.operation_runner import OperationRunner
from agent_run.errors import KernelError, ObservationSuperseded
from agent_run.model import Commit, Issue, ParentIssue, Requirements
from support.core_operations import Ports, prepare_scripted_agent, settle_agent
from support.offline import GraphScenario, OfflinePort
from support.remote_effects import _fixture
from support.requirements_scenarios import source_run
from support.repository_fixture import foreign_task_run
from support.run_scoped_kernel import ScopedKernel


def test_ten_completion_edges_replace_the_full_relation_without_losing_facts(tmp_path, monkeypatch):
    port, git, effect, _, _ = _fixture(tmp_path, monkeypatch)
    ancestors, parent = [], effect.base.sha
    for index in range(10):
        parent = git.make_commit(effect.base.tree, (parent,), f'Completion {index}').sha
        ancestors.append(parent)
    head = git.make_commit(effect.base.tree, (parent,), 'Current Run append')
    git.run('push', 'origin', head.sha + ':' + effect.source_ref)
    query = DeliveryRead(effect.source_ref, effect.target_ref, None, tuple(ancestors), read_pr=False)
    calls, run = [], port.git.run
    def counted(*args, **kwargs):
        calls.append(args)
        return run(*args, **kwargs)
    monkeypatch.setattr(port.git, 'run', counted)
    monkeypatch.setattr(port, '_find_pr', lambda *a, **k: pytest.fail('Final activation must not read a PR'))
    complete = port.delivery(query)
    assert Counter(call[0] for call in calls)['merge-base'] == 12 * 11
    calls.clear()
    demand = tuple((sha, 'source') for sha in ancestors)
    sparse = port.delivery(replace(query, ancestry_pairs=demand + demand))
    expected = tuple(sorted((sha, head.sha) for sha in ancestors))
    assert sparse.ancestry_checked == sparse.ancestry == expected
    assert set(sparse.ancestry) <= set(complete.ancestry)
    assert (sparse.source, sparse.target, sparse.pr, sparse.checks) == (
        complete.source, complete.target, None, None)
    assert Counter(call[0] for call in calls) == {'cat-file': 12, 'merge-base': 10}
    assert not any((head.sha, sha) in sparse.ancestry for sha in ancestors)
    # Next observation sees a force push; a checked false edge is not unknown.
    git.run('push', '--force', 'origin', effect.base.sha + ':' + effect.source_ref)
    changed = port.delivery(replace(query, ancestry_pairs=demand))
    assert changed.source == effect.base
    assert changed.ancestry == ()
    assert changed.ancestry_checked == tuple(sorted((sha, effect.base.sha) for sha in ancestors))


@pytest.mark.parametrize('pairs', [(), (('source', 'source'),)])
def test_explicit_empty_and_reflexive_demand_do_not_expand_to_all_pairs(tmp_path, monkeypatch, pairs):
    port, _, effect, _, _ = _fixture(tmp_path, monkeypatch)
    facts = port.delivery(DeliveryRead(effect.source_ref, effect.target_ref, None,
        (effect.after_sha,), read_pr=False, ancestry_pairs=pairs))
    expected = () if not pairs else ((effect.base.sha, effect.base.sha),)
    assert facts.ancestry == facts.ancestry_checked == expected


@pytest.mark.parametrize('endpoint', ['refs/heads/main', 'unknown_ref', 'a' * 39, 'A' * 40])
def test_bad_demand_is_rejected_before_external_reads(tmp_path, monkeypatch, endpoint):
    port, _, effect, _, _ = _fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(port, '_commit', lambda *a: pytest.fail('Invalid demand reached GitHub'))
    with pytest.raises(ReadUnavailable, match='exact commit identities'):
        port.delivery(DeliveryRead(effect.source_ref, effect.target_ref, None, (),
            read_pr=False, ancestry_pairs=((endpoint, 'source'),)))


def test_missing_source_cannot_be_replaced_with_a_local_head(tmp_path, monkeypatch):
    port, _, effect, _, _ = _fixture(tmp_path, monkeypatch)
    facts = port.delivery(DeliveryRead('refs/heads/missing', effect.target_ref, None,
        (effect.base.sha,), read_pr=False, ancestry_pairs=((effect.base.sha, 'source'),)))
    assert facts.source is None and facts.target == effect.base
    assert facts.ancestry_checked is None and facts.ancestry == ()


@pytest.mark.parametrize('failure', ['missing-self', 'merge-error'])
def test_requested_unavailable_relation_is_never_reported_as_false(tmp_path, monkeypatch, failure):
    port, _, effect, _, _ = _fixture(tmp_path, monkeypatch)
    if failure == 'missing-self':
        pairs = (('f' * 40, 'f' * 40),)
    else:
        pairs = ((effect.after_sha, 'source'),)
        def unknown(*args):
            raise KernelError('git_ancestry_unknown')
        monkeypatch.setattr(port.git, 'is_ancestor', unknown)
    with pytest.raises((ReadUnavailable, KernelError)):
        port.delivery(DeliveryRead(effect.source_ref, effect.target_ref, None, (),
            read_pr=False, ancestry_pairs=pairs))


def test_offline_port_uses_the_same_demand_and_checks_reflexive_objects(tmp_path, monkeypatch):
    port = OfflinePort.create(tmp_path / 'offline')
    base = port.git.commit('refs/heads/main')
    query = DeliveryRead('refs/heads/main', 'refs/heads/main', None, (), read_pr=False,
        ancestry_pairs=((base.sha, 'source'),))
    monkeypatch.setattr(port, 'pull_request_by_ref', lambda *a: pytest.fail('Unexpected PR read'))
    facts = port.delivery(query, ['none'], [])
    assert facts.ancestry_checked == facts.ancestry == ((base.sha, base.sha),)
    missing = 'f' * 40
    with pytest.raises(KernelError, match='git_ancestry_unknown'):
        port.delivery(replace(query, ancestry_pairs=((missing, missing),)), ['none'], [])


def test_core_requires_explicit_coverage_even_for_empty_final_demand(tmp_path):
    original, source = source_run(tmp_path / 'kernel.sqlite3')
    with closing(sqlite3.connect(original.storage.path, isolation_level=None)) as db:
        db.row_factory = sqlite3.Row
        kernel = ScopedKernel(db, original.storage.run_id)
        request = kernel.accept_requirements(kernel.begin_observation(None), source)
        assert request.query.ancestry_pairs == () and not request.query.read_pr
        base = Commit('a' * 40, 'b' * 40)
        facts = DeliveryFacts(base, base)
        before = tuple(db.iterdump())
        with pytest.raises(ReadUnavailable, match='exactly observed'):
            kernel.accept_delivery(request, facts)
        world, fence = kernel.accept_delivery(request, replace(facts, ancestry_checked=()))
        assert world.source == world.target == base and world.ancestry == ()
        assert fence == request.fence and tuple(db.iterdump()) == before
        assert kernel.source_status()['final_subject'] is None


def test_current_completion_demand_is_fenced_before_acceptance_and_activation(tmp_path, monkeypatch):
    source = Requirements(ParentIssue(1, 'Parent', 'Both tickets', (2, 3)), tuple(
        Issue(number, 'Ticket', f'Required {number}', 'OPEN', frozenset({'ready-for-agent'}))
        for number in (2, 3)))
    from support import offline
    create = offline.create_kernel
    with ExitStack() as owned:
        connections = []
        def decision_producer(*args, **kwargs):
            # The ordinary atomic Task/Run creator commits first. Attach the
            # existing decision seam before GraphScenario claims its Executor.
            original = create(*args, **kwargs)
            db = owned.enter_context(closing(sqlite3.connect(original.storage.path, isolation_level=None)))
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA foreign_keys=ON')
            connections.append(db)
            kernel = ScopedKernel(db, original.storage.run_id)
            kernel.storage.path, kernel.storage.run_id = original.storage.path, original.storage.run_id
            return kernel
        monkeypatch.setattr(offline, 'create_kernel', decision_producer)
        scenario = GraphScenario(tmp_path / 'graph', source)
        kernel = scenario.kernel
        assert len(connections) == 1
        db = connections[0]
        ports = Ports(scenario)
        original_delivery, queries = ports.delivery, []
        def observe(query):
            queries.append(query)
            return original_delivery(query)
        monkeypatch.setattr(ports, 'delivery', observe)
        runner = OperationRunner(kernel, scenario.execution, lambda _: ports)
        for _ in range(140):
            step = prepare_scripted_agent(kernel, runner.run_once())
            if step.call is not None:
                settle_agent(scenario, runner, step.call)
            status = kernel.status('ticket:3')
            pending = status['pending_write']
            if pending and pending['operation'] == 'close' and pending['status'] == 'observed':
                break
        else:
            pytest.fail('Normal producer did not reach the second close receipt')
        assert len(kernel.source_status()['completions']) == 1
        # Every Ticket uses explicit demand; publication preserves the reverse
        # target -> stored source edge used to select real commit parents.
        assert queries and all(query.read_pr and query.ancestry_pairs is not None for query in queries)
        assert all(("pr_base", "target") in query.ancestry_pairs for query in queries)
        assert any(before == "target" and after not in {"source", "target", "pr_base"}
                   for query in queries for before, after in query.ancestry_pairs)
        query = kernel.accept_requirements(kernel.begin_observation(None, execution=scenario.execution),
            scenario.port.source(source, 1))
        facts = scenario.port.delivery(query.query, ['none'], [])
        assert len(query.query.ancestry_pairs) == 1
        sibling = foreign_task_run(kernel, scenario.spec, source)
        other = ScopedKernel(db, sibling.storage.run_id)
        other_source = replace(source, parent=replace(source.parent, number=source.parent.number + 10000))
        other_query = other.accept_requirements(other.begin_observation(None), other_source)
        assert other_query.query.anchors == other_query.query.ancestry_pairs == ()
        before = tuple(db.iterdump())
        for incomplete in (None, (), tuple(tuple(reversed(pair)) for pair in facts.ancestry_checked)):
            with pytest.raises(ReadUnavailable, match='exactly observed'):
                kernel.accept_delivery(query, replace(facts, ancestry_checked=incomplete))
        assert tuple(db.iterdump()) == before
        world, fence = kernel.accept_delivery(query, facts)
        source_sequence = kernel.source_status()['observation']
        step = runner.run_once()
        assert step.kind == 'progress'
        assert len(kernel.source_status()['completions']) == 2
        assert kernel.source_status()['observation'] == source_sequence
        for consume in (lambda: kernel.accept_delivery(query, facts),
                lambda: kernel._activate_final(scenario.execution, world.source, world.target,
                    ancestry=world.ancestry, token=fence)):
            with pytest.raises(ObservationSuperseded, match='automatic_step_superseded'):
                consume()
        assert kernel.source_status()['final_subject'] is None
        # A fresh observation requests both exact Completion edges. A checked
        # negative answer remains negative, never an implicit missing proof.
        current = kernel.accept_requirements(kernel.begin_observation(None, execution=scenario.execution),
            scenario.port.source(source, 1))
        observed = scenario.port.delivery(current.query, ['none'], [])
        assert len(current.query.ancestry_pairs) == 2
        assert set(observed.ancestry_checked) == set(observed.ancestry)
        non_reflexive = next(pair for pair in observed.ancestry if pair[0] != pair[1])
        negative = replace(observed, ancestry=tuple(pair for pair in observed.ancestry if pair != non_reflexive))
        world, token = kernel.accept_delivery(current, negative)
        before = tuple(db.iterdump())
        with pytest.raises(KernelError, match='completion_not_in_run'):
            kernel._activate_final(scenario.execution, world.source, world.target,
                ancestry=world.ancestry, token=token)
        assert tuple(db.iterdump()) == before
        calls = list(scenario.role_calls)
        for _ in range(24):
            step = runner.run_once()
            assert step.call is None and step.preparation is None
            if step.reason == 'final_activated':
                break
        else:
            pytest.fail('Normal cleanup and fresh Final demand did not activate')
        assert kernel.delivery(step.subject).base == observed.source
        assert scenario.role_calls == calls


def test_requested_relation_accepts_full_sha256_without_accepting_partial_ids():
    sha = 'a' * 64
    query = DeliveryRead('source-ref', 'target-ref', None, (), read_pr=False,
        ancestry_pairs=((sha, 'source'),))
    assert requested_ancestry(query, Commit('b' * 64, 'c' * 64)) == ((sha, 'b' * 64),)


@pytest.mark.parametrize('pairs', [('source',), (('source',),), (('source', 'source', 'source'),), (([], 'source'),)])
def test_malformed_pair_shape_is_an_explicit_read_failure(pairs):
    with pytest.raises(ReadUnavailable, match='directed pairs'):
        requested_ancestry(DeliveryRead('a', 'b', None, (), ancestry_pairs=pairs), None)


def test_unresolved_edges_do_not_hide_independent_checks():
    base, head = Commit("a" * 40, "b" * 40), Commit("c" * 40, "d" * 40)
    query = DeliveryRead("source", "target", None, (), ancestry_pairs=(
        ("pr_base", "target"), ("target", head.sha), (base.sha, "source")))
    assert requested_ancestry(query, None, base) == ((base.sha, head.sha),)
    assert requested_ancestry(replace(query, ancestry_pairs=(("pr_base", "target"),)),
                              None, Commit("base", "tree")) is None
    assert requested_ancestry(replace(query, ancestry_pairs=()), None) == ()
    with pytest.raises(ReadUnavailable, match="exact commit identities"):
        requested_ancestry(replace(query, ancestry_pairs=(("not-an-oid", "source"),)), None)
