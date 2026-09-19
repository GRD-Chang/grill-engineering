from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
from types import SimpleNamespace

import pytest

from support.workspace import managed_repo, managed_state

from conftest import seed_run, write_fixture
from test_run_lifecycle import _isolated_environment
from test_receipt_successors import _install_host
from test_ticket_194_stop_abandon import _bind_running_executor
from agent_run import cli
from agent_run import git as git_module
from agent_run import task_control as control_module
from agent_run.github_fixture import FixtureGitHubReader
from agent_run.git import GitRepository
from agent_run.run_driver import DirectRunOperations
from agent_run.state import StateStore
from agent_run.systemd_executor_host import SystemdExecutionReadinessError
from agent_run.task_control import (
    ActionReconciliationError,
    TaskControlBusyError,
    TaskControlStore,
)


@pytest.mark.parametrize("revocation", ["none", "stop", "generation"])
def test_cli_stop_preflight_contention_revalidates_before_commit(
    git_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    revocation: str,
) -> None:
    for key, value in _isolated_environment(tmp_path / "observer").items():
        monkeypatch.setenv(key, value)
    fixture = write_fixture(git_repo / "github.json", issues={})
    assert seed_run(git_repo, fixture).returncode == 0
    states = StateStore(managed_state(git_repo))
    state = states.find_unfinished_runs("example/project", 1)[0]
    run_id = state["run_id"]
    # Only a terminal Run now takes the read-only Stop preflight. Keep the
    # real ownership-lock contention and subsequent fence revocation unchanged.
    state.update(status="completed", terminal_kind="completed")
    states.save_run(run_id, state)
    control, task, worker = _bind_running_executor(git_repo, run_id)
    try:
        record = control.load(task)
        assert record is not None
        identity = {
            "action_id": record["executor"]["action_id"],
            "generation": record["executor"]["generation"],
        }
        fence = partial(
            control.assert_executor_current, task, **identity, run_id=run_id
        )
        states._set_write_guard(
            fence,
            transaction=partial(
                control._executor_current_transaction, task, **identity, run_id=run_id
            ),
        )
        operations = DirectRunOperations(
            controller=SimpleNamespace(states=states),
            states=states,
            git=GitRepository(managed_repo(git_repo)),
            github_reader=object(),
            publisher_factory=object,
            agents=object(),
            before_external_step=fence,
        )
        head = GitRepository(managed_repo(git_repo)).checkout_head(managed_repo(git_repo))
        (managed_repo(git_repo) / "README.md").write_text("candidate\n")
        before_state = (states.runs_directory / f"{run_id}.json").read_bytes()
        before_control = control.path_for(task).read_bytes()
        host = _install_host(git_repo, fixture, monkeypatch, "running")
        monkeypatch.setattr(
            cli, "GhGitHubReader", lambda *args, **kwargs: FixtureGitHubReader(fixture)
        )

        def unavailable() -> None:
            raise SystemdExecutionReadinessError("isolated Host unavailable")

        monkeypatch.setattr(host, "check_readiness", unavailable)
        added, preflight_locked, release_preflight = (
            threading.Event(),
            threading.Event(),
            threading.Event(),
        )
        retry_seen, release_retry = threading.Event(), threading.Event()
        real_read = TaskControlStore._read_unlocked
        real_run = GitRepository._run_in
        real_dispatch = git_module.run_git
        dispatched: list[str] = []

        def read(store, key):
            if threading.current_thread().name.startswith("stop-cli"):
                preflight_locked.set()
                assert release_preflight.wait(5)
            return real_read(store, key)

        def run(repository, directory, *arguments):
            result = real_run(repository, directory, *arguments)
            if arguments[0] == "add":
                added.set()
                assert preflight_locked.wait(5)
            return result

        def dispatch(command, **kwargs):
            if command[1] == "commit":
                dispatched.append("commit")
            return real_dispatch(command, **kwargs)

        def retry_sleep(_delay):
            retry_seen.set()
            assert release_retry.wait(5)

        monkeypatch.setattr(TaskControlStore, "_read_unlocked", read)
        monkeypatch.setattr(GitRepository, "_run_in", run)
        monkeypatch.setattr(git_module, "run_git", dispatch)
        monkeypatch.setattr(control_module, "sleep", retry_sleep)
        with (
            ThreadPoolExecutor(max_workers=1) as executor,
            ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="stop-cli"
            ) as observer,
        ):
            future = executor.submit(
                operations.git.commit_candidate,
                managed_repo(git_repo),
                ticket_number=194,
                attempt=1,
                expected_head=head,
            )
            try:
                assert added.wait(5)
                stop = observer.submit(cli.main, ["stop", "1", "--json"])
                assert preflight_locked.wait(5), stop.result(timeout=1)
                assert retry_seen.wait(
                    5
                ), "accurate Executor did not retry the short lock"
                # New Action admission remains strictly nonblocking while preflight holds its lock.
                with pytest.raises(TaskControlBusyError):
                    control.claim_control_action(
                        task,
                        kind="stop",
                        payload={},
                        run_id=run_id,
                        state_dir=states.root,
                    )
                release_preflight.set()
                assert stop.result(timeout=5) == 0
                assert (states.runs_directory / f"{run_id}.json").read_bytes() == before_state
                assert control.path_for(task).read_bytes() == before_control
                if revocation == "stop":
                    control.claim_control_action(
                        task,
                        kind="stop",
                        payload={},
                        run_id=run_id,
                        state_dir=states.root,
                    )
                elif revocation == "generation":
                    control.finish_executor(task, **identity)
                    claim = control.claim_action(task, kind="resume", payload={})
                    control.begin_executor(
                        task, action_id=claim.action_id, run_id=run_id
                    )
            finally:
                release_preflight.set()
                release_retry.set()
            if revocation == "none":
                future.result(timeout=5)
            else:
                with pytest.raises(ActionReconciliationError):
                    future.result(timeout=5)
        assert dispatched == (["commit"] if revocation == "none" else [])
        if revocation == "none":
            assert GitRepository(managed_repo(git_repo)).checkout_head(managed_repo(git_repo)) != head
            states.save_run(run_id, {**state, "status": "completed"})
        else:
            assert GitRepository(managed_repo(git_repo)).checkout_head(managed_repo(git_repo)) == head
            with pytest.raises(ActionReconciliationError):
                states.save_run(run_id, {**state, "status": "completed"})
            assert (
                states.runs_directory / f"{run_id}.json"
            ).read_bytes() == before_state
    finally:
        if worker.poll() is None:
            worker.kill()
        worker.wait(timeout=3)


def test_executor_fence_contention_deadline_is_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = TaskControlStore(tmp_path / "state")
    task = control_module.TaskKey(tmp_path, "example/project", 1)
    claim = control.claim_action(task, kind="run", payload={})
    reservation = control.begin_executor(
        task, action_id=claim.action_id, run_id="run-1"
    )
    identity = {
        "action_id": claim.action_id,
        "generation": reservation.generation,
        "run_id": "run-1",
    }
    before = control.path_for(task).read_bytes()
    clock = iter([0.0, 0.4, 1.0])
    intervals: list[float] = []
    monkeypatch.setattr(control_module, "monotonic", lambda: next(clock))
    monkeypatch.setattr(control_module, "sleep", intervals.append)
    with control._locked(task):
        with pytest.raises(TaskControlBusyError):
            control.assert_executor_current(task, **identity)
        with pytest.raises(TaskControlBusyError):
            control.claim_control_action(
                task,
                kind="stop",
                payload={},
                run_id="run-1",
                state_dir=control.directory.parent,
            )
    assert intervals == [0.01]
    assert control.path_for(task).read_bytes() == before
