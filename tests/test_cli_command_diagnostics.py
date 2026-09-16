from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from support.workspace import managed_state

from agent_run.cli import main
from agent_run.github import GitHubReadError
from agent_run.github_fixture import FixtureGitHubReader
from agent_run.state import StateStore
from conftest import seed_run, write_fixture


def test_resume_without_a_managed_workspace_explains_how_to_start(tmp_path: Path) -> None:
    environment = os.environ.copy()
    environment['PYTHONPATH'] = str(Path(__file__).parents[1] / 'src')
    result = subprocess.run(
        [sys.executable, '-m', 'agent_run.cli', 'resume', '213',
         '--repo', 'example/project', '--json'],
        cwd=tmp_path, env=environment, text=True, capture_output=True, check=False,
    )
    assert result.returncode == 2
    diagnostic = json.loads(result.stdout)['diagnostics'][0]
    assert diagnostic['code'] == 'run_selector_not_found'
    assert 'Runner 工作区' in diagnostic['message']
    assert 'run' in diagnostic['message']
    assert not (tmp_path / '.agent-run').exists()


@pytest.mark.parametrize('as_json', [False, True])
def test_command_read_failure_exposes_safe_cause(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], as_json: bool,
) -> None:
    monkeypatch.chdir(git_repo)

    def unavailable(self: FixtureGitHubReader) -> None:
        raise GitHubReadError('github_timeout', 'GitHub command timed out; token=private-value')

    fixture = write_fixture(git_repo / 'github.json', issues={})
    monkeypatch.setattr(FixtureGitHubReader, 'repository_hint', unavailable)
    assert main(['run', '213', '--github-fixture', str(fixture), *(['--json'] if as_json else [])]) == 2
    output = capsys.readouterr().out
    assert 'private-value' not in output
    assert 'timed out' in output
    assert '重试' in output
    if as_json:
        diagnostic = json.loads(output)['diagnostics'][0]
        assert diagnostic['code'] == 'github_timeout'
        assert diagnostic['operation'] == 'run'
        assert diagnostic['application_status'] == 'not_applied'


def test_new_command_error_does_not_replace_or_repeat_old_run_failure(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fixture = write_fixture(git_repo / 'github.json', issues={})
    assert seed_run(git_repo, fixture).returncode == 0
    states = StateStore(managed_state(git_repo))
    current = states.find_unfinished_runs('example/project', 1)[0]
    current['status'] = 'execution_failed'
    current['diagnostics'] = [{'code': 'old_failure', 'message': 'old usage limit'}]
    states.save_run(current['run_id'], current)
    state_path = states.runs_directory / f"{current['run_id']}.json"
    before = state_path.read_bytes()
    monkeypatch.chdir(git_repo)
    monkeypatch.setattr('agent_run.cli._running_active_runner', lambda: False)

    assert main(['resume', '1', '--repo', 'example/project', '--json']) == 2

    output = json.loads(capsys.readouterr().out)
    assert output['diagnostics'][0]['code'] == 'execution_readiness'
    assert 'old usage limit' not in json.dumps(output)
    assert state_path.read_bytes() == before


@pytest.mark.parametrize('as_json', [False, True])
@pytest.mark.parametrize('summary_size', [10, 9000])
def test_long_multiline_error_preserves_next_step_without_response_body(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], as_json: bool, summary_size: int,
) -> None:
    monkeypatch.chdir(git_repo)
    fixture = write_fixture(git_repo / 'github.json', issues={})

    def unavailable(self: FixtureGitHubReader) -> None:
        raise GitHubReadError(
            'github_timeout', 'timed out ' + 'x' * summary_size + '\nraw response body',
        )

    monkeypatch.setattr(FixtureGitHubReader, 'repository_hint', unavailable)
    assert main(['run', '213', '--github-fixture', str(fixture),
                 *(['--json'] if as_json else [])]) == 2
    output = capsys.readouterr().out
    assert 'raw response body' not in output
    assert '重试' in output
    if as_json:
        diagnostic = json.loads(output)['diagnostics'][0]
        assert len(diagnostic['message'].encode('utf-8')) <= 8192
        assert '重试' in diagnostic['next_action']
    else:
        assert len(output) < 1000
