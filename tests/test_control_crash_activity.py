from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_run import cli
from agent_run.executor_host import FakeExecutorHost, HostObservation
from agent_run.state import StateStore
from conftest import seed_run, write_fixture
from test_run_lifecycle import _file_snapshot
from test_ticket_194_stop_abandon import _bind_running_executor


@pytest.mark.parametrize('kind', ['stop', 'abandon'])
def test_control_receipt_crash_keeps_target_activity_unknown(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], kind: str
) -> None:
    fixture = write_fixture(git_repo / 'github.json', issues={})
    assert seed_run(git_repo, fixture).returncode == 0
    states = StateStore(git_repo / '.agent-run')
    state = states.find_unfinished_runs('example/project', 1)[0]
    run_id = str(state['run_id'])
    control, task, worker = _bind_running_executor(git_repo, run_id)
    monkeypatch.chdir(git_repo)
    monkeypatch.setattr(cli, 'FakeExecutorHost', lambda **kwargs: FakeExecutorHost(separate_process=True))
    try:
        assert cli.main([kind, run_id, '--json', '--github-fixture', str(fixture), '--crash-after-save', '1']) == 2
        capsys.readouterr()
        record = control.load(task)
        assert record is not None
        action = record['action']
        assert action['status'] == 'applying'
        assert action['target_executor']['worker']['pid'] == worker.pid
        assert states.load_run(run_id)['action_application_receipt']['action_id'] == action['action_id']
        assert worker.poll() is None

        def exited(spec, *_args, **_kwargs):
            return HostObservation('exited', spec.generation, None, False)

        monkeypatch.setattr(cli, 'observe_systemd_executor', exited)
        before = _file_snapshot(states.root)
        for _ in range(2):
            for command in ('status', 'history'):
                assert cli.main([command, run_id, '--json']) == 0
                view = json.loads(capsys.readouterr().out)
                assert view['executor_control']['activity'] == 'unknown'
                assert _file_snapshot(states.root) == before
                assert worker.poll() is None

        # Process disappearance alone is not a durable target-exit receipt.
        worker.terminate()
        worker.wait(timeout=3)
        for command in ('status', 'history'):
            assert cli.main([command, run_id, '--json']) == 0
            view = json.loads(capsys.readouterr().out)
            assert view['executor_control']['activity'] == 'unknown'
            assert _file_snapshot(states.root) == before
    finally:
        if worker.poll() is None:
            worker.kill()
        worker.wait(timeout=3)
