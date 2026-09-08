from __future__ import annotations

import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
from threading import Event

import pytest

from agent_run import github_publish
from agent_run.git import GitRepository
from agent_run.github_publish import GhGitHubPublisher
from agent_run.run_driver import _FencedExternal, _fenced_publisher
from agent_run.state import StateStore
from agent_run.task_control import ActionReconciliationError, TaskControlStore, TaskKey


def _publisher(tmp_path: Path):
    task = TaskKey(tmp_path, "example/project", 156)
    states = StateStore(tmp_path / "state")
    state = {"run_id": "run-156", "status": "running"}
    states.save_run("run-156", state)
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
    git = GitRepository(tmp_path)
    publisher = _fenced_publisher(
        GhGitHubPublisher("example/project", git),
        fenced_git=_FencedExternal(git, fence),
        fence=fence,
    )
    return publisher, control, task, identity, states, state


@pytest.mark.parametrize("revocation", ["stop", "generation", "none"])
def test_parent_comment_in_flight_cannot_dispatch_close_after_revocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, revocation: str
) -> None:
    publisher, control, task, identity, states, state = _publisher(tmp_path)
    before = (states.runs_directory / "run-156.json").read_bytes()
    entered, release = Event(), Event()
    mutations: list[str] = []
    remote = {"state": "OPEN", "comments": []}

    def read(command, **kwargs):
        assert command[:3] == ["gh", "issue", "view"]
        return subprocess.CompletedProcess(command, 0, json.dumps(remote), "")

    def write(command, **kwargs):
        operation = command[2]
        mutations.append(operation)
        if operation == "comment":
            entered.set()
            assert release.wait(5), "test did not release the in-flight comment"
            remote["comments"].append({"body": command[command.index("--body") + 1]})
        else:
            assert operation == "close"
            remote["state"] = "CLOSED"
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(github_publish, "run_read_command", read)
    monkeypatch.setattr(github_publish, "run_write_command", write)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            publisher.close_parent_issue,
            parent_number=156, run_id="run-156", pr_number=5,
            integrated_sha="a" * 40, delivery_type="Run",
        )
        try:
            assert entered.wait(5), "Publisher did not dispatch the comment"
            if revocation == "stop":
                control.claim_control_action(
                    task, kind="stop", payload={}, run_id="run-156", state_dir=states.root
                )
            elif revocation == "generation":
                control.finish_executor(task, **identity)
                next_action = control.claim_action(task, kind="resume", payload={})
                replacement = control.begin_executor(task, action_id=next_action.action_id, run_id="run-156")
                assert replacement.generation > identity["generation"]
        finally:
            release.set()
        if revocation == "none":
            future.result(timeout=5)
        else:
            with pytest.raises(ActionReconciliationError):
                future.result(timeout=5)
    assert mutations == (["comment", "close"] if revocation == "none" else ["comment"])
    if revocation != "none":
        assert remote["state"] == "OPEN"
        with pytest.raises(ActionReconciliationError):
            states.save_run("run-156", {**state, "status": "completed"})
        assert (states.runs_directory / "run-156.json").read_bytes() == before


@pytest.mark.parametrize("revoke_after_write", [False, True])
def test_git_dispatch_rechecks_ownership_but_inflight_readback_is_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, revoke_after_write: bool
) -> None:
    publisher, control, task, identity, states, _state = _publisher(tmp_path)
    writes = []
    reads = []
    head = "a" * 40
    previous = "b" * 40

    def revoke():
        control.claim_control_action(task, kind="stop", payload={}, run_id="run-156", state_dir=states.root)

    def read(command, **kwargs):
        reads.append(command)
        if len(reads) == 1:
            if not revoke_after_write:
                revoke()
            sha = previous
        else:
            sha = head
        return subprocess.CompletedProcess(command, 0, f"{sha}\trefs/heads/topic\n", "")

    def write(command, **kwargs):
        writes.append(command)
        assert command[:3] == ["git", "push", "origin"]
        revoke()
        return subprocess.CompletedProcess(command, 1, "", "response lost")

    monkeypatch.setattr(github_publish, "run_read_command", read)
    monkeypatch.setattr(github_publish, "run_write_command", write)
    if revoke_after_write:
        publisher.publish_branch("topic", head, expected_remote_sha=previous)
        assert len(writes) == 1 and len(reads) == 2
    else:
        with pytest.raises(ActionReconciliationError):
            publisher.publish_branch("topic", head, expected_remote_sha=previous)
        assert writes == [] and len(reads) == 1


@pytest.mark.parametrize("operation", ["create", "delete", "ensure_run", "update_ref"])
def test_direct_git_mutations_revalidate_after_the_preceding_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    publisher, control, task, _identity, states, _state = _publisher(tmp_path)
    writes = []
    head = "a" * 40
    branch = "agent-run/run-156"

    def revoke():
        control.claim_control_action(task, kind="stop", payload={}, run_id="run-156", state_dir=states.root)

    def read(command, **kwargs):
        revoke()
        value = f"{head}\trefs/heads/{branch}\n" if operation == "delete" else ""
        return subprocess.CompletedProcess(command, 0, value, "")

    def write(command, **kwargs):
        writes.append(command)
        assert command[1] == "fetch", "revoked Executor dispatched a later Git mutation"
        return subprocess.CompletedProcess(command, 0, "", "")

    def resolve(_git, ref):
        assert ref == "FETCH_HEAD"
        revoke()
        return head

    monkeypatch.setattr(github_publish, "run_read_command", read)
    monkeypatch.setattr(github_publish, "run_write_command", write)
    monkeypatch.setattr(GitRepository, "resolve", resolve)
    with pytest.raises(ActionReconciliationError):
        if operation == "delete":
            publisher.delete_managed_branch(branch)
        elif operation == "update_ref":
            publisher.sync_run_branch(run_branch=branch, integrated_sha=head)
        else:
            publisher.ensure_change_branch(
                branch="topic", base_branch=branch if operation == "ensure_run" else "main",
                expected_base_sha=head, expected_remote_sha=head, recovery_remote_sha=head,
            )
    assert len(writes) == (1 if operation == "update_ref" else 0)


def test_pr_creation_allows_explicit_get_readback_after_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publisher, control, task, _identity, states, _state = _publisher(tmp_path)
    commands = []
    head, base = "a" * 40, "b" * 40
    pull = {
        "number": 5,
        "head": {"ref": "topic", "sha": head, "repo": {"full_name": "example/project"}},
        "base": {"ref": "main", "sha": base, "repo": {"full_name": "example/project"}},
    }

    def dispatch(command, **kwargs):
        commands.append(command)
        if command[1:3] == ["pr", "create"]:
            control.claim_control_action(task, kind="stop", payload={}, run_id="run-156", state_dir=states.root)
            result = ""
        else:
            assert command[1] == "api" and command[command.index("--method") + 1] == "GET"
            result = json.dumps([] if len(commands) == 1 else [pull])
        return subprocess.CompletedProcess(command, 0, result, "")

    def unexpected_retry(*args, **kwargs):
        pytest.fail("explicit GET must keep its existing single-dispatch behavior")

    monkeypatch.setattr(github_publish, "run_write_command", dispatch)
    monkeypatch.setattr(github_publish, "run_read_command", unexpected_retry)
    assert publisher.ensure_change_pr(
        branch="topic", base_branch="main", title="change", body="description",
        expected_head_sha=head, expected_base_sha=base,
    ) == 5
    assert len(commands) == 3
