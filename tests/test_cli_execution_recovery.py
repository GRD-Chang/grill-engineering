from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
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
    outcomes: list[Any] = []
    waits: list[float] = []
    calls: list[dict[str, Any]] = []
    current_thread = "original-thread"

    def worker(arguments: list[str], *, on_stdout_line: Any = None,
               **options: Any) -> subprocess.CompletedProcess[str]:
        nonlocal current_thread
        if calls and 'resume' not in arguments:
            current_thread = 'replacement-thread'
        calls.append({'arguments': arguments, **options})
        line = json.dumps({'type': 'thread.started', 'thread_id': current_thread})
        if on_stdout_line:
            on_stdout_line(line)
        outcome = outcomes.pop(0)
        if isinstance(outcome, dict) or outcome not in {'invalid', 'blocker'}:
            return subprocess.CompletedProcess(arguments, 1, line + '\n' + json.dumps({
                'type': 'turn.failed', 'error': (outcome if isinstance(outcome, dict) else {'message': ('unexpected worker exit' if outcome == 'failure' else outcome)}),
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
    monkeypatch.setattr(
        'agent_run.codex.time', SimpleNamespace(monotonic=time.monotonic, sleep=waits.append),
    )
    monkeypatch.setattr(cli, 'FakeExecutorHost', lambda **kwargs: FakeExecutorHost())
    monkeypatch.setattr(cli, 'CodexCliBackend', lambda **kwargs: CodexCliBackend(
        credential_provider=lambda: 'fixture-reader', **kwargs,
    ))

    def command(name: str, *extra: str) -> tuple[int, dict[str, Any]]:
        policy = ['--development-thread-policy', 'new-per-attempt'] if name == 'run' else []
        result = cli.main([name, '1', '--github-fixture', str(fixture), '--json', *policy, *extra])
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
    assert '只修正结果格式' in calls[4]['prompt']
    arguments = calls[4]['arguments']
    checkout_index = arguments.index(str(calls[4]['cwd']))
    assert arguments[checkout_index - 1] == '--ro-bind'
    assert '不重新开发、审查、验证、读取项目或调用工具' in calls[4]['prompt']
    assert all('resume' in call['arguments'] for call in calls[1:])

    code, blocked = command('resume')
    assert code == 2
    assert len(calls) == 6
    assert blocked['status'] == 'ready_for_human'
    assert blocked['diagnostics'][0]['code'] == 'agent_requires_human'
    assert blocked['active_agent_invocation']['status'] == 'completed'
    assert blocked['parent_job']['review_budget'] == budget


@pytest.mark.parametrize("machine_error", [None, "server_overloaded"])
def test_capacity_continuation_keeps_bounded_history_and_one_business_attempt(
    recovery_cli: Any, machine_error: str | None,
) -> None:
    command, outcomes, calls, waits = recovery_cli
    capacity = "Selected model is at capacity. Please try a different model."
    failure = capacity if machine_error is None else {'message': '服务暂时无法处理请求', 'codex_error_info': machine_error}
    outcomes.extend([failure] * 6 + ['blocker'])
    code, state = command('run')
    assert code == 2
    assert len(calls) == 7
    assert sum(waits) == 180
    assert len(state['agent_invocation_history']) == 1
    active = state['active_agent_invocation']
    assert active['capacity_recovery_count'] == 6
    assert active.get('machine_error') == machine_error
    assert active['ordinary_recovery_used'] is False
    assert active['status'] == 'completed'
    assert state['parent_job']['review_budget']['development_attempts'] == 1
    assert state['parent_job']['review_budget']['window'] == 1
    assert len(json.dumps(active)) < 12000
    assert all('resume' in call['arguments'] for call in calls[1:])


def test_human_reply_after_json_repair_preserves_allowance_and_writable_role(recovery_cli: Any) -> None:
    command, outcomes, calls, _waits = recovery_cli
    outcomes.extend(['invalid', 'invalid', 'blocker', 'invalid'])
    code, blocked = command('run')
    assert code == 2
    attempt = blocked['active_agent_invocation']['semantic_attempt']['attempt_id']
    code, failed = command('resume', '--message', '输入已补充，请继续当前任务。')
    assert code == 2
    assert len(calls) == 4
    assert failed['active_agent_invocation']['semantic_attempt']['attempt_id'] == attempt
    assert failed['active_agent_invocation']['output_attempt'] == 3
    assert '只修正结果格式' not in calls[-1]['prompt']
    arguments = calls[-1]['arguments']
    checkout_index = arguments.index(str(calls[-1]['cwd']))
    assert arguments[checkout_index - 1] == '--bind'
    assert 'resume' in calls[-1]['arguments']
    assert failed['parent_job']['development_thread_id'] == 'original-thread'
    assert failed['parent_job']['review_budget'] == blocked['parent_job']['review_budget']


def test_manual_thread_replacement_keeps_spent_counters_and_archives_identity(recovery_cli: Any) -> None:
    command, outcomes, calls, _waits = recovery_cli
    outcomes.extend(['invalid', 'invalid', 'failure', 'failure', 'blocker'])
    code, failed = command('run')
    assert code == 2
    first = failed['active_agent_invocation']
    code, blocked = command('resume', '--new-thread')
    assert code == 2
    active = blocked['active_agent_invocation']
    assert active['semantic_attempt']['attempt_id'] == first['semantic_attempt']['attempt_id']
    assert active['ordinary_recovery_used'] is True
    assert active['output_attempt'] == 3
    assert active['reported_thread_id'] == 'replacement-thread'
    assert blocked['parent_job']['development_thread_history'] == ['original-thread']
    assert blocked['parent_job']['review_budget'] == failed['parent_job']['review_budget']
    assert 'resume' not in calls[-1]['arguments']
    assert '只修正结果格式' not in calls[-1]['prompt']
    arguments = calls[-1]['arguments']
    checkout_index = arguments.index(str(calls[-1]['cwd']))
    assert arguments[checkout_index - 1] == '--bind'
