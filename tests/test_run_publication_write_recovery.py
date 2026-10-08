"""Final Run writes reconcile durable intent against exact remote facts."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agent_run.github_fixture import FixtureGitHubPublisher
from agent_run.run_publication import RunPublicationEngine
from support.run_publication import RunPublicationAgents
from support.run_scenarios import _accepted_run


class InterruptedPublisher(FixtureGitHubPublisher):
    def __init__(self, *args: Any, action: str, applied: bool) -> None:
        super().__init__(*args)
        self.action = action
        self.applied = applied
        self.interrupt = True
        self.calls = {"ref": 0, "create": 0, "update": 0}

    def _write(self, action: str, callback: Any) -> Any:
        self.calls[action] += 1
        if self.action == action and self.interrupt:
            self.interrupt = False
            if self.applied:
                callback()
            raise OSError(f"{action} response lost")
        return callback()

    def ensure_final_run_ref(self, **authority: Any) -> None:
        self._write("ref", lambda: super(InterruptedPublisher, self).ensure_final_run_ref(**authority))

    def publish_branch(self, *args: Any, **authority: Any) -> Any:
        return self._write("ref", lambda: super(InterruptedPublisher, self).publish_branch(*args, **authority))

    def ensure_run_pr(self, **authority: Any) -> int:
        return self._write("create", lambda: super(InterruptedPublisher, self).ensure_run_pr(**authority))

    def refresh_run_pr_narrative(self, **authority: Any) -> None:
        self._write("update", lambda: super(InterruptedPublisher, self).refresh_run_pr_narrative(**authority))


def _prepare(git_repo: Path, action: str, applied: bool) -> tuple[Any, ...]:
    state, states, git, publisher = _accepted_run(git_repo)
    agents = RunPublicationAgents()
    run_id = str(state["run_id"])
    if action == "update":
        initial = RunPublicationEngine(
            git=git, states=states, agents=agents, github=publisher,
            default_branch="main", default_head_sha=git.resolve("main"),
        ).publish(run_id)
        initial["run_publication"].pop("record")
        initial["run_publication"]["phase"] = "waiting_external"
        initial["status"] = "waiting_external"
        initial["terminal_kind"] = "waiting_external"
        publisher.data["delivery"]["pull_requests"][0]["title"] = "Previous publication title"
        publisher._save()
        states.save_run(run_id, initial)
    interrupted = InterruptedPublisher(git_repo / "github.json", git, action=action, applied=applied)
    engine = RunPublicationEngine(
        git=git, states=states, agents=agents, github=interrupted,
        default_branch="main", default_head_sha=git.resolve("main"),
    )
    return run_id, states, agents, interrupted, engine


@pytest.mark.parametrize("action", ["ref", "create", "update"])
@pytest.mark.parametrize("applied", [False, True], ids=["not-applied", "applied"])
def test_final_write_response_loss_reconciles_before_continuing(
    git_repo: Path, action: str, applied: bool,
) -> None:
    run_id, states, agents, publisher, engine = _prepare(git_repo, action, applied)
    waiting = engine.publish(run_id)
    intent = waiting["run_publication"].get("write_intent")
    assert isinstance(intent, dict)
    assert waiting["status"] == "waiting_external"
    assert states.load_current_run(run_id)["run_publication"]["write_intent"] == intent
    assert publisher.calls[action] == 1
    resumed = engine.publish(run_id)
    assert resumed["status"] == "run_approval_pending"
    assert publisher.calls[action] == (1 if applied or action == "ref" else 2)
    assert len(agents.requests) == 1
    again = engine.publish(run_id)
    assert again["status"] == "run_approval_pending"
    assert publisher.calls[action] == (1 if applied or action == "ref" else 2)


@pytest.mark.parametrize("action", ["ref", "create", "update"])
def test_final_write_success_before_state_save_failure_keeps_durable_intent(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, action: str,
) -> None:
    run_id, states, agents, publisher, engine = _prepare(git_repo, action, True)
    publisher.interrupt = False
    original_save = states.save_run
    failed = False

    def interrupted_save(identifier: str, state: dict[str, Any]) -> None:
        nonlocal failed
        durable = states.load_current_run(identifier)
        intent = durable.get("run_publication", {}).get("write_intent")
        if (
            not failed and publisher.calls[action] == 1
            and isinstance(intent, dict)
            and state["run_publication"].get("write_intent") is None
        ):
            failed = True
            raise OSError("StateStore unavailable after successful remote write")
        original_save(identifier, state)

    monkeypatch.setattr(states, "save_run", interrupted_save)
    waiting = engine.publish(run_id)
    assert failed
    assert isinstance(waiting["run_publication"].get("write_intent"), dict)
    resumed = engine.publish(run_id)
    assert resumed["status"] == "run_approval_pending"
    assert publisher.calls[action] == 1
    assert len(agents.requests) == 1


def test_foreign_narrative_after_unknown_update_is_bounded_and_never_replayed(git_repo: Path) -> None:
    run_id, _states, agents, publisher, engine = _prepare(git_repo, "update", True)
    engine.publish(run_id)
    publisher.data["delivery"]["pull_requests"][0]["title"] = "External replacement"
    publisher._save()
    for _ in range(6):
        result = engine.publish(run_id)
    assert result["status"] == "publication_pending"
    assert isinstance(result["run_publication"].get("write_intent"), dict)
    assert publisher.calls["update"] == 1
    assert len(agents.requests) == 1


@pytest.mark.parametrize("terminal_state", ["CLOSED", "MERGED"])
def test_closed_pr_after_lost_create_response_never_creates_replacement(
    git_repo: Path, terminal_state: str,
) -> None:
    run_id, states, agents, publisher, engine = _prepare(git_repo, "create", True)
    waiting = engine.publish(run_id)
    intent = waiting["run_publication"]["write_intent"]
    pull = publisher.data["delivery"]["pull_requests"][0]
    pull["state"] = terminal_state
    publisher._save()

    for _ in range(6):
        engine.publish(run_id)

    durable = states.load_current_run(run_id)
    assert durable["status"] == "ready_for_human"
    assert terminal_state in durable["diagnostics"][0]["message"]
    assert durable["run_publication"]["write_intent"] == intent
    assert publisher.calls["create"] == 1
    assert len(publisher.data["delivery"]["pull_requests"]) == 1
    assert len(agents.requests) == 1


def test_explicit_final_publication_resume_keeps_intent_and_artifact(git_repo: Path) -> None:
    from copy import deepcopy
    from agent_run.controller import apply_resume_to_state
    from agent_run.delivery_policy import DeliveryPolicy
    from agent_run.guidance_actions import NextAction, next_action_fact
    from agent_run.resume_intent import bind_resume_intent

    run_id, states, agents, publisher, engine = _prepare(git_repo, "update", True)
    engine.publish(run_id)
    publisher.data["delivery"]["pull_requests"][0]["title"] = "External replacement"
    publisher._save()
    for _ in range(6):
        paused = engine.publish(run_id)
    original = deepcopy(paused["run_publication"])
    intent = bind_resume_intent(paused)
    assert intent["authorization"] == "renew_publication_operation_retry"
    assert next_action_fact(paused) == NextAction("command", "resume", run_id)
    apply_resume_to_state(
        paused, explicit_resume=True, resume_human_blocker=False, new_thread=False,
        human_response=None, resume_budget_checkpoint=False,
        budget_policy=DeliveryPolicy(), ticket_number=None, source_status="publication_pending",
    )
    subject = paused["run_publication"]
    assert subject["write_intent"] == original["write_intent"]
    assert subject["artifact"] == original["artifact"]
    assert subject["publication_operation_retry_history"] == [original["publication_operation_retry"]]
    states.save_run(run_id, paused)
    # Visibility converges to the intended title without another Runner edit.
    publisher.data["delivery"]["pull_requests"][0]["title"] = subject["artifact"]["pr_title"]
    publisher._save()
    assert engine.publish(run_id)["status"] == "run_approval_pending"
    assert publisher.calls["update"] == 1
    assert len(agents.requests) == 1


def test_public_resume_reconciles_reopened_pr_then_checks_and_requires_approval(
    git_repo: Path,
) -> None:
    from copy import deepcopy
    from agent_run.controller import Controller
    from agent_run.github_fixture import FixtureGitHubReader

    run_id, states, agents, publisher, engine = _prepare(git_repo, "create", True)
    waiting = engine.publish(run_id)
    original = deepcopy(waiting["run_publication"])
    pull = publisher.data["delivery"]["pull_requests"][0]
    pull["state"] = "CLOSED"
    publisher._save()
    paused = engine.publish(run_id)
    assert paused["status"] == "ready_for_human"

    controller = Controller(FixtureGitHubReader(git_repo / "github.json"), engine.git, states)
    controller.resume(run_id, explicit_resume=True)
    still_closed = engine.publish(run_id)
    assert still_closed["status"] == "ready_for_human"
    assert still_closed["run_publication"]["write_intent"] == original["write_intent"]

    pull["state"] = "OPEN"
    publisher.data["delivery"].update(required_checks=["pending", "pass"], check_position=0)
    publisher._save()
    resumed, _ = controller.resume(run_id, explicit_resume=True)
    assert resumed["run_publication"]["write_intent"] == original["write_intent"]
    assert resumed["run_publication"]["artifact"] == original["artifact"]

    restarted = RunPublicationEngine(
        git=engine.git, states=states, agents=agents, github=publisher,
        default_branch="main", default_head_sha=engine.git.resolve("main"),
    )
    checking = restarted.publish(run_id)
    assert checking["status"] == "waiting_checks"
    assert checking["run_publication"]["pr_number"] == pull["number"]
    assert checking["run_publication"]["artifact"] == original["artifact"]
    approved_gate = restarted.publish(run_id)
    assert approved_gate["status"] == "run_approval_pending"
    assert "approval_grant" not in approved_gate["run_publication"]
    assert approved_gate["run_publication"]["record"]["run_head_sha"] == original["write_intent"]["authority"]["head_sha"]
    assert publisher.calls["create"] == 1
    assert len(agents.requests) == 1
    assert publisher.live_pull_request(int(pull["number"]))["state"] == "OPEN"


@pytest.mark.parametrize("location", ["parent", "run_repair"])
@pytest.mark.parametrize("phase", ["publishing", "merging", "merged"])
def test_change_publication_resume_retains_exact_stage(location: str, phase: str) -> None:
    from copy import deepcopy
    from agent_run.publication_resume import resume_parent_publication_retry

    subject = {
        "phase": "publication_pending", "execution_resume_phase": phase,
        "publication_sha": "a" * 40, "publication": {"pr_title": "Stored title"},
        "ticket_write_intent": {"action": "ensure_ticket_pr", "head_sha": "a" * 40},
        "merge_intent": {"head_sha": "a" * 40},
        "publication_operation_retry": {"attempts": 5, "limit": 5},
    }
    state = {"status": "publication_pending", "run_id": "r1"}
    if location == "parent":
        state["parent_job"] = subject
    else:
        state["run_acceptance"] = {"phase": "repairing", "repair_job": subject}
    before = deepcopy(subject)
    assert resume_parent_publication_retry(state)
    assert subject["phase"] == phase
    assert subject["ticket_write_intent"] == before["ticket_write_intent"]
    assert subject["merge_intent"] == before["merge_intent"]
    assert subject["publication"] == before["publication"]
    assert subject["publication_operation_retry_history"] == [{"attempts": 5, "limit": 5}]
    assert not resume_parent_publication_retry(state)


@pytest.mark.parametrize("payload", [{"title": "Exact title", "body": "Exact\nbody"}, {"title": "Missing body"}])
def test_real_narrative_snapshot_requires_complete_exact_text(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, payload: dict[str, str],
) -> None:
    from agent_run.git import GitRepository
    from agent_run.github import GitHubReadError
    from agent_run.github_publish import GhGitHubPublisher

    publisher = GhGitHubPublisher("example/project", GitRepository(git_repo))
    monkeypatch.setattr(publisher, "_json", lambda *args: payload)
    if "body" in payload:
        assert publisher.read_run_pr_narrative(1) == payload
    else:
        with pytest.raises(GitHubReadError, match="title and body"):
            publisher.read_run_pr_narrative(1)


def test_parent_only_published_retry_resume_continues_without_new_agents(git_repo: Path) -> None:
    from copy import deepcopy
    from agent_run.controller import Controller, apply_resume_to_state
    from agent_run.delivery_policy import DeliveryPolicy
    from agent_run.git import GitRepository
    from agent_run.github_fixture import FixtureGitHubReader
    from agent_run.parent_delivery import ParentDeliveryEngine
    from agent_run.resume_intent import bind_resume_intent
    from agent_run.state import StateStore
    from support.delivery_agents import PassAgents
    from support.seeding import write_fixture

    fixture = write_fixture(git_repo / "github.json", issues={})
    git = GitRepository(git_repo)
    states = StateStore(git_repo / ".agent-run")
    state, _ = Controller(FixtureGitHubReader(fixture), git, states).start(1)
    run_id = str(state["run_id"])
    agents = PassAgents(states.root / "worktrees" / run_id / "parent")
    publisher = FixtureGitHubPublisher(fixture, git)
    engine = ParentDeliveryEngine(git=git, states=states, github=publisher, agents=agents)
    published = engine.deliver(run_id)
    assert published["status"] == "parent_approval_pending"
    # Start from the actual published delivery rather than rerunning costly
    # setup for each failed read. The canonical failure transition owns the
    # exhausted retry checkpoint used by the public resume application.
    from agent_run.change_delivery import ChangeDeliveryEngine
    from types import SimpleNamespace
    stage = SimpleNamespace(contract=SimpleNamespace(label="parent-only"))
    paused = published
    paused["parent_job"]["phase"] = "publishing"
    for _ in range(5):
        ChangeDeliveryEngine._record_publication_operation_failure_in_memory(
            stage, paused, paused["parent_job"], OSError("publication read unavailable"),
        )
    states.save_run(run_id, paused)
    assert paused["status"] == "publication_pending"
    original = deepcopy(paused["parent_job"])
    calls = (len(agents.development_requests), len(agents.review_requests), len(agents.publication_requests))
    assert bind_resume_intent(paused)["authorization"] == "renew_publication_operation_retry"
    apply_resume_to_state(
        paused, explicit_resume=True, resume_human_blocker=False, new_thread=False,
        human_response=None, resume_budget_checkpoint=False,
        budget_policy=DeliveryPolicy(), ticket_number=None, source_status="publication_pending",
    )
    states.save_run(run_id, paused)
    resumed = engine.deliver(run_id)
    assert resumed["status"] == "parent_approval_pending"
    assert resumed["parent_job"]["publication"] == original["publication"]
    assert resumed["parent_job"]["publication_sha"] == original["publication_sha"]
    assert resumed["parent_job"]["pr_number"] == original["pr_number"]
    assert (len(agents.development_requests), len(agents.review_requests), len(agents.publication_requests)) == calls
