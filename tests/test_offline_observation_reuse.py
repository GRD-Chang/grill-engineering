"""One observation reuses preparation, never refs, ownership or write authority."""
from dataclasses import replace
import json
import resource
import time

import pytest

from agent_run.adapters import git_objects
from agent_run.core.operation_contract import DeliveryRead
from agent_run.errors import KernelError
from support.offline import OfflinePort
from support import offline_git


@pytest.fixture
def world(tmp_path):
    port = OfflinePort.create(tmp_path / 'world')
    base = port.git.commit('HEAD')
    commits = [base]
    for index in range(3):
        commits.append(port.git.make_commit(base.tree, (commits[-1].sha,), f'change {index}'))
    physical = port.physical
    assert physical.push('refs/heads/source', None, commits[-1].sha).returncode == 0
    assert physical.push('refs/heads/target', None, commits[-2].sha).returncode == 0
    return port, tuple(commits), DeliveryRead('refs/heads/source', 'refs/heads/target', None,
        tuple(commit.sha for commit in commits), read_pr=False)


@pytest.mark.parametrize('shared_head', [False, True])
def test_observation_reuses_one_adapter_but_keeps_each_physical_read(world, monkeypatch, record_property, shared_head):
    port, commits, request = world
    if shared_head:
        assert port.physical.push(request.target_ref, commits[-2].sha, commits[-1].sha).returncode == 0
    original_refs, original_fetch = offline_git.OfflineGit.refs, offline_git.OfflineGit.fetch
    order, adapters, commands = [], [], []
    def refs(adapter):
        adapters.append(adapter)
        order.append(('refs', None))
        return original_refs(adapter)
    def fetch(adapter, sha):
        adapters.append(adapter)
        order.append(('fetch', sha))
        return original_fetch(adapter, sha)
    def count(original):
        def run(arguments, **kwargs):
            commands.append(tuple(arguments))
            return original(arguments, **kwargs)
        return run
    monkeypatch.setattr(offline_git.OfflineGit, 'refs', refs)
    monkeypatch.setattr(offline_git.OfflineGit, 'fetch', fetch)
    monkeypatch.setattr(offline_git, 'run_git', count(offline_git.run_git))
    monkeypatch.setattr(git_objects, 'run_git', count(git_objects.run_git))
    start, cpu = time.perf_counter(), time.process_time()
    children = resource.getrusage(resource.RUSAGE_CHILDREN)
    observed = port.delivery(request, ('pass',), [])
    metrics = {'git_commands': len(commands), 'wall_seconds': time.perf_counter() - start,
        'parent_cpu_seconds': time.process_time() - cpu}
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    metrics['reaped_child_cpu_seconds'] = after.ru_utime + after.ru_stime - children.ru_utime - children.ru_stime
    record_property('observation_cost', json.dumps(metrics, sort_keys=True))
    target = commits[-1] if shared_head else commits[-2]
    assert observed.source == commits[-1] and observed.target == target
    assert set(observed.ancestry) == {(before.sha, after.sha)
        for i, before in enumerate(commits) for after in commits[i:]}
    assert order == [('refs', None), ('fetch', commits[-1].sha), ('refs', None), ('fetch', target.sha)]
    # Reusing preparation is the cost contract; every original physical call
    # still runs and revalidates ownership. Even an equal head is fetched twice.
    assert len({id(adapter) for adapter in adapters}) == 1


def test_later_observations_keep_fresh_refs_and_pr_facts(world, monkeypatch):
    port, commits, request = world
    from agent_run.model import PullRequest
    original = PullRequest(7, 'example/repository', request.source_ref, request.target_ref,
        commits[-1], commits[-2], 'title', 'body', actor='offline-publisher', head_repository='example/repository')
    current = [original]
    monkeypatch.setattr(port, 'pr_by_number', lambda number: current[0])
    request = replace(request, pr_number=7, read_pr=True)
    first = port.delivery(request, ('fail', 'pass'), [])
    new_head = port.git.make_commit(commits[0].tree, (commits[-1].sha,), 'fresh source')
    assert port.physical.push(request.source_ref, commits[-1].sha, new_head.sha).returncode == 0
    assert port.physical.push(request.target_ref, commits[-2].sha, commits[-1].sha).returncode == 0
    current[0] = replace(original, head=new_head, base=commits[-1], title='new original title')
    second = port.delivery(request, ('fail', 'pass'), [commits[-1].sha])
    assert first.source == original.head and first.target == original.base and first.pr == original
    assert second.source == new_head and second.target == commits[-1] and second.pr == current[0]
    assert first.checks.result == 'fail' and second.checks.result == 'pass'


def test_second_ref_read_still_observes_an_independent_append_after_first_fetch(world, monkeypatch):
    port, commits, request = world
    original = offline_git.OfflineGit.fetch
    calls = []
    def fetch(adapter, sha):
        original(adapter, sha)
        calls.append(sha)
        if len(calls) == 1:
            assert port.physical.push(request.target_ref, commits[-2].sha, commits[-1].sha).returncode == 0
    monkeypatch.setattr(offline_git.OfflineGit, 'fetch', fetch)
    facts = port.delivery(request, ('pass',), [])
    assert facts.source == facts.target == commits[-1]
    assert calls == [commits[-1].sha, commits[-1].sha]


@pytest.mark.parametrize('fault', ['include', 'rewrite', 'symlink', 'alternates'])
@pytest.mark.parametrize('stage', ['after-first-refs', 'next-observation'])
def test_reused_adapter_rechecks_current_private_repository_ownership(world, monkeypatch, tmp_path, fault, stage):
    port, _commits, request = world
    def corrupt():
        directory = port.root / 'remote.git'
        if fault == 'include':
            with (directory / 'config').open('a') as stream:
                stream.write('\n[include]\n\tpath = /unread-offline-config\n')
        elif fault == 'rewrite':
            with (directory / 'config').open('a') as stream:
                stream.write('\n[url "file:///unread-offline-repository"]\n\tinsteadOf = offline\n')
        elif fault == 'symlink':
            (directory / 'refs' / 'foreign').symlink_to(tmp_path / 'foreign-ref')
        else:
            (directory / 'objects' / 'info' / 'alternates').write_text(str(tmp_path / 'foreign-objects') + '\n')
    if stage == 'next-observation':
        port.delivery(request, ('pass',), [])
        corrupt()
    else:
        original = offline_git.OfflineGit.refs
        def refs(adapter):
            result = original(adapter)
            corrupt()
            return result
        monkeypatch.setattr(offline_git.OfflineGit, 'refs', refs)
    with pytest.raises(ValueError, match='unsupported offline Git configuration|must not redirect|self-contained'):
        port.delivery(request, ('pass',), [])
    assert not (tmp_path / 'foreign-ref').exists() and not (tmp_path / 'foreign-objects').exists()


def test_unknown_ancestry_remains_a_failure_after_successful_observation(world):
    port, _commits, request = world
    port.delivery(request, ('pass',), [])
    with pytest.raises(KernelError, match='git_ancestry_unknown'):
        port.delivery(replace(request, anchors=('f' * 40,)), ('pass',), [])
