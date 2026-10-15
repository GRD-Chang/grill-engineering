from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_run import git as git_module
from agent_run import task_control as control_module
from agent_run.git import GitRepository
from agent_run.object_state import save_owned
from agent_run.run_driver import DirectRunOperations
from agent_run.state import StateStore
from agent_run.task_control import (
    ActionReconciliationError,
    TaskControlBusyError,
    TaskControlStore,
)
from support.control import _bind_running_executor
from support.control_state import snapshot_control
from support.lifecycle import _isolated_environment
from support.seeding import seed_run, write_fixture
from support.state import snapshot_run
from support.workspace import managed_repo, managed_state


@pytest.mark.parametrize("revocation", ["none", "stop", "generation"])
def test_git_fence_revalidates_after_add_before_commit(
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
    control, task, worker = _bind_running_executor(git_repo, run_id)
    try:
        record = control.load(task)
        assert record is not None
        identity = {
            "action_id": record["executor"]["action_id"],
            "generation": record["executor"]["generation"],
        }
        added = threading.Event()
        fence_ready = threading.Event()
        release_fence = threading.Event()

        def fence() -> None:
            if threading.current_thread().name.startswith("git-step") and added.is_set():
                fence_ready.set()
                assert release_fence.wait(5)
            assert not control._repository.in_transaction
            control.assert_executor_current(task, **identity, run_id=run_id)

        states._set_write_guard(
            partial(control.assert_executor_current, task, **identity, run_id=run_id),
            transaction=partial(
                control._executor_current_transaction, task, **identity, run_id=run_id,
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
        repository = GitRepository(managed_repo(git_repo))
        head = repository.checkout_head(managed_repo(git_repo))
        (managed_repo(git_repo) / "README.md").write_text("candidate\n")
        before_state = snapshot_run((states.root, str(run_id)))
        real_run = GitRepository._run_in
        real_dispatch = git_module.run_git
        dispatched: list[str] = []

        def run(repository, directory, *arguments):
            assert not control._repository.in_transaction
            result = real_run(repository, directory, *arguments)
            if arguments[0] == "add":
                added.set()
            return result

        def dispatch(command, **kwargs):
            assert not control._repository.in_transaction
            if command[1] == "commit":
                dispatched.append("commit")
            return real_dispatch(command, **kwargs)

        monkeypatch.setattr(GitRepository, "_run_in", run)
        monkeypatch.setattr(git_module, "run_git", dispatch)
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="git-step") as pool:
            future = pool.submit(
                operations.git.commit_candidate,
                managed_repo(git_repo),
                ticket_number=194,
                attempt=1,
                expected_head=head,
            )
            try:
                assert fence_ready.wait(5)
                if revocation == "stop":
                    control.claim_control_action(
                        task, kind="stop", payload={}, run_id=run_id,
                        state_dir=states.root,
                    )
                elif revocation == "generation":
                    control.finish_executor(task, **identity)
                    claim = control.claim_action(task, kind="resume", payload={})
                    control.begin_executor(task, action_id=claim.action_id, run_id=run_id)
            finally:
                release_fence.set()
            if revocation == "none":
                future.result(timeout=5)
            else:
                with pytest.raises(ActionReconciliationError):
                    future.result(timeout=5)
        assert dispatched == (["commit"] if revocation == "none" else [])
        if revocation == "none":
            assert repository.checkout_head(managed_repo(git_repo)) != head
            save_owned(states, run_id, {**state, "status": "completed"}, fields={"status"})
        else:
            assert repository.checkout_head(managed_repo(git_repo)) == head
            with pytest.raises(ActionReconciliationError):
                save_owned(states, run_id, {**state, "status": "completed"}, fields={"status"})
            assert snapshot_run((states.root, str(run_id))) == before_state
    finally:
        if worker.poll() is None:
            worker.kill()
        worker.wait(timeout=3)


def test_external_fence_reads_committed_state_while_writer_is_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = TaskControlStore(tmp_path / "state")
    task = control_module.TaskKey(tmp_path, "example/project", 1)
    claim = control.claim_action(task, kind="run", payload={})
    reservation = control.begin_executor(task, action_id=claim.action_id, run_id="run-1")
    identity = {
        "action_id": claim.action_id,
        "generation": reservation.generation,
        "run_id": "run-1",
    }
    before = snapshot_control(control, task)
    writer_ready = threading.Event()
    release_writer = threading.Event()

    def write() -> None:
        with control._locked(task):
            record = control._require_unlocked(task)
            record["updated_at"] = "2099-01-01T00:00:00Z"
            control._write_unlocked(task, record)
            writer_ready.set()
            assert release_writer.wait(5)

    def unexpected_retry(_delay) -> None:
        pytest.fail("read-only authorization must not wait for the writer")

    monkeypatch.setattr(control_module, "sleep", unexpected_retry)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(write)
        try:
            assert writer_ready.wait(5)
            assert not control._repository.in_transaction
            control.assert_executor_current(task, **identity)
            assert snapshot_control(control, task) == before
            with pytest.raises(TaskControlBusyError):
                control.claim_control_action(
                    task, kind="stop", payload={}, run_id="run-1",
                    state_dir=control.root,
                )
        finally:
            release_writer.set()
        pending.result(timeout=5)
    assert control.load(task)["updated_at"] == "2099-01-01T00:00:00Z"
