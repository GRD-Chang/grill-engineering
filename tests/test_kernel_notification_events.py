"""Pure native event projection uses real committed Core producers."""
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from agent_run_kernel import AgentCall, DeliveryKind, Issue, Kernel, ParentIssue, Requirements, Role, Step
from agent_run_kernel.model import wire_json
from agent_run_kernel.notification_events import events
from support.offline import GraphScenario, Scenario


def _project(kernel: Kernel, mode: str) -> list[dict[str, Any]]:
    with kernel.storage.transaction(write=False) as tx:
        run = tx.one("SELECT id,repository,parent_issue,parent_revision,language,created_at FROM runs")
        title = tx.rows("SELECT title FROM requirement_versions WHERE issue=? AND revision=?",
                        (run["parent_issue"], run["parent_revision"]))
        state = {"run_id": run["id"], "repository": run["repository"],
                 "parent": {"number": run["parent_issue"], "title": title[0][0] if title else ""},
                 "language": run["language"], "created_at": run["created_at"]}
        frozen = deepcopy(state)
        result = [event for row in tx.all("SELECT * FROM history ORDER BY position")
                  for event in events(tx, dict(row), state, mode)]
        assert state == frozen
        return result


def _recorded_agents(scenario: GraphScenario, monkeypatch: pytest.MonkeyPatch,
                     result: Callable[[AgentCall], bytes] | None = None,
                     started: Callable[[AgentCall], None] | None = None) -> None:
    original_agent = scenario.agent
    original_receive = scenario.kernel.receive
    completed: dict[tuple[str, int], tuple[dict[str, Any], bytes]] = {}

    def agent(call: AgentCall) -> bytes:
        # These are explicit synthetic observations, never an OS process claim.
        # Normal Scenario.step and direct fixture consumers share this leaf.
        process = {"pid": 21, "pgid": 21, "start_token": "synthetic-process",
                   "host_pid": 10, "host_start_token": "synthetic-host"}
        assert scenario.kernel.dispatch_agent(call)
        scenario.runner().authorize_agent(call)
        scenario.kernel.agent_started(call, process)
        if started is not None:
            started(call)
        raw = (result or original_agent)(call)
        # A throwing/interrupted callback supplies no known exit. Only this
        # exact synchronously returned output may settle its synthetic process.
        completed[(call.invocation_id, call.output_ordinal)] = (process, raw)
        return raw

    def receive(call: AgentCall, raw: bytes) -> None:
        original_receive(call, raw)
        known = completed.pop((call.invocation_id, call.output_ordinal), None)
        if known is not None and known[1] == raw:
            scenario.kernel.agent_exited(call, known[0], 0)

    monkeypatch.setattr(scenario, "agent", agent)
    monkeypatch.setattr(scenario.kernel, "receive", receive)

@pytest.mark.parametrize("mode", ["detailed", "concise"])
def test_role_events_require_real_start_and_consumption_and_do_not_duplicate_first_start(
    tmp_path: Path, mode: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = GraphScenario(tmp_path, Requirements(
        ParentIssue(1, "Parent", "Original scope", (2,)),
        (Issue(2, "Ticket", "Original task", "OPEN", labels=("ready-for-agent",)),)))
    _recorded_agents(scenario, monkeypatch)
    consumed: list[str] = []
    for _ in range(100):
        step = scenario.step()
        if step.kind == "agent":
            # The explicit synthetic Agent seam recorded dispatch/start/raw/exit.
            # The next real Core advance has not consumed the result.
            assert step.call is not None
            before = _project(scenario.kernel, mode)
            assert sum(event["kind"] == "ticket_started" for event in before) == 1
            assert not any(event["kind"] == "stage_start" and event.get("role") == "development" for event in before)
            assert not any(event["kind"] == "stage_end" and event.get("attempt_id") == step.call.attempt_id for event in before)
            assert scenario.step().kind == "progress"
            after = _project(scenario.kernel, mode)
            ended = [event for event in after if event["kind"] == "stage_end" and event.get("attempt_id") == step.call.attempt_id]
            assert len(ended) == (1 if mode == "detailed" else 0)
            if ended:
                assert ended[0]["role"] == step.call.role.value
                consumed.append(ended[0]["role"])
                if step.call.role.value == "publication":
                    assert ended[0]["status_code"] == "publication_artifact"
                    assert not any(event["kind"] == "pr_created" for event in after)
        else:
            assert step.kind not in {"paused", "waiting"}, step
        # Stop sampling this Ticket only after its actual Completion/cleanup.
        # The next global operation may legitimately admit Final; no global
        # quiescence or subject-selected transition is manufactured here.
        ticket = scenario.kernel.status("ticket:2")
        if (ticket["phase"] == "complete" and ticket["resource_cleanup"] is not None
                and ticket["resource_cleanup"]["state"] == "complete"):
            break
    else:
        raise AssertionError("The real Ticket pipeline did not finish")
    projected = _project(scenario.kernel, mode)
    assert sum(event["kind"] == "ticket_started" for event in projected) == 1
    assert sum(event["kind"] == "ticket_completed" for event in projected) == 1
    assert sum(event["kind"] == "pr_created" for event in projected) == (1 if mode == "detailed" else 0)
    assert consumed == (["development", "review", "publication"] if mode == "detailed" else [])
    assert [event["role"] for event in projected if event["kind"] == "stage_start"] == (
        ["review", "publication"] if mode == "detailed" else [])
    assert _project(scenario.kernel, mode) == projected


@pytest.mark.parametrize("mode", ["detailed", "concise"])
def test_full_run_first_review_and_repair_replace_corresponding_stage_events(tmp_path, monkeypatch, mode):
    from agent_run.notification_event_facts import event_copy

    # d847's Final fixture retains a real historical Ticket and Completion.
    # Its prefix has already passed Review; this script begins at Final Review.
    scenario = Scenario(tmp_path, DeliveryKind.FINAL_RUN, reviews=("fail", "pass"))
    assert scenario.kernel.status("ticket:2")["phase"] == "complete"
    assert len(scenario.kernel.source_status()["completions"]) == 1
    start_views = {}

    def started(call):
        if call.subject == "final:1" and call.role == Role.DEVELOPMENT:
            start_views[call.attempt_id] = _project(scenario.kernel, mode)

    _recorded_agents(scenario, monkeypatch, started=started)
    first_review = repair = None
    planned_id = None
    for _ in range(100):
        step = scenario.step()
        projected = _project(scenario.kernel, mode)
        if step.call is not None and step.call.role == Role.REVIEW and first_review is None:
            first_review = step.call.attempt_id
            started = [event for event in projected if event["kind"] == "acceptance_started"]
            assert len(started) == 1 and started[0]["attempt_id"] == first_review
            assert not any(event["kind"] == "stage_start" and event.get("attempt_id") == first_review for event in projected)
            assert not any(event["kind"] == "acceptance_repair" for event in projected)
            assert scenario.step().kind == "progress"
            planned = [event for event in _project(scenario.kernel, mode) if event["kind"] == "acceptance_repair"]
            assert len(planned) == 1 and planned[0]["live"] is False
            assert planned[0]["title"] == event_copy({"language": "zh"}, "acceptance_repair_planned")
            planned_id = planned[0]["id"]
            assert not any(event["kind"] == "stage_end" and event.get("attempt_id") == first_review
                           for event in _project(scenario.kernel, mode))
        elif step.call is not None and step.call.role == Role.DEVELOPMENT:
            repair = step.call.attempt_id
            milestones = [event for event in start_views[repair] if event["kind"] == "acceptance_repair"]
            assert milestones[-1]["live"] is True
            assert milestones[-1]["title"] == event_copy({"language": "zh"}, "acceptance_repair_active")
            assert {event["id"] for event in milestones} == {planned_id}
            assert not any(event["kind"] == "stage_start" and event.get("attempt_id") == repair for event in projected)
        promoted = [event for event in projected if event["kind"] == "acceptance_passed"]
        if promoted:
            assert first_review is not None and repair is not None
            assert not any(event["kind"] == "stage_end" and event.get("attempt_id") == promoted[0]["attempt_id"]
                           for event in projected)
            break
        assert step.kind not in {"paused", "waiting", "approval"}, step
    else:
        raise AssertionError("FullRun did not promote its actual repair Review")


@pytest.mark.parametrize("role", [Role.DEVELOPMENT, Role.REVIEW, Role.PUBLICATION])
def test_human_result_uses_original_paused_material_even_after_resume(tmp_path, monkeypatch, role):
    scenario = GraphScenario(tmp_path, Requirements(
        ParentIssue(1, "Parent", "Original scope", (2,)),
        (Issue(2, "Ticket", "Original task", "OPEN", labels=("ready-for-agent",)),)))
    reason = "Original human decision / 保留原文"
    original = scenario.agent

    def result(call):
        raw = original(call)
        if call.role != role:
            return raw
        if role == Role.REVIEW:
            return wire_json({"checks": {lane: {"status": "blocked" if lane == "e2e" else "pass",
                                                "evidence": reason if lane == "e2e" else "Passed exact lane",
                                                "findings": []} for lane in ("e2e", "standards", "spec")}})
        return wire_json({"result_kind": "human_blocker", "human_blockers": [reason],
                          **({"summary": None} if role == Role.DEVELOPMENT else
                             {"commit_message": None, "pr_title": None, "pr_body_markdown": None})})

    _recorded_agents(scenario, monkeypatch, result)
    for _ in range(100):
        step = scenario.step()
        if step.call is not None and step.call.role == role:
            call = step.call
            assert not any(event["kind"] == "stage_end" and event.get("attempt_id") == call.attempt_id
                           for event in _project(scenario.kernel, "detailed"))
            assert scenario.step().kind == "paused"
            break
        assert step.kind not in {"paused", "waiting", "approval"}, step
    else:
        raise AssertionError("The selected role never returned its human artifact")
    results = [event for event in _project(scenario.kernel, "detailed")
               if event["kind"] == "stage_end" and event.get("attempt_id") == call.attempt_id]
    assert len(results) == 1 and reason in results[0]["summary"]
    assert results[0]["status_code"] == ("review_blocked" if role == Role.REVIEW else "human_blocker")
    assert results[0]["color"] == "yellow" and results[0]["next_step"]
    assert not any(event["kind"] == "stage_end" for event in _project(scenario.kernel, "concise"))
    scenario.kernel.control("resume", "answer-original", subject=call.subject,
                            message="Use the original decision", world=scenario.world())
    assert [event for event in _project(scenario.kernel, "detailed") if event["id"] == results[0]["id"]] == results


def test_human_revision_after_a_new_generation_is_not_the_old_automatic_repair(tmp_path, monkeypatch):
    from dataclasses import replace

    # d847's Final fixture retains a real historical Ticket and Completion.
    # Its prefix has already passed Review; this script begins at Final Review.
    scenario = Scenario(tmp_path, DeliveryKind.FINAL_RUN, reviews=("fail", "pass"))
    assert scenario.kernel.status("ticket:2")["phase"] == "complete"
    assert len(scenario.kernel.source_status()["completions"]) == 1
    start_views = {}

    def started(call):
        if call.subject == "final:1" and call.role == Role.DEVELOPMENT:
            start_views[call.attempt_id] = _project(scenario.kernel, "detailed")

    _recorded_agents(scenario, monkeypatch, started=started)
    for _ in range(100):
        step = scenario.step()
        if step.call is not None and step.call.role == Role.REVIEW:
            assert scenario.step().kind == "progress"
            break
        assert step.kind not in {"paused", "waiting", "approval"}, step
    else:
        raise AssertionError("The first overall Review did not return its failure")
    original_generation = scenario.kernel.status("final:1")["generation"]
    planned = [event for event in _project(scenario.kernel, "detailed") if event["kind"] == "acceptance_repair"]
    assert len(planned) == 1 and planned[0]["live"] is False
    scenario.requirements = replace(scenario.requirements, parent=replace(
        scenario.requirements.parent, body="A revised whole-Run requirement before automatic repair started"))
    fresh = scenario.world()
    assert scenario.kernel.status("final:1")["phase"] == "refresh_final"
    # A new Parent requirement needs the same explicit lifecycle action as the
    # formal entry; automatic repair must not accept it on the maintainer's behalf.
    assert scenario.kernel.control("requeue", "accept-revised-whole-run-requirement",
                                   subject="final:1", world=fresh) == "applied"
    refreshed = scenario.run()
    assert refreshed.kind == "approval", refreshed
    assert scenario.kernel.status("final:1")["generation"] == original_generation + 1
    assert scenario.kernel.control("revise", "human-revision-after-refresh", subject="final:1",
                                   world=scenario.world(), message="A separate maintainer revision") == "applied"
    step = scenario.step()
    assert step.call is not None and step.call.role == Role.DEVELOPMENT
    assert step.call.context["repair_source"] == "human_revision"
    current = start_views[step.call.attempt_id]
    assert any(event["kind"] == "stage_start" and event.get("attempt_id") == step.call.attempt_id for event in current)
    assert not any(event["kind"] == "acceptance_repair" and event["live"] for event in current)
