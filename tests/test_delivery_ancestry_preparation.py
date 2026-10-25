"""Per-observation object preparation keeps all exact directed ancestry facts."""
from collections import Counter
import subprocess
from types import SimpleNamespace

import pytest

from agent_run.adapters.git_objects import LocalGit
from agent_run.core.operation_contract import DeliveryRead, ReadUnavailable
from agent_run.errors import GitHubReadError, KernelError
from test_kernel_remote_effects import _fixture


def query(effect, anchors):
    return DeliveryRead(effect.source_ref, effect.target_ref, None, tuple(anchors), read_pr=False)


def counted(port, monkeypatch):
    calls, run = [], port.git.run
    def record(*args, **kwargs):
        calls.append(args)
        return run(*args, **kwargs)
    monkeypatch.setattr(port.git, 'run', record)
    return calls


@pytest.mark.parametrize('count', [3, 6])
def test_delivery_prepares_each_exact_sha_once(tmp_path, monkeypatch, count):
    port, git, effect, _, _ = _fixture(tmp_path, monkeypatch)
    heads = [effect.base.sha, effect.after_sha]
    while len(heads) < count:
        heads.append(git.make_commit(effect.candidate.tree, (heads[-1],), 'Exact object, same tree').sha)
    heads.sort()
    calls = counted(port, monkeypatch)
    old = tuple((a, b) for a in heads for b in heads if port._ancestor(a, b))
    assert len(calls) == 3 * count * (count - 1)
    calls.clear()
    facts = port.delivery(query(effect, heads + heads))
    assert facts.ancestry == old
    assert facts.source == facts.target == effect.base
    assert Counter(c[0] for c in calls) == {'cat-file': count, 'merge-base': count * (count - 1)}
    # Independent real-Git oracle, including both directions and self pairs.
    for a in heads:
        for b in heads:
            result = subprocess.run(['git', 'merge-base', '--is-ancestor', a, b], cwd=git.root,
                                    capture_output=True, timeout=5)
            assert result.returncode in (0, 1)
            assert ((a, b) in facts.ancestry) == (result.returncode == 0)


def test_next_observation_rechecks_objects_and_fresh_refs(tmp_path, monkeypatch):
    port, git, effect, _, _ = _fixture(tmp_path, monkeypatch)
    calls = counted(port, monkeypatch)
    first = port.delivery(query(effect, (effect.after_sha,)))
    prepared = [c for c in calls if c[0] == 'cat-file']
    git.run('push', 'origin', effect.after_sha + ':' + effect.source_ref)
    calls.clear()
    second = port.delivery(query(effect, (effect.after_sha,)))
    assert first.source == effect.base and second.source == effect.candidate
    assert first.target == second.target == effect.base
    assert [c for c in calls if c[0] == 'cat-file'] == prepared


def test_missing_object_fetch_is_exact_and_does_not_move_refs(tmp_path, monkeypatch):
    port, git, effect, _, _ = _fixture(tmp_path, monkeypatch)
    remote = LocalGit(tmp_path / 'origin.git')
    remote.run('config', 'user.name', 'Remote fixture')
    remote.run('config', 'user.email', 'remote@example.invalid')
    missing = remote.make_commit(effect.base.tree, (effect.base.sha,), 'Remote only')
    assert git.run('cat-file', '-e', missing.sha + '^{commit}', check=False).returncode != 0
    read, fetches = port._read, []
    def fetch_read(arguments):
        if arguments[:2] == ['git', 'fetch']:
            fetches.append(tuple(arguments))
        return read(arguments)
    monkeypatch.setattr(port, '_read', fetch_read)
    before = remote.run('show-ref').stdout
    facts = port.delivery(query(effect, (missing.sha,)))
    assert (effect.base.sha, missing.sha) in facts.ancestry
    assert len(fetches) == 1 and fetches[0][-1] == missing.sha
    assert '--no-write-fetch-head' in fetches[0] and '--refmap=' in fetches[0]
    assert remote.run('show-ref').stdout == before
    assert not (git.root / '.git/FETCH_HEAD').exists()


@pytest.mark.parametrize('failure', ['fetch-error', 'missing-after-fetch'])
def test_unverified_self_pair_cannot_be_accepted(tmp_path, monkeypatch, failure):
    port, _, _, _, _ = _fixture(tmp_path, monkeypatch)
    missing, fetches = 'f' * 40, []
    def fetch(sha):
        fetches.append(sha)
        if failure == 'fetch-error':
            raise GitHubReadError('fixture_fetch_failed', 'Original fetch failed')
    port.git_writes = SimpleNamespace(fetch=fetch)
    with pytest.raises(ReadUnavailable):
        port.delivery(DeliveryRead('refs/heads/absent-source', 'refs/heads/absent-target', None,
                                   (missing,), read_pr=False))
    assert fetches == [missing]


def test_merge_base_error_is_not_absence(tmp_path, monkeypatch):
    port, _, effect, _, _ = _fixture(tmp_path, monkeypatch)
    run = port.git.run
    def failed(*args, **kwargs):
        if args[0] == 'merge-base':
            return subprocess.CompletedProcess(args, 128, '', 'Synthetic merge-base error')
        return run(*args, **kwargs)
    monkeypatch.setattr(port.git, 'run', failed)
    with pytest.raises(KernelError, match='git_ancestry_unknown'):
        port.delivery(query(effect, (effect.after_sha,)))


def test_object_preparation_preserves_the_selected_writer_refusal(tmp_path, monkeypatch):
    port, _, _, _, _ = _fixture(tmp_path, monkeypatch)
    fetches = []
    refused = KernelError('operator_stopped')
    def fetch(sha):
        fetches.append(sha)
        raise refused
    port.git_writes = SimpleNamespace(fetch=fetch)
    with pytest.raises(KernelError) as caught:
        port.delivery(DeliveryRead('refs/heads/absent-source', 'refs/heads/absent-target', None,
                                   ('f' * 40,), read_pr=False))
    assert caught.value is refused and fetches == ['f' * 40]


def test_object_pruned_after_preparation_is_not_accepted(tmp_path, monkeypatch):
    port, git, effect, _, _ = _fixture(tmp_path, monkeypatch)
    orphan = git.make_commit(effect.candidate.tree, (effect.after_sha,), 'Unreferenced temporary object')
    ancestor, pruned = port.git.is_ancestor, []
    def prune_then_compare(a, b):
        if not pruned:
            git.run('prune', '--expire', 'now')
            pruned.append(True)
            assert git.run('cat-file', '-e', orphan.sha + '^{commit}', check=False).returncode != 0
        return ancestor(a, b)
    monkeypatch.setattr(port.git, 'is_ancestor', prune_then_compare)
    with pytest.raises(KernelError, match='git_ancestry_unknown'):
        port.delivery(query(effect, (orphan.sha,)))
    assert pruned == [True]


def test_invalid_sha_fails_before_preparation(tmp_path, monkeypatch):
    port, _, effect, _, _ = _fixture(tmp_path, monkeypatch)
    calls = counted(port, monkeypatch)
    with pytest.raises(ValueError, match='invalid commit identity'):
        port.delivery(query(effect, ('A' * 40,)))
    assert not calls
