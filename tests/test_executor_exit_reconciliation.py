import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from support.workspace import managed_repo, managed_state

from agent_run.executor_host import _process_start_token
from agent_run.cli import main
from agent_run.run_lifecycle import prepare_action_application_receipt
from agent_run.state import StateStore
from agent_run.task_control import TaskControlStore, TaskKey
from cli_run_supervision_support import _parent_only_agents
from conftest import seed_idle_control, seed_run, write_fixture
from test_ticket_194_stop_abandon import _run_cli


def test_stop_idle_delivery_persists_operator_pause(git_repo: Path) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    assert seed_run(git_repo, fixture).returncode == 0
    states = StateStore(managed_state(git_repo))
    original = states.find_unfinished_runs("example/project", 1)[0]
    run_id = original["run_id"]
    control = TaskControlStore(states.root)
    task = TaskKey(managed_repo(git_repo), "example/project", 1)
    seed_idle_control(control, task, run_id, state_dir=states.root)

    result = _run_cli(git_repo, fixture, "stop", run_id)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "operator_stopped"
    assert states.load_run(run_id)["status"] == "operator_stopped"
    before = control.path_for(task).read_bytes()
    repeated = _run_cli(git_repo, fixture, "stop", run_id)
    assert repeated.returncode == 0, repeated.stderr
    assert control.path_for(task).read_bytes() == before


@pytest.mark.parametrize("case", [
    "exited", "orphan", "recorded_exit_orphan", "unknown", "save_interrupted",
])
def test_resume_reconciles_externally_exited_executor(
    git_repo: Path, case: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    assert seed_run(git_repo, fixture).returncode == 0
    states = StateStore(managed_state(git_repo))
    current = states.find_unfinished_runs("example/project", 1)[0]
    run_id = current["run_id"]
    control = TaskControlStore(states.root)
    task = TaskKey(managed_repo(git_repo), "example/project", 1)
    claim = control.claim_action(task, kind="run", payload={"parent": 1})
    reservation = control.begin_executor(task, action_id=claim.action_id, run_id=run_id)
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait(timeout=3)
    token = None if case == "unknown" else "old"
    control.mark_process_started(task, action_id=claim.action_id,
        generation=reservation.generation, pid=child.pid, process_start_token=token)
    control.mark_handshake(task, action_id=claim.action_id,
        generation=reservation.generation, pid=child.pid, process_start_token=token)
    control.bind_run(task, claim.action_id, run_id, generation=reservation.generation,
        state_dir=states.root)
    prepare_action_application_receipt(current, claim.action)
    states.save_run(run_id, current)
    control.record_application(task, action_id=claim.action_id, run_id=run_id,
        generation=reservation.generation, payload_digest=claim.action["payload_digest"])
    control.complete_action(task, action_id=claim.action_id,
        generation=reservation.generation, result_status=current["status"])

    worker = None
    try:
        if case in {"orphan", "recorded_exit_orphan"}:
            worker = subprocess.Popen([sys.executable, "-c", "import signal; signal.pause()"],
                start_new_session=True)
            token = _process_start_token(worker.pid)
            assert token is not None
            control.mark_worker_started(task, action_id=claim.action_id,
                generation=reservation.generation, pid=worker.pid, process_start_token=token)
            if case == "recorded_exit_orphan":
                control.finish_executor(task, action_id=claim.action_id,
                    generation=reservation.generation)
        before = states.load_run(run_id)
        control_before = control.path_for(task).read_bytes()
        if case == "save_interrupted":
            original_save = StateStore.save_run

            def fail_interruption_save(
                self: StateStore, selected_run_id: str, value: dict[str, Any],
            ) -> None:
                if any(item.get("code") == "session_interrupted"
                       for item in value.get("diagnostics", [])):
                    raise OSError("injected Run persistence failure")
                return original_save(self, selected_run_id, value)

            with monkeypatch.context() as patch:
                patch.chdir(git_repo)
                patch.setattr(StateStore, "save_run", fail_interruption_save)
                assert main([
                    "resume", run_id, "--json", "--github-fixture",
                    str(fixture), "--agent-fixture",
                    str(_parent_only_agents(git_repo / "agents.json")),
                ]) == 2
            assert control.load(task)["executor"]["status"] == "exited"
            assert states.load_run(run_id) == before
        result = _run_cli(git_repo, fixture, "resume", run_id, "--agent-fixture",
            str(_parent_only_agents(git_repo / "agents.json")))
        if case == "unknown":
            assert result.returncode == 2, result.stdout + result.stderr
            assert "退出证据不足" in result.stdout
            assert states.load_run(run_id) == before
            assert control.path_for(task).read_bytes() == control_before
        elif worker is not None:
            assert result.returncode == 2, result.stdout + result.stderr
            assert "stop" in result.stdout
            assert worker.poll() is None
            assert states.load_run(run_id) == before
            assert control.path_for(task).read_bytes() == control_before
            stopped = _run_cli(git_repo, fixture, "stop", run_id)
            assert stopped.returncode == 0, stopped.stdout + stopped.stderr
            worker.wait(timeout=3)
            assert states.load_run(run_id)["status"] == "operator_stopped"
        else:
            assert result.returncode == 0, result.stdout + result.stderr
            assert states.load_run(run_id)["status"] == "parent_approval_pending"
            assert control.load(task)["action"]["kind"] == "resume"
    finally:
        if worker is not None:
            if worker.poll() is None:
                worker.kill()
            worker.wait(timeout=3)
