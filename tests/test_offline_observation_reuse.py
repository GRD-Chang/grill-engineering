"""One ref sample per observation; later reads and write authority stay fresh."""
from dataclasses import replace

import pytest

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
def test_observation_returns_original_commits_and_ancestry(world, shared_head):
    port, commits, request = world
    if shared_head:
        assert port.physical.push(request.target_ref, commits[-2].sha, commits[-1].sha).returncode == 0
    observed = port.delivery(request, ('pass',), [])
    target = commits[-1] if shared_head else commits[-2]
    assert observed.source == commits[-1] and observed.target == target
    assert set(observed.ancestry) == {(before.sha, after.sha)
        for i, before in enumerate(commits) for after in commits[i:]}


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


def test_observation_samples_both_refs_before_transfer_and_rereads_next_time(world, monkeypatch):
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
    assert facts.source == commits[-1] and facts.target == commits[-2]
    assert port.delivery(request, ('pass',), []).target == commits[-1]


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


def test_missing_remote_object_is_transferred_without_moving_local_refs(world):
    port, commits, _ = world
    physical = port.physical
    original_refs = port.git.run('show-ref').stdout
    sha = physical._run(physical.remote, 'commit-tree', commits[0].tree,
                        '-p', commits[-1].sha, input='Remote-only commit\n').stdout.strip()
    assert port.git.run('cat-file', '-e', sha + '^{commit}', check=False).returncode != 0
    physical.fetch(sha)
    assert port.git.commit(sha).tree == commits[0].tree
    assert port.git.run('show-ref').stdout == original_refs
    assert physical.commit(sha)['message'] == 'Remote-only commit\n'


@pytest.mark.parametrize("object_kind", ["missing", "tree", "blob"])
def test_commit_batch_rejects_missing_or_non_commit_objects(world, object_kind):
    port, commits, _ = world
    head = ("f" * 40 if object_kind == "missing" else commits[0].tree if object_kind == "tree"
            else port.git.run("rev-parse", commits[0].sha + ":README.md").stdout.strip())
    with pytest.raises(KernelError, match="git_operation_failed"):
        port.physical.read_commits((head,))


@pytest.mark.parametrize("fault", ["truncated", "extra", "identity", "tree"])
def test_commit_batch_rejects_malformed_output(world, monkeypatch, fault):
    port, commits, _ = world
    physical = port.physical
    original = physical._run
    def malformed(directory, *args, **kwargs):
        result = original(directory, *args, **kwargs)
        if args[:2] == ("cat-file", "--batch-check=%(objectname) %(objecttype)"):
            lines = result.stdout.splitlines()
            if fault == "truncated":
                lines.pop()
            elif fault == "extra":
                lines.append(lines[0])
            elif fault == "identity":
                lines[0] = commits[1].sha + " commit"
            else:
                lines[1] = "not-an-oid tree"
            result.stdout = "\n".join(lines) + "\n"
        return result
    monkeypatch.setattr(physical, "_run", malformed)
    with pytest.raises(KernelError, match="git_operation_failed"):
        physical.read_commits((commits[0].sha,))
