"""Exercise real Parent close reconciliation and both persisted delivery callers."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from agent_run.git import GitRepository
from agent_run.github import GitHubReadError
from agent_run.github_publish import GhGitHubPublisher


class ParentRemote:
    def __init__(self, git: GitRepository, monkeypatch: pytest.MonkeyPatch):
        self.state = "OPEN"
        self.comments = []
        self.events = []
        self.writes = []
        self.actor = "delivery[bot]"
        self.event_reads = 0
        self.lose_response = True
        self.lose_comment_response = False
        self.hide_comments = False
        self.publisher = GhGitHubPublisher("example/project", git)
        monkeypatch.setattr(self.publisher, "_publisher_login", lambda: "delivery[bot]")
        monkeypatch.setattr(self.publisher, "_json", self.read)
        monkeypatch.setattr(self.publisher, "_require", self.write)

    def read(self, *arguments):
        if arguments[:2] == ("issue", "view"):
            return {"state": self.state, "comments": [] if self.hide_comments else deepcopy(self.comments)}
        if arguments[1].endswith("events"):
            self.event_reads += 1
            return [deepcopy(self.events)]
        raise AssertionError(arguments)

    def write(self, *arguments):
        self.writes.append(arguments[1])
        if arguments[1] == "comment":
            self.comments.append({"body": arguments[-1], "author": {"login": "delivery[bot]"},
                                  "createdAt": "2026-09-28T10:00:00Z"})
            if self.lose_comment_response:
                raise OSError("lost comment response")
        else:
            assert arguments[1] == "close"
            self.state = "CLOSED"
            if self.actor:
                self.events.append(self.event(self.actor))
            if self.lose_response:
                raise OSError("lost close response")

    @staticmethod
    def event(actor):
        return {"id": 11, "event": "closed", "actor": {"login": actor},
                "created_at": "2026-09-28T10:00:01Z"}


@pytest.mark.parametrize("delivery_type", ["Parent-only", "Final Run"])
@pytest.mark.parametrize("actor", ["delivery[bot]", "maintainer", None])
def test_parent_unknown_close_requires_intent_and_matching_event(tmp_path, monkeypatch, delivery_type, actor):
    remote = ParentRemote(GitRepository(tmp_path), monkeypatch)
    remote.actor = actor
    authority = dict(parent_number=1, run_id="run-1", pr_number=2, integrated_sha="abc",
                     delivery_type=delivery_type)
    journal = []
    with pytest.raises(OSError, match="lost close response"):
        remote.publisher.close_parent_issue(**authority, record_intent=lambda value: journal.append(deepcopy(value)))
    assert journal[0]["dispatch_attempted"] is False
    assert journal[-1]["dispatch_attempted"] is True
    assert journal[-1]["integrated_sha"] == "abc"
    assert journal[-1]["actor"] == "delivery[bot]"
    assert journal[-1]["baseline_event_id"] == 0

    def recover():
        remote.publisher.close_parent_issue(**authority, close_intent=deepcopy(journal[-1]),
                                             record_intent=lambda value: journal.append(deepcopy(value)))
    if actor == "delivery[bot]":
        recover()
        assert journal[-1]["event_id"] == 11
    else:
        with pytest.raises(GitHubReadError) as caught:
            recover()
        assert caught.value.code == ("parent_close_external_conflict" if actor else "github_write_outcome_unknown")
        if actor is None:
            # Delayed events are reconciled without a second close or comment.
            remote.events.append(remote.event("delivery[bot]"))
            recover()
    assert remote.event_reads > 0
    assert remote.writes == ["comment", "close"]


def test_open_readback_after_unknown_close_never_replays(tmp_path, monkeypatch):
    remote = ParentRemote(GitRepository(tmp_path), monkeypatch)
    remote.actor = None
    authority = dict(parent_number=1, run_id="run-1", pr_number=2, integrated_sha="abc", delivery_type="Final Run")
    intent = {}
    with pytest.raises(OSError):
        remote.publisher.close_parent_issue(**authority, close_intent=intent)
    remote.state = "OPEN"
    with pytest.raises(GitHubReadError, match="reconcile before dispatching again"):
        remote.publisher.close_parent_issue(**authority, close_intent=intent)
    assert remote.writes == ["comment", "close"]


@pytest.mark.parametrize("mutation", ["actor", "sha", "old-event", "reopen"])
def test_parent_recovery_rejects_changed_binding_or_transition(tmp_path, monkeypatch, mutation):
    remote = ParentRemote(GitRepository(tmp_path), monkeypatch)
    authority = dict(parent_number=1, run_id="run-1", pr_number=2, integrated_sha="abc", delivery_type="Final Run")
    intent = {}
    with pytest.raises(OSError):
        remote.publisher.close_parent_issue(**authority, close_intent=intent)
    if mutation == "actor":
        intent["actor"] = "another[bot]"
    elif mutation == "sha":
        authority["integrated_sha"] = "different"
    elif mutation == "old-event":
        remote.events[0]["created_at"] = "2026-09-28T09:59:59Z"
    else:
        remote.events.extend([{**remote.event("maintainer"), "id": 12, "event": "reopened"},
                              {**remote.event("delivery[bot]"), "id": 13}])
    with pytest.raises(GitHubReadError) as caught:
        remote.publisher.close_parent_issue(**authority, close_intent=intent)
    assert caught.value.code == "parent_close_external_conflict"
    assert remote.writes == ["comment", "close"]


def delivery_with_parent_remote(git_repo, monkeypatch, parent_only):
    from agent_run.controller import Controller
    from agent_run.github_fixture import FixtureGitHubReader, FixtureGitHubPublisher
    from agent_run.parent_delivery import ParentDeliveryEngine
    from agent_run.run_publication import RunPublicationEngine
    from agent_run.state import StateStore
    from support.delivery_agents import ScriptedAgents
    from support.run_publication import RunPublicationAgents
    from support.run_scenarios import _accepted_run
    from support.seeding import write_fixture

    if parent_only:
        fixture = write_fixture(git_repo / "github.json", issues={})
        git, states = GitRepository(git_repo), StateStore(git_repo / ".agent-run")
        state, _ = Controller(FixtureGitHubReader(fixture), git, states).start(1)
        publisher = FixtureGitHubPublisher(fixture, git)
        engine = ParentDeliveryEngine(git=git, states=states, github=publisher,
                                       agents=ScriptedAgents(states.root / "worktrees" / state["run_id"] / "parent"))
        assert engine.deliver(state["run_id"])["status"] == "parent_approval_pending"
        subject = "parent_job"
    else:
        state, states, git, publisher = _accepted_run(git_repo)
        engine = RunPublicationEngine(git=git, states=states, github=publisher, agents=RunPublicationAgents(),
                                      default_branch="main", default_head_sha=git.resolve("main"))
        assert engine.publish(state["run_id"])["status"] == "run_approval_pending"
        subject = "run_publication"
    remote = ParentRemote(git, monkeypatch)
    monkeypatch.setattr(publisher, "close_parent_issue", remote.publisher.close_parent_issue)
    return engine, states, state["run_id"], subject, remote


@pytest.mark.parametrize("parent_only", [True, False])
@pytest.mark.parametrize("actor", ["delivery[bot]", "maintainer", None])
def test_delivery_callers_persist_parent_intent_and_never_complete_foreign_close(git_repo, monkeypatch, parent_only, actor):
    engine, states, run_id, subject, remote = delivery_with_parent_remote(git_repo, monkeypatch, parent_only)
    remote.actor = actor
    with pytest.raises(OSError, match="lost close response"):
        engine.approve(run_id)
    interrupted = states.load_current_run(run_id)
    assert interrupted["status"] != "completed"
    assert interrupted[subject]["parent_close_intent"]["dispatch_attempted"] is True
    assert not interrupted[subject].get("parent_closed")
    original_grant = deepcopy(interrupted[subject]["approval_grant"])
    if actor == "delivery[bot]":
        completed = engine.recover_closeout(run_id)
        assert completed["status"] == "completed"
        assert completed[subject]["parent_close_intent"]["event_id"] == 11
        assert completed[subject]["approval_grant"] == original_grant
    else:
        if parent_only and actor is None:
            assert engine.recover_closeout(run_id)["status"] == "waiting_external"
        else:
            with pytest.raises(GitHubReadError) as caught:
                engine.recover_closeout(run_id)
            assert caught.value.code == ("parent_close_external_conflict" if actor else "github_write_outcome_unknown")
        pending = states.load_current_run(run_id)
        assert pending["status"] != "completed"
        assert not pending[subject].get("parent_closed")
        assert pending[subject]["approval_grant"] == original_grant
        if actor is None:
            remote.events.append(remote.event("delivery[bot]"))
            assert engine.recover_closeout(run_id)["status"] == "completed"
    assert remote.writes == ["comment", "close"]


def test_parent_comment_response_loss_resumes_from_saved_intent(tmp_path, monkeypatch):
    remote = ParentRemote(GitRepository(tmp_path), monkeypatch)
    remote.lose_response = False
    authority = dict(parent_number=1, run_id="run-1", pr_number=2, integrated_sha="abc", delivery_type="Final Run")
    intent = {}

    def lost_comment(*arguments):
        remote.write(*arguments)
        if arguments[1] == "comment":
            raise OSError("lost comment response")

    monkeypatch.setattr(remote.publisher, "_require", lost_comment)
    with pytest.raises(OSError, match="lost comment response"):
        remote.publisher.close_parent_issue(**authority, close_intent=intent)
    assert intent["dispatch_attempted"] is False
    remote.publisher.close_parent_issue(**authority, close_intent=intent)
    assert intent["event_id"] == 11
    assert remote.writes == ["comment", "close"]


def test_parent_historical_events_are_not_the_new_close(tmp_path, monkeypatch):
    remote = ParentRemote(GitRepository(tmp_path), monkeypatch)
    remote.events = [{**remote.event("delivery[bot]"), "id": 1},
                     {**remote.event("maintainer"), "id": 2, "event": "reopened"}]
    intent = {}
    authority = dict(parent_number=1, run_id="run-1", pr_number=2, integrated_sha="abc", delivery_type="Final Run")
    with pytest.raises(OSError):
        remote.publisher.close_parent_issue(**authority, close_intent=intent)
    assert intent["baseline_event_id"] == 2
    remote.publisher.close_parent_issue(**authority, close_intent=intent)
    assert intent["event_id"] == 11
    # Remote comments cannot reconstruct proof of a local close dispatch.
    with pytest.raises(GitHubReadError, match="persisted close intent"):
        remote.publisher.close_parent_issue(**authority)
    assert remote.writes == ["comment", "close"]


@pytest.mark.parametrize("failure_at", [1, 2, 3])
def test_parent_intent_must_persist_before_each_write(tmp_path, monkeypatch, failure_at):
    remote = ParentRemote(GitRepository(tmp_path), monkeypatch)
    saved = []

    def save(intent):
        saved.append(intent)
        if len(saved) == failure_at:
            raise OSError("state save failed")

    with pytest.raises(OSError, match="state save failed"):
        remote.publisher.close_parent_issue(parent_number=1, run_id="run-1", pr_number=2,
            integrated_sha="abc", delivery_type="Final Run", record_intent=save)
    assert remote.writes == ([] if failure_at < 3 else ["comment"])
    assert remote.state == "OPEN"


@pytest.mark.parametrize("legacy", [False, True])
def test_unknown_comment_waits_for_visibility_without_replay(tmp_path, monkeypatch, legacy):
    remote = ParentRemote(GitRepository(tmp_path), monkeypatch)
    remote.lose_comment_response = True
    remote.lose_response = False
    authority = dict(parent_number=1, run_id="run-1", pr_number=2, integrated_sha="abc", delivery_type="Final Run")
    intent = {}
    with pytest.raises(OSError, match="lost comment response"):
        remote.publisher.close_parent_issue(**authority, close_intent=intent)
    assert intent["comment_dispatch_attempted"] is True
    assert intent["dispatch_attempted"] is False
    if legacy:
        del intent["comment_dispatch_attempted"]
    remote.hide_comments = True
    for _ in range(2):
        with pytest.raises(GitHubReadError) as caught:
            remote.publisher.close_parent_issue(**authority, close_intent=intent)
        assert caught.value.code == "github_write_outcome_unknown"
        assert remote.writes == ["comment"]
    remote.hide_comments = False
    remote.publisher.close_parent_issue(**authority, close_intent=intent)
    assert intent["dispatch_attempted"] is True
    assert intent["event_id"] == 11
    assert remote.writes == ["comment", "close"]
    assert len(remote.comments) == 1


@pytest.mark.parametrize("parent_only", [True, False])
@pytest.mark.parametrize("external_close", [True, False])
def test_delivery_comment_loss_recovers_without_claiming_undispatched_close(
    git_repo, monkeypatch, parent_only, external_close,
):
    engine, states, run_id, subject, remote = delivery_with_parent_remote(git_repo, monkeypatch, parent_only)
    remote.lose_comment_response = True
    remote.lose_response = False
    with pytest.raises(OSError, match="lost comment response"):
        engine.approve(run_id)
    interrupted = states.load_current_run(run_id)
    intent = interrupted[subject]["parent_close_intent"]
    assert intent["comment_dispatch_attempted"] is True
    assert intent["dispatch_attempted"] is False
    grant = deepcopy(interrupted[subject]["approval_grant"])
    if external_close:
        # Another Worker using the same App closes the Issue. The Publisher
        # never sent close, despite matching actor and a later event timestamp.
        remote.state = "CLOSED"
        remote.events.append(remote.event("delivery[bot]"))
        with pytest.raises(GitHubReadError) as caught:
            engine.recover_closeout(run_id)
        assert caught.value.code == "parent_close_external_conflict"
    else:
        remote.hide_comments = True
        for _ in range(2):
            if parent_only:
                assert engine.recover_closeout(run_id)["status"] == "waiting_external"
            else:
                with pytest.raises(GitHubReadError) as caught:
                    engine.recover_closeout(run_id)
                assert caught.value.code == "github_write_outcome_unknown"
            assert remote.writes == ["comment"]
    pending = states.load_current_run(run_id)
    assert pending["status"] != "completed"
    assert not pending[subject].get("parent_closed")
    assert pending[subject]["parent_close_intent"]["dispatch_attempted"] is False
    assert "event_id" not in pending[subject]["parent_close_intent"]
    assert pending[subject]["approval_grant"] == grant
    assert remote.writes == ["comment"]
    if not external_close:
        remote.hide_comments = False
        completed = engine.recover_closeout(run_id)
        assert completed["status"] == "completed"
        assert completed[subject]["parent_close_intent"]["dispatch_attempted"] is True
        assert completed[subject]["parent_close_intent"]["event_id"] == 11
        assert completed[subject]["approval_grant"] == grant
        assert remote.writes == ["comment", "close"]
    assert len(remote.comments) == 1
