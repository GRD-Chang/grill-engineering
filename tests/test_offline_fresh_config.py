"""Fresh private Git config validation; real Git, no Task/Host/SaveClock."""
import json
import os
import resource
import time

import pytest

from support.environment import durable_snapshot
from support.offline import OfflinePort
from support import offline_git


@pytest.fixture
def world(tmp_path):
    return OfflinePort.create(tmp_path / 'world')


@pytest.fixture(params=['git-parser', 'canonical-bytes'])
def parser(request, monkeypatch):
    if request.param == 'git-parser':
        monkeypatch.setattr(offline_git.OfflineGit, '_canonical_config', lambda *args, **kwargs: False)
    return request.param


def commands(monkeypatch):
    actual, calls = offline_git.OfflineGit._run, []
    def run(backend, directory, *args, **kwargs):
        calls.append(args)
        return actual(backend, directory, *args, **kwargs)
    monkeypatch.setattr(offline_git.OfflineGit, '_run', run)
    return calls


def test_canonical_files_skip_only_the_config_parser(world, parser, monkeypatch):
    backend = world.physical
    before = durable_snapshot(world.root)
    calls = commands(monkeypatch)
    assert backend.refs() == {'refs/heads/main': world.git.commit('HEAD').sha}
    assert sum(row[:1] == ('config',) for row in calls) == (2 if parser == 'git-parser' else 0)
    assert sum(row[:1] == ('rev-parse',) for row in calls) == 2
    assert sum(row[:1] == ('ls-remote',) for row in calls) == 1
    assert durable_snapshot(world.root) == before


@pytest.mark.parametrize('bare', [False, True])
@pytest.mark.parametrize('change', ['comment', 'large-comment', 'user', 'filemode', 'duplicate', 'continuation'])
def test_legal_noncanonical_files_still_use_git(world, parser, monkeypatch, bare, change):
    backend = world.physical
    directory = backend.remote if bare else backend.repository / '.git'
    path = directory / 'config'
    body = path.read_bytes()
    body = {
        'comment': b'# legal alternate spelling\n' + body,
        'large-comment': b'#' + b' ' * (1024 * 1024) + b'\n' + body,
        'user': body + b'[user]\n\tname = "Other Private User"\n\temail = other@example.invalid\n',
        'filemode': body.replace(b'filemode = true', b'filemode = false'),
        'duplicate': body + b'[core]\n\tbare = ' + str(bare).lower().encode() + b'\n',
        'continuation': body + b'[user]\n\tname = First\\\nSecond\n',
    }[change]
    path.write_bytes(body)
    before = durable_snapshot(world.root)
    calls = commands(monkeypatch)
    backend._owned()
    assert any(row[:4] == ('config', '--no-includes', '--file', str(path)) for row in calls)
    assert sum(row[:1] == ('rev-parse',) for row in calls) == 2
    assert durable_snapshot(world.root) == before


def test_sha256_repositories_keep_the_original_parser(world, parser, tmp_path, monkeypatch):
    root = tmp_path / 'sha256-world'
    root.mkdir()
    backend = world.physical
    backend._run(root, 'init', '--template=', '--object-format=sha256', str(root / 'authority'))
    backend._run(root, 'init', '--bare', '--template=', '--object-format=sha256', str(root / 'remote.git'))
    sha256 = offline_git.OfflineGit(root, environment=backend.environment)
    before = durable_snapshot(root)
    calls = commands(monkeypatch)
    sha256._owned()
    assert sum(row[:1] == ('config',) for row in calls) == 2
    assert sum(row[:1] == ('rev-parse',) for row in calls) == 2
    assert durable_snapshot(root) == before


@pytest.mark.parametrize('bare', [False, True])
@pytest.mark.parametrize('suffix', [
    b'[include]\n\tpath = never-read\n',
    b'[includeIf "gitdir:*"]\n\tpath = never-read\n',
    b'[url "file:///never-read"]\n\tinsteadOf = private\n',
    b'[core]\n\thooksPath = never-run\n',
    b'[core]\n\tbare = INVALID\n\tbare = false\n',
    b'[user]\n\tname = "unterminated\n',
])
def test_changed_configuration_refuses_before_any_write(world, parser, monkeypatch, bare, suffix):
    backend = world.physical
    head = world.git.commit('HEAD').sha
    directory = backend.remote if bare else backend.repository / '.git'
    with (directory / 'config').open('ab') as stream:
        stream.write(suffix)
    before = durable_snapshot(world.root)
    callbacks = []
    writer = backend._with_written(lambda *args: callbacks.append(args))
    calls = commands(monkeypatch)
    with pytest.raises((ValueError, OSError)):
        writer.push('refs/heads/unwritten', None, head)
    assert not any(row[:1] == ('push',) for row in calls)
    assert callbacks == [] and durable_snapshot(world.root) == before


@pytest.mark.parametrize('fault', ['head-missing', 'head-invalid', 'refs-missing', 'objects-missing',
    'commondir', 'worktree-config', 'config-symlink', 'refs-symlink', 'objects-symlink',
    'alternates', 'gitfile', 'gitdir-symlink', 'parent-symlink'])
def test_layout_and_object_checks_remain_fresh(world, parser, tmp_path, monkeypatch, fault):
    backend = world.physical
    directory = backend.repository / '.git'
    if fault == 'head-missing':
        (directory / 'HEAD').unlink()
    elif fault == 'head-invalid':
        (directory / 'HEAD').write_text('not a Git HEAD\n')
    elif fault in {'refs-missing', 'objects-missing'}:
        (directory / fault.split('-')[0]).rename(directory / ('saved-' + fault))
    elif fault in {'commondir', 'worktree-config'}:
        (directory / ('commondir' if fault == 'commondir' else 'config.worktree')).write_text('unread\n')
    elif fault.endswith('-symlink') and fault != 'parent-symlink':
        name = {'config-symlink': 'config', 'refs-symlink': 'refs',
                'objects-symlink': 'objects', 'gitdir-symlink': '.'}[fault]
        path = directory if name == '.' else directory / name
        destination = tmp_path / ('moved-' + fault)
        path.rename(destination)
        path.symlink_to(destination, target_is_directory=destination.is_dir())
    elif fault == 'alternates':
        (directory / 'objects/info/alternates').write_text(str(tmp_path / 'unread-objects') + '\n')
    elif fault == 'gitfile':
        directory.rename(tmp_path / 'saved-git')
        directory.write_text('gitdir: ' + str(tmp_path / 'saved-git') + '\n')
    else:
        destination = tmp_path / 'moved-world'
        world.root.rename(destination)
        world.root.symlink_to(destination, target_is_directory=True)
    callbacks = []
    calls = commands(monkeypatch)
    with pytest.raises((ValueError, OSError)):
        backend._with_written(lambda *args: callbacks.append(args)).push('refs/heads/unwritten', None, 'a' * 40)
    assert not any(row[:1] == ('push',) for row in calls) and callbacks == []


@pytest.mark.parametrize('redirect', [
    {'GIT_DIR': 'unread'}, {'GIT_COMMON_DIR': 'unread'}, {'GIT_OBJECT_DIRECTORY': 'unread'},
    {'GIT_NAMESPACE': 'other'}, {'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'user.name', 'GIT_CONFIG_VALUE_0': 'other'},
    {'GIT_CONFIG_PARAMETERS': "'user.name=other'"}, {'GIT_CONFIG_GLOBAL': 'unread'},
])
def test_mutable_redirect_environment_uses_original_parser(world, parser, monkeypatch, redirect):
    backend = world.physical
    backend.environment.update(redirect)
    calls = commands(monkeypatch)
    try:
        backend._owned()
    except (ValueError, OSError):
        pass  # Original Git decides whether this environment is acceptable.
    assert calls[0][:3] == ('config', '--no-includes', '--file')


@pytest.mark.parametrize('fault', ['replace', 'modify', 'read-error', 'close-error'])
def test_config_read_identity_and_io_failures_do_not_fallback(world, monkeypatch, fault):
    backend = world.physical
    path = backend.repository / '.git/config'
    read, close = os.read, os.close
    closed_descriptors = []
    error = OSError('controlled config read/close failure')
    def changed(descriptor, size):
        if fault == 'read-error':
            raise error
        body = read(descriptor, size)
        if fault == 'replace':
            path.rename(path.with_name('old-config'))
            path.write_bytes(body)
        elif fault == 'modify':
            path.write_bytes(body + b'# changed\n')
        return body
    def closed(descriptor):
        closed_descriptors.append(descriptor)
        close(descriptor)
        if fault == 'close-error':
            raise error
    monkeypatch.setattr(offline_git.os, 'read', changed)
    monkeypatch.setattr(offline_git.os, 'close', closed)
    calls = commands(monkeypatch)
    with pytest.raises((ValueError, OSError)) as caught:
        backend._owned()
    if fault.endswith('error'):
        assert caught.value is error
    assert calls == []
    assert len(closed_descriptors) == 1


@pytest.mark.parametrize('primary_type', [OSError, KeyboardInterrupt])
@pytest.mark.parametrize('note_failure', [None, OSError, KeyboardInterrupt])
def test_read_or_cancel_wins_over_close_and_note_failure(world, monkeypatch, primary_type, note_failure):
    backend = world.physical
    note_error = None if note_failure is None else note_failure('controlled note failure')
    class ReadFailure(primary_type):
        def add_note(self, note):
            if note_error is not None:
                raise note_error
            super().add_note(note)
    primary = ReadFailure('controlled original read/cancel failure')
    cleanup = OSError('controlled simultaneous descriptor close failure')
    original_tracebacks, closed_descriptors = [], []
    close = os.close
    def failed_read(descriptor, size):
        try:
            raise primary
        except BaseException:
            original_tracebacks.append(primary.__traceback__)
            raise
    def failed_close(descriptor):
        closed_descriptors.append(descriptor)
        close(descriptor)
        raise cleanup
    monkeypatch.setattr(offline_git.os, 'read', failed_read)
    monkeypatch.setattr(offline_git.os, 'close', failed_close)
    calls = commands(monkeypatch)
    with pytest.raises(primary_type) as caught:
        backend._owned()
    assert caught.value is primary
    traceback = primary.__traceback__
    while traceback is not None and traceback.tb_frame.f_code is not failed_read.__code__:
        traceback = traceback.tb_next
    assert traceback is original_tracebacks[0]
    assert len(closed_descriptors) == 1 and calls == []
    if note_failure is None:
        assert primary.__notes__ == ['Offline Git config descriptor close failed; original exception preserved']


def test_same_refs_workload_compares_git_parser_and_fresh_bytes(world, monkeypatch, record_property):
    backend = world.physical
    expected = backend.refs()
    before = durable_snapshot(world.root)
    original = offline_git.OfflineGit._canonical_config
    calls = commands(monkeypatch)
    batches = []
    for mode in ('git-parser', 'canonical-bytes', 'canonical-bytes', 'git-parser'):
        monkeypatch.setattr(offline_git.OfflineGit, '_canonical_config', original if mode == 'canonical-bytes' else lambda *args, **kwargs: False)
        calls.clear()
        children = resource.getrusage(resource.RUSAGE_CHILDREN)
        begin, cpu = time.perf_counter_ns(), time.process_time_ns()
        for _ in range(12):
            assert backend.refs() == expected
        end_children = resource.getrusage(resource.RUSAGE_CHILDREN)
        batches.append({'mode': mode, 'wall_ns': time.perf_counter_ns() - begin,
            'parent_cpu_ns': time.process_time_ns() - cpu,
            'waited_child_cpu_seconds': end_children.ru_utime + end_children.ru_stime - children.ru_utime - children.ru_stime,
            'commands': len(calls), 'config_commands': sum(row[:1] == ('config',) for row in calls)})
        assert len(calls) == (60 if mode == 'git-parser' else 36)
        assert sum(row[:1] == ('rev-parse',) for row in calls) == 24
        assert sum(row[:1] == ('ls-remote',) for row in calls) == 12
        assert durable_snapshot(world.root) == before
    record_property('fresh_config_cost', json.dumps(batches, sort_keys=True))
    print('fresh_config_cost=' + json.dumps(batches, sort_keys=True))
