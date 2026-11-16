"""Finite object observations retain real Git's commit peeling contract."""
import subprocess

import pytest

from agent_run.core.operation_contract import DeliveryRead, ReadUnavailable
from agent_run.adapters.git_objects import LocalGit
from agent_run.errors import GitError, KernelError
from support.remote_effects import _fixture


def test_commit_availability_reads_full_sha256_objects(tmp_path):
    git = LocalGit(tmp_path)
    git.run('init', '--object-format=sha256', '--initial-branch=main')
    tree = git.run('mktree', input_text='').stdout.strip()
    head = git.run('-c', 'user.name=Object fixture', '-c', 'user.email=objects@example.invalid',
                   'commit-tree', tree, input_text='SHA-256 observation\n').stdout.strip()
    assert len(head) == len(tree) == 64
    assert git.missing_commits((head, 'f' * 64, head)) == ('f' * 64,)


def test_commit_availability_peels_tags_without_requiring_tree(tmp_path, monkeypatch):
    _, git, effect, _, _ = _fixture(tmp_path, monkeypatch)
    git.run('tag', '-a', 'anchor', effect.base.sha, '-m', 'Anchor')
    tag = git.run('rev-parse', 'anchor').stdout.strip()
    absent_tree = 'e' * 40
    raw = f'tree {absent_tree}\nauthor A <a@example.invalid> 1 +0000\ncommitter A <a@example.invalid> 1 +0000\n\nIncomplete\n'
    incomplete = git.run('hash-object', '-t', 'commit', '-w', '--stdin', input_text=raw).stdout.strip()
    assert git.missing_commits((tag, incomplete, 'f' * 40, tag)) == ('f' * 40,)
    assert git.missing_commits((effect.base.sha,)) == ()
    blob = git.run('hash-object', '-w', '--stdin', input_text='blob').stdout.strip()
    assert git.missing_commits((effect.base.tree, blob)) == (effect.base.tree, blob)
    monkeypatch.setattr(git, 'run', lambda *a, **k: pytest.fail('Empty/invalid input spawned Git'))
    assert git.missing_commits(()) == ()
    for value in ('main', 'a' * 39, 'A' * 40, 'a' * 40 + '\n'):
        with pytest.raises((ValueError, KernelError)):
            git.missing_commits((value,))


@pytest.mark.parametrize('fault', ['extra', 'reversed', 'truncated', 'type', 'exit'])
def test_batch_protocol_errors_are_not_missing_objects(tmp_path, monkeypatch, fault):
    _, git, effect, _, _ = _fixture(tmp_path, monkeypatch)
    heads = (effect.base.sha, effect.after_sha)
    run = git.run
    def corrupt(*args, **kwargs):
        result = run(*args, **kwargs)
        if args[0] != 'cat-file':
            return result
        rows = result.stdout.splitlines()
        output = {'extra': rows + rows[:1], 'reversed': list(reversed(rows)),
                  'truncated': rows[:1], 'type': [rows[0].replace(' commit ', ' tree '), *rows[1:]],
                  'exit': rows}[fault]
        return subprocess.CompletedProcess(result.args, 1 if fault == 'exit' else 0, '\n'.join(output) + '\n', '')
    monkeypatch.setattr(git, 'run', corrupt)
    with pytest.raises(KernelError):
        git.missing_commits(heads)


@pytest.mark.parametrize('failure', ['fatal', 'protocol', 'fetch-success'])
def test_remote_unavailable_objects_cannot_become_false_or_trigger_spurious_fetch(tmp_path, monkeypatch, failure):
    port, git, effect, _, _ = _fixture(tmp_path, monkeypatch)
    calls, run = [], port.git.run
    def observe(*args, **kwargs):
        calls.append(args)
        if args[0] == 'cat-file' and failure == 'fatal':
            raise KernelError('git_operation_failed') from GitError('Native failure')
        result = run(*args, **kwargs)
        if args[0] == 'cat-file' and failure == 'protocol':
            return subprocess.CompletedProcess(result.args, 0, '', '')
        return result
    monkeypatch.setattr(port.git, 'run', observe)
    read = port._read
    def transport(arguments, *args, **kwargs):
        if arguments[:2] == ['git', 'fetch']:
            calls.append(('fetch',))
            return ''
        return read(arguments, *args, **kwargs)
    monkeypatch.setattr(port, '_read', transport)
    anchor = 'f' * 40 if failure == 'fetch-success' else effect.after_sha
    query = DeliveryRead(effect.source_ref, effect.target_ref, None, (anchor,),
                         read_pr=False, ancestry_pairs=((anchor, 'source'),))
    with pytest.raises((KernelError, ReadUnavailable)):
        port.delivery(query)
    assert sum(call[0] == 'fetch' for call in calls) == (failure == 'fetch-success')
    assert not any(call[0] == 'merge-base' for call in calls)


def test_native_child_failure_keeps_kernel_classification(tmp_path, monkeypatch):
    _, git, effect, _, _ = _fixture(tmp_path, monkeypatch)
    from agent_run.adapters import git_objects
    def interrupted(*args, **kwargs):
        raise GitError('Controlled native child ownership failure')
    monkeypatch.setattr(git_objects, 'run_git', interrupted)
    with pytest.raises(KernelError, match='git_operation_failed') as caught:
        git.missing_commits((effect.base.sha,))
    assert isinstance(caught.value.__cause__, GitError)


def test_remote_fetches_only_missing_objects_without_changing_refs(tmp_path, monkeypatch):
    port, git, effect, _, _ = _fixture(tmp_path, monkeypatch)
    remote = tmp_path / 'origin.git'
    remote_only = git.run('-c', 'user.name=Remote author', '-c', 'user.email=remote@example.invalid',
                          'commit-tree', effect.base.tree, '-p', effect.base.sha,
                          cwd=remote, input_text='Remote-only\n').stdout.strip()
    refs = git.run('show-ref').stdout
    fetch_head = git.root / '.git' / 'FETCH_HEAD'
    fetch_head.write_text('existing fetch boundary\n')
    assert git.missing_commits((remote_only,)) == (remote_only,)
    calls, run = [], port.git.run
    def observe(*args, **kwargs):
        calls.append(args)
        return run(*args, **kwargs)
    monkeypatch.setattr(port.git, 'run', observe)
    assert port._ancestor(effect.base.sha, remote_only)
    assert sum(call[0] == 'cat-file' for call in calls) == 2
    assert git.run('show-ref').stdout == refs
    assert fetch_head.read_text() == 'existing fetch boundary\n'
    calls.clear()
    assert port._ancestor(remote_only, remote_only)
    assert calls == []
