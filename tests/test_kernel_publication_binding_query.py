"""Publication semantics remain distinct from a reused Development binding."""
from io import StringIO
from pathlib import Path

import pytest

from agent_run.model import Issue, ParentIssue, Requirements
import agent_run.queries.run as query
from agent_run.model import Role
from support.offline import GraphScenario
from agent_run.cli.render import render
from support.notification import _project
from support.notification import recorded_agents as _recorded_agents
from support.environment import durable_snapshot as _snapshot


@pytest.mark.parametrize("kind", ["ticket", "parent_only"])
def test_publication_query_keeps_semantic_role_and_original_development_profile(tmp_path: Path, monkeypatch, kind):
    requirements = Requirements(ParentIssue(1, "Parent", "Original scope", (2,) if kind == "ticket" else ()),
        (Issue(2, "Ticket", "Original task", "OPEN", labels=("ready-for-agent",)),) if kind == "ticket" else ())
    scenario = GraphScenario(tmp_path, requirements)
    scenario.kernel.configure_profiles("initial-distinct-profiles", overrides={
        "development_model": "original-development", "development_effort": "high",
        "publication_model": "original-independent-publication", "publication_effort": "low"})
    original = scenario.agent
    calls = []
    observations = []
    now = [100.0]
    scenario.kernel.clock = lambda: now[0]

    def started(call):
        duration = {Role.DEVELOPMENT: 2, Role.REVIEW: 3, Role.PUBLICATION: 5}[call.role]
        scenario.kernel.observe_worker_activity(call, "turn-start", active=True, observed_at=now[0] + 1,
                                               evidence={"type": "turn.started"})
        scenario.kernel.observe_worker_activity(call, "turn-end", active=False, observed_at=now[0] + duration + 1,
                                               evidence={"type": "turn.completed"})
        now[0] += duration + 2

    def agent(call):
        calls.append(call)
        if call.role == Role.PUBLICATION:
            development = next(old for old in calls if old.role == Role.DEVELOPMENT)
            assert call.thread_id == development.thread_id and call.mode == "resume"
            assert call.profile_revision == development.profile_revision
            before = _snapshot(tmp_path)
            status = query.read(scenario.kernel.storage.path, "status", storage=scenario.kernel.storage)
            history = query.read(scenario.kernel.storage.path, "history", storage=scenario.kernel.storage).data
            settings = query.read(scenario.kernel.storage.path, "settings", storage=scenario.kernel.storage).data
            invocation = next(row for attempt in history["history"]["attempts"] for row in attempt["invocations"] if row["id"] == call.invocation_id)
            assert invocation["role"] == "publication" and invocation["binding_role"] == "development"
            assert invocation["thread"] == development.thread_id
            assert invocation["model"] == "original-development" and invocation["effort"] == "high"
            assert invocation["profile_reference"] is None
            assert invocation["profile_revision"] == development.profile_revision
            subject = next(row for row in status.data["subjects"] if row["subject"] == status.data["focus_subject"])
            pending = subject["pending_attempt"]
            current = next(row for row in pending["invocations"] if row["id"] == pending["current_invocation"])
            assert pending["role"] == "publication"
            assert current["model"] == "original-development"
            assert settings["profile"]["profiles"]["development"]["model"] == "future-development"
            binding = next(row for row in settings["bindings"] if row["thread_identity"] == development.thread_id)
            assert binding["role"] == "development" and binding["model"] == "original-development"
            for style in ("plain", "rich"):
                output = StringIO()
                render(status, style=style, details=False, stream=output)
                assert "发布 Agent" in output.getvalue() and "original-development" in output.getvalue()
            assert _snapshot(tmp_path) == before
            observations.append(invocation)
        raw = original(call)
        if call.role == Role.REVIEW:
            scenario.kernel.configure_profiles("future-profiles-reference-development", overrides={
                "development_model": "future-development", "development_effort": "low",
                "publication_from_development": True})
        return raw

    # Existing synthetic process-observation seam; this is a query consumer,
    # not an installed Worker/Host or external notification delivery claim.
    _recorded_agents(scenario, monkeypatch, agent, started, activity=False)
    assert scenario.run().kind == ("complete" if kind == "ticket" else "approval")
    assert [call.role for call in calls] == [Role.DEVELOPMENT, Role.REVIEW, Role.PUBLICATION]
    assert len(observations) == 1
    before = _snapshot(tmp_path)
    history = query.read(scenario.kernel.storage.path, "history", storage=scenario.kernel.storage).data
    assert history["history"]["rounds"] == {
        ("ticket" if kind == "ticket" else "parent") + "_" + role: 1
        for role in ("development", "review", "publication")}
    events = _project(scenario.kernel, "detailed")
    publication = next(event for event in events if event["kind"] == "stage_end" and event.get("role") == "publication")
    assert publication["status_code"] == "publication_artifact" and publication["duration_seconds"] == 5
    assert publication["effort"] == {}  # Shared backend does not add a Development round.
    if kind == "ticket":
        completed = next(event for event in events if event["kind"] == "ticket_completed")
        assert completed["effort"]["development"]["rounds"] == 1
        assert completed["effort"]["review"]["rounds"] == 1
        assert completed["total_seconds"] == 10  # Includes the semantic Publication's five seconds.
    assert _snapshot(tmp_path) == before
