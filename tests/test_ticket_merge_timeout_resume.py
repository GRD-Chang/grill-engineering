"""An unobserved merge retains exclusive integration ownership through Resume."""
from copy import deepcopy
import json
from pathlib import Path
from typing import Any

import pytest

from agent_run import external_supervision, ticket_supervision
from agent_run.git import GitRepository
from agent_run.state import StateStore
from agent_run.state_contract import require_current_run_state
from support.cli import load_only_run_state, run_cli, stdout_json
from support.cli_payloads import passing_acceptance, publication
from support.seeding import seed_run, write_fixture
from support.ticket_parallel import TicketAgents, ticket_engine
from support.workspace import managed_repo, managed_state


def test_merge_observation_timeout_retains_seat_until_public_resume(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [100.0]
    monkeypatch.setattr(external_supervision, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        ticket_supervision, "ExternalSupervisor",
        lambda: external_supervision.ExternalSupervisor(now=lambda: clock[0]),
    )
    fixture = write_fixture(git_repo / "github.json", issues={
        str(number): {
            "number": number, "title": f"Deliver ticket {number}",
            "body": f"Implement ticket {number}.", "state": "OPEN",
            "labels": ["ready-for-agent"],
            "blocked_by": [{"number": 2, "state": "OPEN"}] if number == 4 else [],
        }
        for number in (2, 3, 4)
    })
    run_id = stdout_json(seed_run(git_repo, fixture, "1", idle_control=True))["run_id"]
    states = StateStore(managed_state(git_repo))
    git = GitRepository(managed_repo(git_repo))
    developed: list[int] = []
    agents = TicketAgents(git_repo, (2, 3, 4), developed.append)
    engine = ticket_engine(git, states, fixture, agents)
    original_sync = engine.github.sync_run_branch
    observations = []

    def unavailable(**kwargs: Any) -> None:
        observations.append(kwargs)
        # Keep the fixture's remote branch synchronized with its completed
        # merge, then lose the response before durable local confirmation.
        original_sync(**kwargs)
        raise OSError("merge observation unavailable")

    monkeypatch.setattr(engine.github, "sync_run_branch", unavailable)
    waiting = engine.deliver(run_id)
    before = deepcopy(waiting["ticket_jobs"]["2"])
    assert before["phase"] == "merging"
    assert waiting["ticket_integration_holder"] == 2
    deadline = before["external_wait"]["supervision_window"]["deadline"]
    clock[0] += 61
    engine.supervise(run_id, frozenset())
    assert len(observations) >= 2
    clock[0] = deadline + 0.1
    engine.supervise(run_id, frozenset())
    paused = states.load_current_run(run_id)
    assert paused is not None
    job = paused["ticket_jobs"]["2"]
    assert job["phase"] == "blocked"
    assert job["blocked_reason"] == "ticket_supervision_timeout"
    assert job["execution_resume_phase"] == "merging"
    assert paused["ticket_integration_holder"] == 2
    assert job["merge_intent"] == before["merge_intent"]
    require_current_run_state(paused)

    ticket_engine(git, states, fixture, agents, development_only=True).deliver_ticket(run_id, 3)
    queued = engine.deliver_ticket(run_id, 3)
    assert developed == [2, 3]
    assert queued["ticket_jobs"]["3"]["phase"] == "ready_for_integration"
    assert queued["ticket_integration_holder"] == 2
    assert "4" not in queued["ticket_jobs"]
    remote = json.loads(fixture.read_text())["delivery"]
    assert len(remote["pull_requests"]) == 1
    assert remote["pull_requests"][0]["state"] == "MERGED"
    assert not remote.get("closed_issues")
    require_current_run_state(queued)

    monkeypatch.setattr(engine.github, "sync_run_branch", original_sync)
    payloads = [
        json.loads((git_repo / f"ticket-{number}-agents.json").read_text())
        for number in (3, 4)
    ]
    recovery = git_repo / "resume-agents.json"
    recovery.write_text(json.dumps({
        "developments": payloads[1]["developments"],
        "reviews": [item for payload in payloads for item in payload["reviews"]],
        "publications": [item for payload in payloads for item in payload["publications"]],
        "run_reviews": [passing_acceptance("run-reviewer", "All tickets passed.")],
        "run_publications": [publication()],
    }))
    resumed = run_cli(
        git_repo, fixture, "resume", run_id, "--ticket", "2",
        "--agent-fixture", str(recovery),
    )
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    final = load_only_run_state(git_repo)
    assert final["status"] == "run_approval_pending"
    recovered = final["ticket_jobs"]["2"]
    for key in ("candidate_sha", "publication_sha", "review_budget", "merge_intent"):
        assert recovered[key] == before[key]
    assert all(final["ticket_jobs"][str(number)]["phase"] == "completed" for number in (2, 3, 4))
    assert final["ticket_integration_holder"] is None
    remote = json.loads(fixture.read_text())["delivery"]
    assert sorted(remote["closed_issues"]) == [2, 3, 4]
    assert len(remote["pull_requests"]) == 4
    for action in ("completion_comment", "close_issue", "delete_managed_branch"):
        assert sum(mutation["action"] == action for mutation in remote["mutations"]) == 3
    require_current_run_state(final)
    snapshot = fixture.read_bytes()
    repeated = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(recovery))
    assert stdout_json(repeated)["status"] == "run_approval_pending"
    assert fixture.read_bytes() == snapshot
