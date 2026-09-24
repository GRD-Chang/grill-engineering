from __future__ import annotations

import os
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

from agent_run.git import GitRepository
from agent_run.run_driver import DirectRunOperations
from agent_run.state import StateStore
from agent_run.task_control import ActionReconciliationError, TaskControlStore, TaskKey


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def test_worktree_prune_waits_for_another_checkout_add(
    git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A prune during worktree add can delete the new checkout's incomplete
    # registry entry even when the two callers use different GitRepository
    # objects for the same Run repository.
    from agent_run import git as git_module

    real_run = git_module.run_git
    add_entered, release_add, prune_entered = Event(), Event(), Event()
    checkout = tmp_path / "validation"

    def dispatch(command, **kwargs):
        if command[1:3] == ["worktree", "add"]:
            add_entered.set()
            assert release_add.wait(10), "test did not release worktree add"
        elif command[1:3] == ["worktree", "prune"] and add_entered.is_set():
            prune_entered.set()
        return real_run(command, **kwargs)

    monkeypatch.setattr(git_module, "run_git", dispatch)
    adding = GitRepository(git_repo)
    pruning = GitRepository(git_repo)
    prune_started = Event()

    def prune() -> None:
        prune_started.set()
        pruning.prune_worktrees()

    with ThreadPoolExecutor(max_workers=2) as executor:
        add_future = executor.submit(
            adding.prepare_validation_checkout,
            head_sha=_git(git_repo, "rev-parse", "HEAD"), checkout=checkout,
        )
        try:
            assert add_entered.wait(10)
            prune_future = executor.submit(prune)
            assert prune_started.wait(10)
            assert not prune_entered.wait(0.2)
        finally:
            release_add.set()
        add_future.result(timeout=10)
        prune_future.result(timeout=10)
    assert prune_entered.is_set()
    assert checkout.is_dir()
    assert f"worktree {checkout}" in _git(git_repo, "worktree", "list", "--porcelain")


@pytest.mark.parametrize("revocation", ["stop", "generation", "none"])
@pytest.mark.parametrize("operation", ["prune", "add", "remove"])
def test_worktree_lock_wait_rechecks_executor_ownership(
    git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    operation: str, revocation: str,
) -> None:
    from agent_run import git as git_module

    head = _git(git_repo, "rev-parse", "HEAD")
    checkout = tmp_path / "orphan"
    registry = git_repo / ".git" / "worktrees" / checkout.name
    if operation in {"prune", "remove"}:
        _git(git_repo, "worktree", "add", "--detach", str(checkout), head)
        assert registry.is_dir()
        if operation == "prune":
            shutil.rmtree(checkout)

    states = StateStore(tmp_path / "state")
    run_id = "run-156"
    states.save_run(run_id, {"run_id": run_id, "status": "running"})
    task = TaskKey(git_repo, "example/project", 156)
    control = TaskControlStore(states.root)
    claim = control.claim_action(task, kind="run", payload={"parent": 156})
    reservation = control.begin_executor(task, action_id=claim.action_id, run_id=run_id)
    identity = {"action_id": claim.action_id, "generation": reservation.generation}
    control.mark_handshake(task, **identity, pid=os.getpid(), process_start_token=None)
    control.record_application(
        task, **identity, run_id=run_id, payload_digest=claim.action["payload_digest"]
    )
    control.complete_action(task, **identity)
    repository = GitRepository(git_repo)
    repository._set_write_guard(
        partial(control.assert_executor_current, task, **identity, run_id=run_id)
    )
    checked = Event()
    original_check = repository._check_write_guard

    def observed_check() -> None:
        original_check()
        checked.set()

    monkeypatch.setattr(repository, "_check_write_guard", observed_check)
    if operation == "prune":
        action = repository.prune_worktrees
    elif operation == "add":
        action = partial(
            repository._run, "worktree", "add", "--detach", str(checkout), head,
        )
    else:
        action = partial(repository._run, "worktree", "remove", "--force", str(checkout))

    with ThreadPoolExecutor(max_workers=1) as executor:
        with git_module._worktree_registry_lock:
            future = executor.submit(action)
            assert checked.wait(10), "Git dispatch did not reach the lock"
            assert not future.done()
            if revocation == "stop":
                control.claim_control_action(
                    task, kind="stop", payload={}, run_id=run_id, state_dir=states.root,
                )
            elif revocation == "generation":
                control.finish_executor(task, **identity)
                replacement = control.claim_action(task, kind="resume", payload={})
                successor = control.begin_executor(
                    task, action_id=replacement.action_id, run_id=run_id,
                )
                assert successor.generation > reservation.generation
        if revocation == "none":
            result = future.result(timeout=10)
            if result is not None:
                assert result.returncode == 0
        else:
            with pytest.raises(ActionReconciliationError):
                future.result(timeout=10)

    if operation == "add":
        assert checkout.exists() == (revocation == "none")
        assert registry.exists() == (revocation == "none")
    else:
        assert registry.exists() == (revocation != "none")
        if operation == "remove":
            assert checkout.exists() == (revocation != "none")


def test_cached_commit_check_still_observes_executor_revocation(git_repo: Path) -> None:
    repository = GitRepository(git_repo)
    head = _git(git_repo, "rev-parse", "HEAD")
    assert repository._commit_exists(head)

    def revoked() -> None:
        raise ActionReconciliationError("executor ownership revoked")

    repository._set_write_guard(revoked)
    with pytest.raises(ActionReconciliationError, match="ownership revoked"):
        repository._commit_exists(head)


@pytest.mark.parametrize("revocation", ["stop", "generation", "none"])
@pytest.mark.parametrize("operation", ["candidate", "restore", "worktree", "integration"])
def test_git_successor_write_rechecks_executor_ownership(
    git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    operation: str, revocation: str,
) -> None:
    # Every Git mutation in this test belongs to a disposable real repository.
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for variable in ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME"):
        monkeypatch.setenv(variable, str(home / variable))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    root = git_repo
    _git(root, "branch", "-m", "agent-run/test")
    head = _git(root, "rev-parse", "HEAD")
    default_head = head
    if operation == "integration":
        _git(root, "checkout", "-b", "default")
        (root / "default-file").write_text("default\n")
        _git(root, "add", ".")
        _git(root, "commit", "-m", "default")
        default_head = _git(root, "rev-parse", "HEAD")
        _git(root, "checkout", "agent-run/test")
        (root / "run-file").write_text("run\n")
        _git(root, "add", ".")
        _git(root, "commit", "-m", "run")
        head = _git(root, "rev-parse", "HEAD")
        _git(root, "merge", "--no-commit", "--no-ff", "default")
    else:
        (root / "README.md").write_text("candidate\n")
    untracked = root / "untracked"
    untracked.write_text("must survive rejected clean\n")

    task = TaskKey(root, "example/project", 156)
    states = StateStore(tmp_path / "state")
    state = {"run_id": "run-156", "status": "running"}
    states.save_run("run-156", state)
    state_path = states.runs_directory / "run-156.json"
    before_state = state_path.read_bytes()
    control = TaskControlStore(states.root)
    claim = control.claim_action(task, kind="run", payload={"parent": 156})
    reservation = control.begin_executor(task, action_id=claim.action_id, run_id="run-156")
    identity = {"action_id": claim.action_id, "generation": reservation.generation}
    control.mark_handshake(task, **identity, pid=os.getpid(), process_start_token=None)
    control.record_application(
        task, **identity, run_id="run-156", payload_digest=claim.action["payload_digest"]
    )
    control.complete_action(task, **identity)
    fence = partial(control.assert_executor_current, task, **identity, run_id="run-156")
    states._set_write_guard(
        fence,
        transaction=partial(control._executor_current_transaction, task, **identity, run_id="run-156"),
    )
    operations = DirectRunOperations(
        controller=SimpleNamespace(states=states), states=states,
        git=GitRepository(root), github_reader=object(), publisher_factory=object,
        agents=object(), before_external_step=fence,
    )
    checkout = tmp_path / "ticket"
    if operation == "candidate":
        first, forbidden = "add", "commit"
        action = partial(operations.git.commit_candidate, root, ticket_number=194, attempt=1, expected_head=head)
    elif operation == "restore":
        first, forbidden = "reset", "clean"
        action = partial(operations.git.restore_managed_checkout, root, expected_head=head)
    elif operation == "worktree":
        first, forbidden = "branch", "worktree"
        action = partial(operations.git.prepare_ticket_checkout, branch="agent-run/ticket", base_sha=head, checkout=checkout)
    else:
        first, forbidden = "add", "commit-tree"
        action = partial(operations.git.commit_merge_resolution_candidate, root, run_head_sha=head, default_head_sha=default_head, attempt=1)

    entered, release = Event(), Event()
    dispatched: list[tuple[str, ...]] = []
    from agent_run import git as git_module
    real_run = git_module.run_git

    def dispatch(command, **kwargs):
        assert command[0] == "git"
        dispatched.append(tuple(command[1:]))
        result = real_run(command, **kwargs)
        if command[1] == first and result.returncode == 0:
            entered.set()
            assert release.wait(10), "test did not release completed Git operation"
        return result

    monkeypatch.setattr(git_module, "run_git", dispatch)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(action)
        try:
            assert entered.wait(10), "first Git mutation was not dispatched"
            if revocation == "stop":
                control.claim_control_action(task, kind="stop", payload={}, run_id="run-156", state_dir=states.root)
            elif revocation == "generation":
                control.finish_executor(task, **identity)
                replacement = control.claim_action(task, kind="resume", payload={})
                next_executor = control.begin_executor(task, action_id=replacement.action_id, run_id="run-156")
                assert next_executor.generation > reservation.generation
        finally:
            release.set()
        if revocation == "none":
            future.result(timeout=10)
        else:
            with pytest.raises(ActionReconciliationError):
                future.result(timeout=10)
    monkeypatch.setattr(git_module, "run_git", real_run)
    successor_commands = [args for args in dispatched if args[0] == forbidden and (forbidden != "worktree" or args[1] == "add")]
    assert sum(args[0] == first for args in dispatched) == 1
    assert len(successor_commands) == (1 if revocation == "none" else 0)
    if operation == "integration" and revocation != "none":
        assert not any(args[0] == "write-tree" for args in dispatched)
    if revocation != "none":
        assert _git(root, "rev-parse", "HEAD") == head
        with pytest.raises(ActionReconciliationError):
            states.save_run("run-156", {**state, "status": "completed"})
        assert state_path.read_bytes() == before_state
    else:
        states.save_run("run-156", {**state, "status": "completed"})
        assert states.load_run("run-156")["status"] == "completed"
    if operation == "restore":
        assert untracked.exists() == (revocation != "none")
    elif operation == "worktree":
        assert checkout.exists() == (revocation == "none")
    elif revocation == "none":
        assert _git(root, "rev-parse", "HEAD") != head
