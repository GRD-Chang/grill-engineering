from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from agent_run import cli
from agent_run.codex import CodexCliBackend
from agent_run.executor_host import FakeExecutorHost
from conftest import write_fixture
from test_cli import load_only_run_state


@pytest.fixture
def recovery_cli(
    git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> Any:
    fixture = write_fixture(git_repo / 'github.json', issues={})
    outcomes: list[str] = []
    waits: list[float] = []
    calls: list[dict[str, Any]] = []

    def worker(arguments: list[str], *, on_stdout_line: Any = None,
               **options: Any) -> subprocess.CompletedProcess[str]:
        calls.append({'arguments': arguments, **options})
        line = json.dumps({'type': 'thread.started', 'thread_id': 'original-thread'})
        if on_stdout_line:
            on_stdout_line(line)
        outcome = outcomes.pop(0)
        if outcome not in {'invalid', 'blocker'}:
            return subprocess.CompletedProcess(arguments, 1, line + '\n' + json.dumps({
                'type': 'turn.failed', 'error': {'message': ('unexpected worker exit' if outcome == 'failure' else outcome)},
            }), '')
        artifact = ({'invalid': True} if outcome == 'invalid' else {
            'result_kind': 'human_blocker', 'summary': None,
            'human_blockers': ['需求缺少必要输入；已检查 Issue；请补充输入。'],
        })
        Path(arguments[arguments.index('--output-last-message') + 1]).write_text(json.dumps(artifact))
        return subprocess.CompletedProcess(arguments, 0, line, '')

    monkeypatch.chdir(git_repo)
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path / 'state'))
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'config'))
    monkeypatch.setattr('agent_run.codex.run_worker_process', worker)
    monkeypatch.setattr('agent_run.codex.time.sleep', waits.append)
    monkeypatch.setattr(cli, 'FakeExecutorHost', lambda **kwargs: FakeExecutorHost())
    monkeypatch.setattr(cli, 'CodexCliBackend', lambda **kwargs: CodexCliBackend(
        credential_provider=lambda: 'fixture-reader', **kwargs,
    ))

    def command(name: str) -> tuple[int, dict[str, Any]]:
        result = cli.main([name, '1', '--github-fixture', str(fixture), '--json'])
        capsys.readouterr()
        return result, load_only_run_state(git_repo)

    return command, outcomes, calls, waits


def test_manual_resume_preserves_spent_recovery_and_interrupted_json_step(recovery_cli: Any) -> None:
    command, outcomes, calls, _waits = recovery_cli
    outcomes.extend(['invalid', 'invalid', 'failure', 'failure', 'failure', 'blocker'])
    code, failed = command('run')
    assert code == 2
    assert len(calls) == 4
    first = failed['active_agent_invocation']
    assert first['output_attempt'] == 3
    assert first['ordinary_recovery_used'] is True
    assert first['execution_interrupted'] is True
    budget = failed['parent_job']['review_budget']
    attempt_id = first['semantic_attempt']['attempt_id']

    code, failed_again = command('resume')
    assert code == 2
    assert len(calls) == 5  # The automatic allowance remains spent.
    assert failed_again['parent_job']['review_budget'] == budget
    assert failed_again['active_agent_invocation']['semantic_attempt']['attempt_id'] == attempt_id
    assert failed_again['active_agent_invocation']['ordinary_recovery_used'] is True
    assert calls[2]['prompt'] == calls[3]['prompt'] == calls[4]['prompt']
    assert '不要修改文件' in calls[4]['prompt']
    assert all('resume' in call['arguments'] for call in calls[1:])

    code, blocked = command('resume')
    assert code == 2
    assert len(calls) == 6
    assert blocked['status'] == 'ready_for_human'
    assert blocked['diagnostics'][0]['code'] == 'agent_requires_human'
    assert blocked['active_agent_invocation']['status'] == 'completed'
    assert blocked['parent_job']['review_budget'] == budget


def test_capacity_continuation_keeps_bounded_history_and_one_business_attempt(recovery_cli: Any) -> None:
    command, outcomes, calls, waits = recovery_cli
    capacity = "Selected model is at capacity. Please try a different model."
    outcomes.extend([capacity] * 6 + ['blocker'])
    code, state = command('run')
    assert code == 2
    assert len(calls) == 7
    assert sum(waits) == 180
    assert len(state['agent_invocation_history']) == 1
    active = state['active_agent_invocation']
    assert active['capacity_recovery_count'] == 6
    assert active['ordinary_recovery_used'] is False
    assert active['status'] == 'completed'
    assert state['parent_job']['review_budget']['development_attempts'] == 1
    assert state['parent_job']['review_budget']['window'] == 1
    assert len(json.dumps(active)) < 12000
    assert all('resume' in call['arguments'] for call in calls[1:])
