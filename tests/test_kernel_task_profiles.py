"""A managed profile change belongs to the exact published Task."""

from support.repository_fixture import create_kernel
from dataclasses import replace
from pathlib import Path

import pytest

from agent_run.contracts.task import TaskKey
from agent_run.core.kernel import Kernel
from agent_run.errors import KernelError
from support.offline import GraphScenario
from support.repository_fixture import foreign_task_run
from agent_run.storage.routing import RepositoryRoute
import sqlite3
from support.requirements_scenarios import independent_source
from support.core_operations import CoreOperations


@pytest.mark.parametrize('binding', ['current', 'unbound', 'foreign'])
def test_task_profile_revision_requires_exact_binding_and_only_changes_new_threads(
    tmp_path: Path, binding: str,
) -> None:
    source = independent_source()
    # This contract concerns one Task's Thread/Profile binding, not sibling scheduling.
    source = replace(source, parent=replace(source.parent, sub_issue_numbers=(3,)),
                     issues=tuple(issue for issue in source.issues if issue.number == 3))
    if binding == 'unbound':
        # An idle Task has no Run or Executor to prepare. Exercise its real
        # metadata producer rather than first starting an unrelated Scenario.
        task = TaskKey(tmp_path / 'repository', 'example/project', source.parent.number)
        route = RepositoryRoute(tmp_path / 'idle' / 'kernel.sqlite3', task.workspace, task.repository)
        route.initialize()
        route.idle(task)
        before = route.database.read_bytes()
        with pytest.raises(KernelError, match='missing_or_ambiguous_fact'):
            route.kernel('unallocated-profile-run')
        assert route.database.read_bytes() == before
        assert route.task(task).current_run is None
        with route.metadata().transaction(write=False) as tx:
            for table in ('runs', 'invocations', 'actions', 'profile_revisions'):
                assert tx.one('SELECT count(*) FROM ' + table)[0] == 0
        return
    scenario = GraphScenario(tmp_path / 'source', source)
    task = TaskKey(Path(scenario.spec.repository_root), scenario.spec.repository, scenario.spec.parent_issue)
    kernel = create_kernel(tmp_path / 'profile.sqlite3', scenario.spec, requirements=scenario.requirements)
    if binding == 'foreign':
        other = foreign_task_run(kernel, scenario.spec, scenario.requirements)
        with pytest.raises(sqlite3.IntegrityError):
            with RepositoryRoute.from_database(kernel.storage.path).metadata().transaction(ordered=False) as tx:
                tx.execute('UPDATE tasks SET current_run=? WHERE id=?', ('other-run', task.fingerprint))
        with kernel.storage.transaction(write=False) as tx:
            assert tx.one('SELECT profile_revision FROM runs WHERE id=?', (kernel.storage.run_id,))[0] == 1
            assert tx.one('SELECT count(*) FROM actions WHERE run_id=?', (kernel.storage.run_id,))[0] == 0
            assert tx.one('SELECT count(*) FROM profile_revisions WHERE run_id=?', (kernel.storage.run_id,))[0] == 1
        assert other.storage.run_id != kernel.storage.run_id
        return
    scenario.kernel = kernel
    scenario.execution = kernel.claim_executor('profile-executor')
    operations = CoreOperations(kernel, scenario.execution, scenario.make_ports)
    for _ in range(20):
        step = operations.once()
        if step.call is not None:
            first = step.call
            break
    else:
        raise AssertionError('The original Task did not allocate its Development Thread')
    assert first.role == 'development' and first.profile_revision == 1
    kernel.bind_thread(first, 'original-backend-thread')
    with kernel.storage.transaction(write=False) as tx:
        old_thread = dict(tx.one('SELECT * FROM threads WHERE id=?', (first.thread_id,)))
        old_invocation = dict(tx.one('SELECT * FROM invocations WHERE id=?', (first.invocation_id,)))
    assert kernel.configure_task_profiles('choose-new-model', overrides={'development_model': 'next-thread-model'}) == 2
    assert kernel.configure_task_profiles('choose-new-model', overrides={'development_model': 'next-thread-model'}) == 2
    with kernel.storage.transaction(write=False) as tx:
        assert dict(tx.one('SELECT * FROM threads WHERE id=?', (first.thread_id,))) == old_thread
        assert dict(tx.one('SELECT * FROM invocations WHERE id=?', (first.invocation_id,))) == old_invocation
        assert tx.one('SELECT count(*) FROM hosts')[0] == 0
    human = b'{"result_kind":"human_blocker","summary":null,"human_blockers":["Need a decision"]}'
    kernel.receive(first, human)
    assert operations.once().kind == 'paused'
    kernel.control('resume', 'keep-existing-thread', subject=first.subject)
    resumed = operations.once().call
    assert resumed is not None and resumed.thread_id == first.thread_id
    assert resumed.profile_revision == 1 and resumed.profile == first.profile
    kernel.receive(resumed, human)
    assert operations.once().kind == 'paused'
    kernel.control('resume', 'use-new-thread', subject=first.subject, new_thread=True)
    changed = operations.once().call
    assert changed is not None and changed.thread_id != first.thread_id and changed.attempt_id == first.attempt_id
    assert changed.profile_revision == 2 and changed.profile.model == 'next-thread-model'
