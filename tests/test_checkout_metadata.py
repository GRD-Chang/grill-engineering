"""Read-only checkout metadata batching with real private Git resources."""
from dataclasses import replace
from pathlib import Path
from statistics import median
from time import perf_counter_ns, thread_time_ns

import pytest

from agent_run.adapters.git_objects import LocalGit
from agent_run.errors import KernelError
from support.remote_effects import publication


def test_checkout_batches_current_metadata_without_changing_facts(git_repo, tmp_path, monkeypatch):
    git, local, effect = publication(git_repo, tmp_path)
    checkout = Path(effect.payload['checkout'])
    common = git.run('rev-parse', '--path-format=absolute', '--git-common-dir', cwd=checkout).stdout.strip()
    top = git.run('rev-parse', '--show-toplevel', cwd=checkout).stdout.strip()
    head = git.run('rev-parse', '--verify', 'HEAD', cwd=checkout).stdout.strip()
    run, commands, observed = LocalGit.run, [], []

    def trace(self, *arguments, **options):
        result = run(self, *arguments, **options)
        commands.append((arguments, options.get('cwd')))
        if '--show-toplevel' in arguments:
            observed.append(result.stdout)
        return result

    monkeypatch.setattr(LocalGit, 'run', trace)
    assert local._checkout(effect) == (checkout, head)
    assert len(commands) == 4  # Root identity, registry, checkout metadata, detached HEAD.
    assert observed == [common + '\n' + top + '\n' + head + '\n']
    assert commands[-1][0] == ('symbolic-ref', '-q', 'HEAD')


@pytest.mark.parametrize('suffix', [' space', '\tspace', '\nline', '\rline'])
def test_checkout_preserves_unusual_path_names(git_repo, tmp_path, suffix):
    git, local, effect = publication(git_repo, tmp_path)
    checkout = Path(effect.payload['checkout'])
    moved = checkout.with_name(checkout.name + suffix)
    git.run('worktree', 'move', str(checkout), str(moved))
    changed = replace(effect, payload={**effect.payload, 'checkout': str(moved)})
    assert local._checkout(changed) == (moved, effect.before_sha)


@pytest.mark.parametrize('change', ['head', 'symbolic', 'registration', 'gitfile', 'common'])
def test_checkout_rejects_change_after_registry_read(git_repo, tmp_path, monkeypatch, change):
    git, local, effect = publication(git_repo, tmp_path)
    checkout = Path(effect.payload['checkout'])
    other = checkout.with_name('relocated')
    foreign = tmp_path / 'foreign-repository'
    if change == 'common':
        git.run('clone', '--no-hardlinks', str(git_repo), str(foreign))
    later = git.make_commit(git.commit('HEAD').tree, (effect.before_sha,), 'Concurrent checkout change')
    run, changed = LocalGit.run, []

    def race(self, *arguments, **options):
        result = run(self, *arguments, **options)
        if arguments == ('worktree', 'list', '--porcelain', '-z') and not changed:
            changed.append(change)
            if change == 'head':
                run(git, 'reset', '--hard', later.sha, cwd=checkout)
            elif change == 'symbolic':
                run(git, 'switch', '-c', 'foreign-branch', cwd=checkout)
            elif change == 'registration':
                run(git, 'worktree', 'move', str(checkout), str(other))
            elif change == 'gitfile':
                (checkout / '.git').write_text('gitdir: ' + str(git_repo / '.git') + '\n')
            else:
                (checkout / '.git').write_text('gitdir: ' + str(foreign / '.git') + '\n')
        return result

    monkeypatch.setattr(LocalGit, 'run', race)
    with pytest.raises(KernelError) as refused:
        local._checkout(effect)
    assert refused.value.code == ('local_checkout_unavailable' if change == 'registration' else 'foreign_local_checkout')
    assert changed == [change]


def test_checkout_still_checks_symbolic_head_after_metadata(git_repo, tmp_path, monkeypatch):
    git, local, effect = publication(git_repo, tmp_path)
    checkout = Path(effect.payload['checkout'])
    run, changed = LocalGit.run, []

    def race(self, *arguments, **options):
        result = run(self, *arguments, **options)
        if arguments[0] == 'rev-parse' and '--verify' in arguments and options.get('cwd') == checkout and not changed:
            changed.append(True)
            run(git, 'switch', '-c', 'late-symbolic-branch', cwd=checkout)
        return result

    monkeypatch.setattr(LocalGit, 'run', race)
    with pytest.raises(KernelError, match='foreign_local_checkout'):
        local._checkout(effect)
    assert changed == [True]


def test_checkout_rereads_head_on_each_call_and_preserves_effect_admission(git_repo, tmp_path):
    git, local, effect = publication(git_repo, tmp_path)
    checkout = Path(effect.payload['checkout'])
    assert local._checkout(effect) == (checkout, effect.before_sha)
    later = git.make_commit(git.commit('HEAD').tree, (effect.before_sha,), 'Later owner work')
    git.run('reset', '--hard', later.sha, cwd=checkout)
    assert local._checkout(effect) == (checkout, later.sha)
    assert local.reconcile(effect).disposition == 'third'


@pytest.mark.parametrize('failure', ['command', 'shape'])
def test_checkout_metadata_failure_does_not_supply_a_head(git_repo, tmp_path, monkeypatch, failure):
    git, local, effect = publication(git_repo, tmp_path)
    run = LocalGit.run
    original = KernelError('git_operation_failed', 'Original metadata read failure')

    def unavailable(self, *arguments, **options):
        if '--show-toplevel' in arguments:
            if failure == 'command':
                raise original
            return run(self, 'rev-parse', '--verify', 'HEAD', **options)
        return run(self, *arguments, **options)

    monkeypatch.setattr(LocalGit, 'run', unavailable)
    with pytest.raises(KernelError) as refused:
        local._checkout(effect)
    if failure == 'command':
        assert refused.value is original
    else:
        assert refused.value.code == 'foreign_local_checkout'


def test_checkout_metadata_batch_cost_same_checkout(git_repo, tmp_path):
    git, _, effect = publication(git_repo, tmp_path)
    checkout = Path(effect.payload['checkout'])
    samples = {'separate': [], 'batch': []}
    for pair in range(7):
        values = {}
        for kind in (('separate', 'batch') if pair % 2 == 0 else ('batch', 'separate')):
            cpu, wall = thread_time_ns(), perf_counter_ns()
            if kind == 'separate':
                values[kind] = tuple(git.run('rev-parse', *args, cwd=checkout).stdout.strip() for args in (
                    ('--path-format=absolute', '--git-common-dir'), ('--show-toplevel',), ('--verify', 'HEAD')))
            else:
                values[kind] = tuple(git.run('rev-parse', '--path-format=absolute', '--git-common-dir',
                    '--show-toplevel', '--verify', 'HEAD', cwd=checkout).stdout.rstrip('\n').split('\n'))
            samples[kind].append((thread_time_ns() - cpu, perf_counter_ns() - wall))
        assert values['separate'] == values['batch']
    print('checkout metadata seven alternating pairs', samples,
          'median CPU/wall ns', {kind: tuple(median(row[index] for row in rows) for index in (0, 1))
                                 for kind, rows in samples.items()})
