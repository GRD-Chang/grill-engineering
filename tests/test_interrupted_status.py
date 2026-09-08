from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_run import cli
from agent_run.executor_host import HostObservation

from cli_fixtures import run_agents
from conftest import write_fixture
from test_cli import run_cli, stdout_json
from test_cli_delivery import ticket
from test_run_lifecycle import _file_snapshot, _isolated_environment


@pytest.mark.parametrize("case", ["interrupted", "unknown", "ended", "capacity"])
def test_interrupted_agent_has_unknown_duration_and_recovery_guidance(
    git_repo: Path,
    tmp_path: Path,
    case: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fixture = write_fixture(git_repo / 'github.json', issues={'3': ticket()})
    agents = run_agents(git_repo / 'agents.json')
    environment = _isolated_environment(tmp_path / 'status')
    first = run_cli(
        git_repo, fixture, 'run', '1', '--agent-fixture', str(agents),
        extra_env=environment,
    )
    assert first.returncode == 0, first.stderr
    run_id = stdout_json(first)['run_id']
    state_path = next((git_repo / '.agent-run' / 'runs').glob('*.json'))
    state = json.loads(state_path.read_text())
    invocation = dict(state['agent_invocation_history'][-1])
    invocation.update(
        status='running', started_at='2026-09-07T01:39:19+00:00', ended_at=None,
        deadline_at='2026-09-07T03:39:19+00:00',
    )
    state['status'] = 'run_publication'
    state['run_publication']['phase'] = 'publishing'
    state['run_publication']['pending_semantic_attempt'] = dict(invocation['semantic_attempt'])
    state['run_publication']['semantic_attempt_history'] = []
    state['active_agent_invocation'] = invocation
    state['agent_invocation_history'][-1] = dict(invocation)
    if case == 'ended':
        invocation['ended_at'] = '2026-09-07T01:47:25+00:00'
        state['agent_invocation_history'][-1] = dict(invocation)
    control_path = next((git_repo / '.agent-run' / 'task-control').glob('*.json'))
    if case == 'unknown':
        control_path.unlink()
    if case == 'capacity':
        invocation.update(
            status='resuming', recovery_waiting=True, recovery_kind='capacity',
            capacity_recovery_count=3, ordinary_recovery_used=False,
        )
        state['agent_invocation_history'][-1] = dict(invocation)
        control = json.loads(control_path.read_text())
        control['executor'].update(status='running', pid=123)
        control_path.write_text(json.dumps(control))
        monkeypatch.setattr(
            cli, 'observe_systemd_executor',
            lambda spec, *args, **kwargs: HostObservation(
                'running', spec.generation, 123, True
            ),
        )
    state_path.write_text(json.dumps(state))
    monkeypatch.chdir(git_repo)
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    before = _file_snapshot(git_repo)
    for command in ('status', 'history'):
        assert cli.main([command, run_id, '--json']) == 0
        output = json.loads(capsys.readouterr().out)
        expected_activity = {
            'unknown': 'unknown', 'capacity': 'running',
        }.get(case, 'not_running')
        assert output['executor_control']['activity'] == expected_activity
        if command == 'status':
            agent = output['progress']['current_agent']
            assert agent['is_active'] is False
            assert agent['duration_seconds'] == (486 if case == 'ended' else None)
            assert agent['remaining_seconds'] is None
            assert output['agent_invocation']['status'] == invocation['status']
        else:
            events = [
                event for event in output['events']
                if event.get('at') == (invocation.get('ended_at') or invocation['started_at'])
                and event.get('invocation_role')
            ]
            assert events[-1]['duration_seconds'] == (486 if case == 'ended' else None)
        assert cli.main([command, run_id]) == 0
        human = capsys.readouterr().out
        expected = ('运行状态无法确认' if case == 'unknown' else
                    '模型容量不足，等待 30 秒后自动续接' if case == 'capacity' else
                    '执行已中断，等待恢复')
        assert expected in human
        if case != 'ended':
            assert '实际执行时长未知' in human
        assert '2026-09-07' in human
        assert '你暂时无需操作' not in human
        assert '本轮剩余' not in human
    assert _file_snapshot(git_repo) == before


def test_normal_approval_gate_is_not_reported_as_interrupted(
    git_repo: Path, tmp_path: Path,
) -> None:
    fixture = write_fixture(git_repo / 'github.json', issues={'3': ticket()})
    agents = run_agents(git_repo / 'agents.json')
    environment = _isolated_environment(tmp_path / 'approval')
    first = run_cli(
        git_repo, fixture, 'run', '1', '--agent-fixture', str(agents),
        extra_env=environment,
    )
    assert first.returncode == 0, first.stderr
    run_id = stdout_json(first)['run_id']
    for command in ('status', 'history'):
        result = run_cli(git_repo, fixture, command, run_id, extra_env=environment)
        assert result.returncode == 0, result.stderr
        assert '执行已中断' not in result.stdout
        assert 'approve' in result.stdout


def test_budget_checkpoint_explains_new_window_authorization(git_repo: Path) -> None:
    from test_resume_intent import _checkpoint

    fixture, _, state = _checkpoint(git_repo)
    before = _file_snapshot(git_repo)
    for command in ("status", "history"):
        result = run_cli(git_repo, fixture, command, state["run_id"])
        assert result.returncode == 0, result.stderr
        assert "resume 将授权新的预算窗口" in result.stdout
    assert _file_snapshot(git_repo) == before
