"""Real fixture CLI reads retain their receipts without service rewrites."""
import json
import ast
import subprocess
import sys

import pytest

from agent_run.adapters.git_objects import LocalGit
from agent_run.adapters.github import GhGitHubReader
from support.systemd_manager import _CODEX, _control_environment


def test_managed_fake_gh_uses_only_explicit_stdlib_support(git_repo, tmp_path, monkeypatch):
    environment, state = _control_environment(git_repo, tmp_path, monkeypatch, codex=_CODEX, tickets=(2, 3))
    binary = tmp_path / 'control-bin'
    assert (binary / 'gh').read_text().splitlines()[0] == '#!' + sys.executable + ' -S'
    assert (binary / 'codex').read_text().splitlines()[0] == '#!' + sys.executable
    # The local-failure story's existing request-persistence extension retains
    # its original anchor and must still compile in the generated CLI.
    ast.parse((binary / 'gh').read_text().replace('write_text(state,json.dumps(d))',
        "if method!='GET':write_text(state,json.dumps(d))"))
    reader = GhGitHubReader('example/project', working_directory=git_repo, runner_identity={'source': 'host'})
    original = json.loads(state.read_text())
    requirements = reader.requirements(1)
    assert requirements.parent.number == 1
    assert [issue.number for issue in requirements.issues] == [2, 3]
    assert json.loads(state.read_text()) == original


def test_pure_fake_reads_leave_state_bytes_and_mtime_unchanged(git_repo, tmp_path, monkeypatch):
    environment, state = _control_environment(git_repo, tmp_path, monkeypatch, codex=_CODEX, tickets=(2,))
    # Formatting is deliberately noncanonical; a read must not rewrite it.
    state.write_text(json.dumps(json.loads(state.read_text()), indent=2) + '\n')
    original, stamp = state.read_bytes(), state.stat().st_mtime_ns
    git = LocalGit(git_repo)
    commit = git.commit('HEAD')
    reader = GhGitHubReader('example/project', working_directory=git_repo, runner_identity={'source': 'host'})
    assert reader.requirements(1).issues[0].number == 2
    for endpoint in ['user', 'repos/example/project/git/ref/heads/main',
                     'repos/example/project/git/commits/' + commit.sha]:
        result = subprocess.run([str(tmp_path / 'control-bin/gh'), 'api', endpoint],
            cwd=git_repo, env=environment, text=True, capture_output=True, timeout=5)
        assert result.returncode == 0, result.stderr
        assert isinstance(json.loads(result.stdout), dict)
    assert state.read_bytes() == original and state.stat().st_mtime_ns == stamp


def test_fake_commit_receipt_reads_one_exact_object(git_repo, tmp_path, monkeypatch):
    environment, state = _control_environment(git_repo, tmp_path, monkeypatch, codex=_CODEX, tickets=(2,))
    git = LocalGit(git_repo)
    base = git.commit('HEAD')
    other = git.make_commit(base.tree, (), 'Independent root')
    raw = (f'tree {base.tree}\nparent {base.sha}\nparent {other.sha}\n'
        'author Publisher <publisher@example.invalid> 946684800 +0000\n'
        'committer Publisher <publisher@example.invalid> 946684800 +0000\n'
        'gpgsig synthetic-header\n parent not-a-parent-header\n\nOriginal\r\n中文\n\n')
    sha = git.run('hash-object', '-w', '-t', 'commit', '--stdin', input_text=raw).stdout.strip()
    git.run('push', str(tmp_path / 'origin.git'), sha + ':refs/heads/receipt')
    executable = tmp_path / 'control-bin/gh'
    calls = tmp_path / 'internal-git-calls'
    # Observe only this test-owned CLI's actual child creations; stdout stays
    # the API response and no product process or environment value is logged.
    prefix = ("import sys\n"
        "def count_git(event, values):\n"
        "    if event == 'subprocess.Popen' and values[1][0] == 'git':\n"
        f"        with open({str(calls)!r}, 'a') as stream: stream.write('git\\n')\n"
        "sys.addaudithook(count_git)\n")
    program = executable.read_text().split('\n', 1)
    executable.write_text(program[0] + '\n' + prefix + program[1])
    response = subprocess.run([str(executable), 'api', 'repos/example/project/git/commits/' + sha],
        cwd=git_repo, env=environment, text=True, capture_output=True, timeout=5)
    assert response.returncode == 0, response.stderr
    assert json.loads(response.stdout) == {'sha': sha, 'tree': {'sha': base.tree},
        'parents': [{'sha': base.sha}, {'sha': other.sha}], 'message': raw.partition('\n\n')[2]}
    assert calls.read_text().splitlines() == ['git']


@pytest.mark.parametrize('opaque', [
    'gpgsig opaque\u2028parent {extra}\n',
    'gpgsig opaque\u0085parent {extra}\n',
    'gpgsig opaque\rparent {extra}\n',
    'gpgsig opaque\vparent {extra}\n',
    'extra terminal\nparent {extra}\n',
], ids=['line-separator', 'next-line', 'carriage-return', 'vertical-tab', 'parent-after-header'])
def test_fake_commit_parents_match_git_header_boundaries(git_repo, tmp_path, monkeypatch, opaque):
    environment, state = _control_environment(git_repo, tmp_path, monkeypatch, codex=_CODEX, tickets=(2,))
    git = LocalGit(git_repo)
    base = git.commit('HEAD')
    extra = git.make_commit(base.tree, (), 'Opaque header identity')
    raw = (f'tree {base.tree}\nparent {base.sha}\n'
        'author Publisher <publisher@example.invalid> 946684800 +0000\n'
        'committer Publisher <publisher@example.invalid> 946684800 +0000\n'
        + opaque.format(extra=extra.sha) + '\nOriginal body\r\n中文\n')
    sha = git.run('hash-object', '-w', '-t', 'commit', '--stdin', input_text=raw).stdout.strip()
    # The old fixture's Git formatter is the oracle for this same real object;
    # opaque header content must not add an API parent or change its tree.
    tree, parents = git.run('show', '-s', '--format=%T%n%P', sha).stdout.rstrip('\n').split('\n')
    assert tree == base.tree and parents == base.sha
    git.run('push', str(tmp_path / 'origin.git'), sha + ':refs/heads/opaque')
    response = subprocess.run([str(tmp_path / 'control-bin/gh'), 'api', 'repos/example/project/git/commits/' + sha],
        cwd=git_repo, env=environment, text=True, capture_output=True, timeout=5)
    assert response.returncode == 0, response.stderr
    assert json.loads(response.stdout) == {'sha': sha, 'tree': {'sha': tree},
        'parents': [{'sha': value} for value in parents.split()], 'message': raw.partition('\n\n')[2]}


def test_fake_pr_get_still_persists_fresh_live_heads(git_repo, tmp_path, monkeypatch):
    environment, state = _control_environment(git_repo, tmp_path, monkeypatch, codex=_CODEX, tickets=(2,))
    git = LocalGit(git_repo)
    base = git.commit('HEAD')
    remote = str(tmp_path / 'origin.git')
    git.run('push', remote, base.sha + ':refs/heads/candidate')
    payload = tmp_path / 'pr.json'
    payload.write_text(json.dumps({'head': 'candidate', 'base': 'main', 'title': 'Original', 'body': 'Original'}))
    executable = str(tmp_path / 'control-bin/gh')
    created = subprocess.run([executable, 'api', 'repos/example/project/pulls', '--method', 'POST', '--input', str(payload)],
        cwd=git_repo, env=environment, text=True, capture_output=True, timeout=5)
    assert created.returncode == 0, created.stderr
    assert json.loads(created.stdout)['head']['sha'] == base.sha
    later = git.make_commit(base.tree, (base.sha,), 'Later live head')
    git.run('push', remote, later.sha + ':refs/heads/candidate')
    response = subprocess.run([executable, 'api', 'repos/example/project/pulls/17'],
        cwd=git_repo, env=environment, text=True, capture_output=True, timeout=5)
    assert response.returncode == 0, response.stderr
    assert json.loads(response.stdout)['head']['sha'] == later.sha
    stored, = json.loads(state.read_text())['prs']
    assert stored['head']['sha'] == later.sha and stored['base']['sha'] == base.sha
