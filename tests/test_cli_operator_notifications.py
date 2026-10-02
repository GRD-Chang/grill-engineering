"""Parallel Ticket human boundaries through the public lifecycle and outbox."""
from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from threading import Event
from typing import Any

import pytest

from agent_run import cli
from agent_run.git import GitError
from agent_run.messages import text
from agent_run.notification_events import events
from agent_run.notifications import Notifications, read_notifications
from agent_run.user_defaults import UserDefaultsStore
from support.cli import issue, load_only_run_state, run_cli, stdout_json
from support.cli_payloads import HUMAN_BLOCKER, human_blocker_step
from support.inprocess_cli import invoke_cli_inprocess
from support.seeding import write_fixture
from support.ticket_parallel import TicketAgents
from support.workspace import managed_state


@pytest.mark.parametrize("execution_failure", [False, True], ids=["human-blocker", "execution-failure"])
def test_noncurrent_ticket_notifies_while_sibling_continues_and_resumes_only_target(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, execution_failure: bool,
) -> None:
    # Real lifecycles cover the distinct Agent blocker and Controller failure
    # writers across concurrent selection and notification. Mode combinations
    # remain in fact unit tests.
    fixture = write_fixture(git_repo / "github.json", issues={
        "2": issue(2), "3": issue(3),
        "4": issue(4, blocked_by=[{"number": 3, "state": "OPEN"}]),
    })
    UserDefaultsStore().configure(notifications={
        "enabled": True, "profile": "test", "app_id": "cli_test", "open_id": "ou_test",
    })
    evidence_root = git_repo.parent / "operator-notification-evidence"
    evidence_root.mkdir()
    original_evidence = "git fetch failed: remote host unavailable" if execution_failure else HUMAN_BLOCKER
    blocked_reason = "ticket_execution_failed" if execution_failure else "agent_requires_human"

    def record(name: str, value: Any) -> None:
        with (evidence_root / f"{name}.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(value) + "\n")

    def records(name: str) -> list[Any]:
        path = evidence_root / f"{name}.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def blocker_cards() -> list[dict[str, Any]]:
        return [card for card in records("cards") if original_evidence in json.dumps(card)
                and card["header"]["subtitle"]["content"].endswith("#3")]

    senders: list[Notifications] = []
    sibling_started, blocker_sent = Event(), Event()
    original_observe, original_close = Notifications.observe, Notifications.close

    def send(_config: Any, card: dict[str, Any], _identity: Any, _cancel: Any) -> dict[str, Any]:
        record("cards", card)
        if original_evidence in json.dumps(card) and card["header"]["subtitle"]["content"].endswith("#3"):
            blocker_sent.set()
        return {"outcome": "success", "reason": None}

    def observe(sender: Notifications, current: dict[str, Any], **kwargs: Any) -> None:
        if sender not in senders:
            senders.append(sender)
        job = current.get("ticket_jobs", {}).get("3", {})
        if (job.get("blocked_reason") == blocked_reason
                and (current.get("active_ticket_job") or {}).get("ticket_number") == 2):
            record("noncurrent", {"status": current["status"], "selected": 2, "blocked": 3})
        original_observe(sender, current, **kwargs)

    def drain(sender: Notifications) -> None:
        if sender.thread is not None:
            with sender.condition:
                assert sender.condition.wait_for(lambda: not sender.document["pending"], timeout=10)

    def close(sender: Notifications) -> None:
        try:
            drain(sender)
        finally:
            original_close(sender)
            assert sender.thread is None or not sender.thread.is_alive()

    monkeypatch.setattr("agent_run.notifications.send", send)
    monkeypatch.setattr(Notifications, "observe", observe)
    monkeypatch.setattr(Notifications, "close", close)

    def before_develop(number: int) -> None:
        record("developments", number)
        if number == 3:
            if records("developments").count(3) == 1:
                assert sibling_started.wait(10)
                if execution_failure:
                    raise GitError(original_evidence)
            return
        assert number == 2, "Dependency waiting must not dispatch Ticket 4"
        sibling_started.set()
        assert blocker_sent.wait(10), "Non-current Ticket 3 was never notified"
        assert records("noncurrent"), "The regression requires Ticket 2 to remain selected"
        sender = senders[0]
        drain(sender)
        current = load_only_run_state(git_repo)
        assert current["ticket_jobs"]["2"]["phase"] == "developing"
        assert current["ticket_jobs"]["3"]["phase"] == "blocked"
        assert current["status"] not in {"ready_for_human", "operator_stopped", "execution_failed"}
        before = records("cards")
        journal = read_notifications(managed_state(git_repo), current["run_id"])
        run_path = managed_state(git_repo) / "runs" / f"{current['run_id']}.json"
        run_before = run_path.read_bytes()
        for _ in range(3):
            sender.observe(current)
        for command in ("status", "history"):
            result = run_cli(git_repo, fixture, command, current["run_id"], "--json")
            assert result.returncode == 0, result.stdout + result.stderr
            record("queries", stdout_json(result))
        assert run_path.read_bytes() == run_before
        assert read_notifications(managed_state(git_repo), current["run_id"]) == journal
        assert records("cards") == before

    agents = TicketAgents(git_repo, (2, 3, 4), before_develop)
    agents.backends[3].data["developments"] = [human_blocker_step("developer-3")]
    monkeypatch.setattr(cli, "FixtureAgentBackend", lambda _path: agents)

    try:
        started = invoke_cli_inprocess(
            git_repo, fixture, "run", "1", "--notification-mode", "concise",
            "--agent-fixture", str(git_repo / "ticket-2-agents.json"), "--json",
        )
        assert started.returncode == 2, started.stdout + started.stderr
        current = load_only_run_state(git_repo)
        run_id = current["run_id"]
        assert records("queries") and records("noncurrent"), started.stdout + started.stderr
        for query in records("queries"):
            actions = query["operator_actions"]
            assert len(actions) == 1, actions
            assert actions[0]["work_subject"] == "ticket:3"
            assert actions[0]["scope"] == "local"
            assert actions[0]["reason"] == blocked_reason
            assert actions[0]["generation"] == 1
            assert actions[0]["action"]["ticket_number"] == 3
            assert "--ticket 3" in actions[0]["next_action"]
            assert original_evidence in str(actions[0]["evidence"])
            assert actions[0]["reasons"] == [original_evidence]
        assert current["ticket_jobs"]["2"]["phase"] == "completed"
        assert current["ticket_jobs"]["3"]["blocked_reason"] == blocked_reason
        assert "4" not in current["ticket_jobs"]
        assert sorted(records("developments")) == [2, 3]
        assert len(blocker_cards()) == 1
        assert "--ticket 3" in json.dumps(blocker_cards()[0])
        local_slot = next(event["id"] for event in events(current)
                          if event.get("current") and event.get("work_subject") == "ticket:3")
        assert read_notifications(managed_state(git_repo), run_id)["occurrences"][local_slot] == 1
        if execution_failure:
            assert current["ticket_jobs"]["3"]["execution_failure"] == original_evidence
            # Resolve the failing execution through the advertised target, and
            # preserve the sibling's completed work. The next Agent boundary is
            # deliberately a distinct human request.
            ticket_two = deepcopy(current["ticket_jobs"]["2"])
            resumed = invoke_cli_inprocess(
                git_repo, fixture, "resume", run_id, "--ticket", "3",
                "--agent-fixture", str(git_repo / "ticket-3-agents.json"), "--json",
            )
            assert resumed.returncode == 2, resumed.stdout + resumed.stderr
            after = load_only_run_state(git_repo)
            assert after["ticket_jobs"]["2"] == ticket_two
            assert records("developments").count(2) == 1
            assert records("developments").count(3) == 2
            assert after["ticket_jobs"]["3"]["blocked_reason"] == "agent_requires_human"
            boundary = next(event for event in events(after) if event.get("work_subject") == "ticket:3")
            assert original_evidence not in str(boundary["evidence"])
            return

        stopped = invoke_cli_inprocess(git_repo, fixture, "stop", run_id, "--json")
        assert stopped.returncode == 0, stopped.stdout + stopped.stderr
        paused = load_only_run_state(git_repo)
        assert paused["status"] == "operator_stopped"
        assert paused["ticket_jobs"]["3"]["blocked_reason"] == "agent_requires_human"
        for command in ("status", "history"):
            result = invoke_cli_inprocess(git_repo, fixture, command, run_id, "--json")
            assert result.returncode == 0, result.stdout + result.stderr
            record("queries", stdout_json(result))
            actions = stdout_json(result)["operator_actions"]
            assert {(action["work_subject"], action["scope"]) for action in actions} == {
                ("ticket:3", "local"), (f"run:{run_id}", "global"),
            }
        assert len(blocker_cards()) == 1, "A global Stop must not repeat the continuing local blocker"
        stopped_cards = [card for card in records("cards")
                         if card["header"]["title"]["content"] == text("notification.event.stopped", language="zh")]
        assert len(stopped_cards) == 1
        assert stopped_cards[0]["header"]["subtitle"]["content"].endswith("#1")

        ticket_two = deepcopy(paused["ticket_jobs"]["2"])
        ticket_three_attempt = paused["ticket_agent_invocations"]["3"]["semantic_attempt"]
        global_resume = invoke_cli_inprocess(
            git_repo, fixture, "resume", run_id,
            "--agent-fixture", str(git_repo / "ticket-3-agents.json"), "--json",
        )
        assert global_resume.returncode == 2, global_resume.stdout + global_resume.stderr
        assert load_only_run_state(git_repo)["status"] == "progress_exhausted"
        assert sorted(records("developments")) == [2, 3]
        # Explicit Resume reports its actual outcome separately from polling.
        resume_occurrence = read_notifications(managed_state(git_repo), run_id)["occurrences"][local_slot]
        agents.backends[3].data["developments"] = [{
            **human_blocker_step("developer-3"), "expected_thread_id": "developer-3",
        }]
        resumed = invoke_cli_inprocess(
            git_repo, fixture, "resume", run_id, "--ticket", "3", "--message", "Access restored.",
            "--agent-fixture", str(git_repo / "ticket-3-agents.json"), "--json",
        )
        assert resumed.returncode == 2, resumed.stdout + resumed.stderr
        after = load_only_run_state(git_repo)
        assert after["ticket_jobs"]["2"] == ticket_two
        assert records("developments").count(2) == 1
        assert records("developments").count(3) == 2, resumed.stdout + resumed.stderr
        assert after["ticket_jobs"]["3"]["blocked_reason"] == "agent_requires_human"
        assert after["ticket_agent_invocations"]["3"]["semantic_attempt"] == ticket_three_attempt
        assert read_notifications(managed_state(git_repo), run_id)["occurrences"][local_slot] > resume_occurrence
        assert len(blocker_cards()) >= 2, "A resolved then repeated blocker needs a new notification"
        assert not read_notifications(managed_state(git_repo), run_id)["pending"]
    finally:
        sibling_started.set()
        blocker_sent.set()
        for sender in senders:
            original_close(sender)
        assert all(sender.thread is None or not sender.thread.is_alive() for sender in senders)
