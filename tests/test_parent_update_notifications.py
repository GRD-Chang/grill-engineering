"""Updating is submitted before synchronization; transport cannot gate delivery."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from agent_run.executor_host import FakeExecutorHost
from agent_run.git import GitRepository
from agent_run.messages import text
from agent_run.notification_events import events
from agent_run.notifications import read_notifications
from agent_run.state import StateStore
from support.cli import load_only_run_state, stdout_json
from support.inprocess_cli import invoke_cli_inprocess
from support.parent_approval_update import append_remote, ready_parent, update_agents
from support.workspace import managed_state


@pytest.mark.parametrize("mode", ["concise", "detailed"])
@pytest.mark.parametrize("language", ["zh", "en"])
def test_update_notification_precedes_git_and_failed_delivery_does_not_block(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, mode: str, language: str,
) -> None:
    fixture, before = ready_parent(git_repo)
    root = managed_state(git_repo)
    states = StateStore(root)
    before["language"] = language
    before["notifications"] = {
        "enabled": True, "mode": mode, "profile": "isolated-test",
        "open_id": "ou_test", "app_id": "cli_test",
    }
    states.save_run(before["run_id"], before)
    append_remote(git_repo, fixture, "main", "remote.txt", "remote contribution\n")
    agents = update_agents(git_repo)
    sent: list[str] = []

    def unavailable(
        _config: Any, card: Any, _identity: Any, _cancel: Any, *, timeout_seconds: float,
    ) -> dict[str, Any]:
        assert 0 <= timeout_seconds <= 60
        sent.append(json.dumps(card, ensure_ascii=False))
        return {"outcome": "failed", "reason": "isolated transport unavailable"}

    monkeypatch.setattr("agent_run.notifications.send", unavailable)
    monkeypatch.setattr("agent_run.cli.FakeExecutorHost", lambda **_: FakeExecutorHost())
    integrate = GitRepository.integrate_remote_heads
    submitted: list[str] = []

    def verify_submission(self: GitRepository, *args: Any, **kwargs: Any) -> Any:
        assert text("cli.approval_update_intent", language=language) in sys.stderr.getvalue()
        current = states.load_current_run(before["run_id"])
        assert current is not None
        update = next(event for event in events(current) if event["kind"] == "approval_update")
        outbox = read_notifications(root, before["run_id"])
        assert update["id"] in outbox["seen"]
        assert not current["parent_job"].get("approval_grant")
        submitted.append(update["id"])
        return integrate(self, *args, **kwargs)

    monkeypatch.setattr(GitRepository, "integrate_remote_heads", verify_submission)
    result = invoke_cli_inprocess(
        git_repo, fixture, "approve", before["run_id"], "--agent-fixture", str(agents), "--json",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert submitted
    assert any(text("notification.event.approval_update", language=language) in value for value in sent)
    assert stdout_json(result)["action"]["status"] == "updating"
    final = load_only_run_state(git_repo)
    assert final["status"] == "parent_approval_pending"
    assert not final["parent_job"].get("approval_grant")
    assert json.loads(fixture.read_text())["delivery"]["pull_requests"][0]["state"] == "OPEN"
