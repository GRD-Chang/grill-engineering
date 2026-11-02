"""Private Git fixture configuration only; no Task, SaveClock, Host or Worker."""
from collections import Counter
from pathlib import Path
import json
import resource
import time

import pytest

from agent_run.adapters.remote import RemoteEffects
from agent_run.contracts.write_authority import WriteAuthority
from agent_run.errors import KernelError
from agent_run.model import Effect, Execution
from support.environment import durable_snapshot
from support.offline import LostResponse, OfflinePort
from support.offline_api import SyntheticGitHub
from support.offline_git import OfflineGit


def legacy_remote(port, written):
    """The reviewed baseline, used only for same-assertion comparisons."""
    api = SyntheticGitHub(port.git, port.database, 'example/project', physical=port.physical,
                          written=written, read_faults=port.read_faults)
    writer = OfflineGit(port.root, environment=port.git.env, written=api.record_git_write,
                        managed_workspace=port._managed_workspace)
    return RemoteEffects(port.git.root, 'example/project', runner_identity={'source': 'host'},
                         remote=port.remote_name, api=api, git_writes=writer)


@pytest.fixture(params=['legacy', 'derived'])
def build(request):
    return legacy_remote if request.param == 'legacy' else lambda port, written: port._remote('example/project', written)


@pytest.fixture
def world(tmp_path):
    port = OfflinePort.create(tmp_path / 'world')
    port.objects.bind('example/project', {1: 'open'})
    base = port.git.commit('HEAD')
    after = port.git.make_commit(base.tree, (base.sha,), 'new fixture commit')
    return port, base, after


def corrupt(port, tmp_path, location, fault):
    directory = port.git.root / '.git' if location == 'local' else port.root / 'remote.git'
    if fault == 'include':
        with (directory / 'config').open('a') as stream:
            stream.write('\n[include]\n\tpath = ignored-private-fixture-file\n')
    elif fault == 'rewrite':
        with (directory / 'config').open('a') as stream:
            stream.write('\n[url "file:///unreached-private-fixture"]\n\tinsteadOf = offline\n')
    elif fault == 'alternates':
        (directory / 'objects/info/alternates').write_text(str(tmp_path / 'foreign-objects') + '\n')
    else:
        (directory / 'objects/info/foreign').symlink_to(tmp_path / 'unreached-foreign-directory')


@pytest.mark.parametrize('location', ['local', 'bare'])
@pytest.mark.parametrize('fault', ['include', 'rewrite', 'alternates', 'symlink'])
def test_derived_writer_rechecks_tampered_world_before_push_or_delete(world, build, tmp_path, location, fault):
    port, base, after = world
    ref = 'refs/heads/original'
    assert port.physical.push(ref, None, base.sha).returncode == 0
    called = []
    adapter = build(port, lambda *args: called.append(args))
    corrupt(port, tmp_path, location, fault)
    before, objects = durable_snapshot(port.root), port.objects.snapshot()
    for operation in (lambda: adapter.git_writes.push('refs/heads/new', None, after.sha),
                      lambda: adapter.git_writes.delete(ref, base.sha)):
        with pytest.raises(ValueError, match='unsupported offline Git configuration|must not redirect|self-contained'):
            operation()
        assert durable_snapshot(port.root) == before
        assert port.objects.snapshot() == objects
        assert called == []


def test_initial_invalid_configuration_still_refuses_during_first_construction(world, build, tmp_path):
    port, _, _ = world
    corrupt(port, tmp_path, 'bare', 'include')
    before, objects = durable_snapshot(port.root), port.objects.snapshot()
    with pytest.raises(ValueError, match='unsupported offline Git configuration'):
        build(port, lambda *_: pytest.fail('No callback before a valid backend'))
    assert durable_snapshot(port.root) == before and port.objects.snapshot() == objects


def test_between_constructors_tampering_is_rejected_before_write_with_explicit_timing_change(
    world, build, tmp_path, monkeypatch, request,
):
    port, _, after = world
    initialize = SyntheticGitHub.__init__
    called = []

    def changed(api, *args, **kwargs):
        initialize(api, *args, **kwargs)
        corrupt(port, tmp_path, 'bare', 'include')

    monkeypatch.setattr(SyntheticGitHub, '__init__', changed)
    if request.node.callspec.params['build'] == 'legacy':
        with pytest.raises(ValueError, match='unsupported offline Git configuration'):
            build(port, lambda *args: called.append(args))
    else:
        # Construction no longer repeats ownership after the API's read-only
        # format check. The first real operation must freshly reject instead.
        adapter = build(port, lambda *args: called.append(args))
        before = durable_snapshot(port.root)
        with pytest.raises(ValueError, match='unsupported offline Git configuration'):
            adapter.git_writes.push('refs/heads/new', None, after.sha)
        assert durable_snapshot(port.root) == before
    assert called == [] and port.objects.snapshot()['api_requests'] == []


def test_interleaved_backends_keep_callbacks_and_environment_independent(world, build):
    port, base, after = world
    calls = [[], []]
    first = build(port, lambda *args: calls[0].append(args))
    second = build(port, lambda *args: calls[1].append(args))
    for adapter in (first, second):
        assert adapter.api.physical is not adapter.git_writes
        assert adapter.api.physical.written is None
        assert adapter.git_writes.written.__self__ is adapter.api
        assert adapter.git_writes.environment == adapter.api.physical.environment
        assert adapter.git_writes.environment is not adapter.api.physical.environment
    original = dict(first.api.physical.environment)
    first.git_writes.environment['LANG'] = 'C' if original.get('LANG') != 'C' else 'POSIX'
    assert first.git_writes.environment != original
    assert first.api.physical.environment == second.git_writes.environment == original
    assert first.git_writes.push('refs/heads/first', None, after.sha).returncode == 0
    assert second.git_writes.push('refs/heads/second', None, base.sha).returncode == 0
    assert first.git_writes.delete('refs/heads/first', after.sha).returncode == 0
    assert [item[0] for item in calls[0]] == [1, 3]
    assert [item[0] for item in calls[1]] == [2]
    assert all(item[2].returncode == 0 for batch in calls for item in batch)
    assert first.api.physical.refs() == second.api.physical.refs()
    assert len(port.objects.snapshot()['api_requests']) == 3


def test_lost_ack_keeps_the_real_git_record_and_readback_never_pushes_again(world, build, monkeypatch):
    port, _, after = world
    actual, pushes = OfflineGit._run, []

    def run(backend, directory, *args, **kwargs):
        if args[:1] == ('push',):
            pushes.append(args)
        return actual(backend, directory, *args, **kwargs)

    monkeypatch.setattr(OfflineGit, '_run', run)
    error = LostResponse('Real Git and GIT record committed; callback reply lost')
    called = []

    def lost(position, response, result):
        called.append(position)
        assert result.returncode == 0
        assert len(port.objects.snapshot()['api_requests']) == 1
        raise error

    adapter = build(port, lost)
    with pytest.raises(LostResponse) as caught:
        adapter.git_writes.push('refs/heads/published', None, after.sha)
    assert caught.value is error and called == [1]
    other_calls = []
    reopened = build(port, lambda *args: other_calls.append(args))
    assert reopened.git_writes.refs()['refs/heads/published'] == after.sha
    assert len(pushes) == 1 and other_calls == []
    records = port.objects.snapshot()['api_requests']
    assert len(records) == 1 and records[0]['method'] == 'GIT'
    assert json.loads(records[0]['response'])['returncode'] == 0


def test_authority_refusal_and_symbolic_ref_never_dispatch_a_push(world, build):
    port, base, after = world
    called = []
    adapter = build(port, lambda *args: called.append(args))
    effect = Effect('original', 'ticket:2', 'push', 'example/project', 'refs/heads/new', 'refs/heads/main',
                    base.sha, after.sha, after, base, None, 'offline-publisher',
                    {'expected_remote_sha': None}, Execution('controlled-unit', 'owner', 1))
    error = KernelError('operator_stopped')

    def refused():
        raise error

    before = durable_snapshot(port.root)
    with pytest.raises(KernelError) as caught:
        adapter.execute(effect, WriteAuthority(lambda: refused))
    assert caught.value is error
    assert durable_snapshot(port.root) == before and called == []
    physical = port.physical
    physical._run(physical.remote, 'symbolic-ref', 'refs/heads/alias', 'refs/heads/main')
    before = durable_snapshot(port.root)
    with pytest.raises(OSError, match='original direct ref'):
        adapter.git_writes.push('refs/heads/alias', base.sha, after.sha)
    assert durable_snapshot(port.root) == before and called == []


def test_failed_cas_records_failure_without_forging_success(world, build):
    port, base, after = world
    assert port.physical.push('refs/heads/existing', None, base.sha).returncode == 0
    called = []
    adapter = build(port, lambda *args: called.append(args))
    result = adapter.git_writes.push('refs/heads/existing', None, after.sha)
    assert result.returncode != 0 and len(called) == 1
    assert called[0][2] is result
    assert adapter.git_writes.refs()['refs/heads/existing'] == base.sha
    records = port.objects.snapshot()['api_requests']
    assert len(records) == 1 and json.loads(records[0]['response'])['returncode'] == result.returncode


def test_same_read_workload_keeps_facts_and_removes_only_duplicate_construction(world, monkeypatch, record_property):
    port, _, _ = world
    expected = port.physical.refs()
    original_owned, original_run = OfflineGit._owned, OfflineGit._run
    counts = Counter()

    def owned(backend):
        counts['owned'] += 1
        return original_owned(backend)

    def run(backend, directory, *args, **kwargs):
        counts['commands'] += 1
        return original_run(backend, directory, *args, **kwargs)

    monkeypatch.setattr(OfflineGit, '_owned', owned)
    monkeypatch.setattr(OfflineGit, '_run', run)
    batches = []
    before = durable_snapshot(port.root)
    for name in ('legacy', 'derived', 'derived', 'legacy'):
        counts.clear()
        begin, cpu = time.perf_counter_ns(), time.process_time_ns()
        children = resource.getrusage(resource.RUSAGE_CHILDREN)
        for _ in range(4):
            adapter = legacy_remote(port, lambda *_: pytest.fail('Read emitted a mutation')) if name == 'legacy' else port._remote('example/project', lambda *_: pytest.fail('Read emitted a mutation'))
            assert adapter.api.object('user') == {'login': 'offline-publisher'}
            assert adapter.api.physical.refs() == adapter.git_writes.refs() == expected
        end_children = resource.getrusage(resource.RUSAGE_CHILDREN)
        batches.append({'mode': name, 'wall_ns': time.perf_counter_ns() - begin,
                        'parent_cpu_ns': time.process_time_ns() - cpu,
                        'reaped_child_cpu_seconds': end_children.ru_utime + end_children.ru_stime - children.ru_utime - children.ru_stime,
                        **counts})
        assert counts == {'owned': 16 if name == 'legacy' else 12,
                          'commands': 40 if name == 'legacy' else 32}
        assert durable_snapshot(port.root) == before
    record_property('backend_configuration_cost', json.dumps(batches, sort_keys=True))
