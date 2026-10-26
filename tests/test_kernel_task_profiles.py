"""A managed profile change belongs to the exact published Task."""
from dataclasses import replace
from pathlib import Path

import pytest

from agent_run.contracts.task import TaskKey
from agent_run.core.kernel import Kernel
from agent_run.errors import KernelError
from support.offline import GraphScenario
from agent_run.storage.tasks import bind_task, locked_task
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
    scenario = GraphScenario(tmp_path / 'source', source)
    create = Kernel.create if binding == 'unbound' else Kernel.create_task
    kernel = create(tmp_path / 'profile.sqlite3', scenario.spec, requirements=scenario.requirements)
    task = TaskKey(Path(scenario.spec.repository_root), scenario.spec.repository, scenario.spec.parent_issue)
    if binding == 'foreign':
        other = Kernel.create(tmp_path / 'other.sqlite3', replace(scenario.spec, run_id='other-run'),
                              requirements=scenario.requirements)
        with locked_task(task) as location:
            bind_task(location, 'other-run', other.storage.path)
    if binding != 'current':
        with pytest.raises(KernelError, match='task_location_identity'):
            kernel.configure_task_profiles('choose-new-model', overrides={'development_model': 'next-thread-model'})
        with kernel.storage.transaction(write=False) as tx:
            assert tx.one('SELECT profile_revision FROM runs')[0] == 1
            assert tx.one('SELECT count(*) FROM actions')[0] == 0
            assert tx.one('SELECT count(*) FROM profile_revisions')[0] == 1
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
