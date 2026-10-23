"""Real role allocation freezes responsibility facts before prompt rendering."""

from dataclasses import replace
from pathlib import Path

import pytest

from support.core_operations import CoreOperations, scripted_return, final_scenario
from agent_run.model import AgentCall, DeliveryKind, Policy, Role, Step
from agent_run.model import wire_json
from support.offline import Scenario, review_result


def next_call(scenario: Scenario, role: Role) -> AgentCall:
    operations = CoreOperations(scenario.kernel, scenario.execution, scenario.make_ports)
    for _ in range(70):
        step = operations.once()
        if step.call is not None and step.call.role == role:
            return step.call
        if step.call is not None:
            scripted_return(scenario.kernel, operations.runner, step.call, scenario.agent)
    raise AssertionError(f"No {role} call")


def test_same_thread_new_repair_and_human_continuation_keep_distinct_responsibilities(tmp_path: Path) -> None:
    scenario = Scenario(tmp_path / "ticket", DeliveryKind.TICKET, reviews=("fail", "pass"))
    operations = CoreOperations(scenario.kernel, scenario.execution, scenario.make_ports)
    first = next_call(scenario, Role.DEVELOPMENT)
    assert first.context.get("semantic_continuation") is False
    scripted_return(scenario.kernel, operations.runner, first, scenario.agent)
    review = next_call(scenario, Role.REVIEW)
    context = review.context.get("review_context")
    assert isinstance(context, dict)
    assert context["full_range"] == {"base_sha": scenario.initial.sha, "candidate_sha": review.context["reviewed_commit"]}
    assert context["focus_range"] == context["full_range"] and context["has_complete_baseline"] is False
    scripted_return(scenario.kernel, operations.runner, review, scenario.agent)
    repair = next_call(scenario, Role.DEVELOPMENT)
    assert repair.thread_id == first.thread_id and repair.mode == "resume"
    assert repair.attempt_id != first.attempt_id and repair.context.get("semantic_continuation") is False
    assert repair.context.get("acceptance_artifact") == review_result("fail")
    assert repair.context["review_budget_context"]["remaining_review_attempts"] == 2
    scenario.kernel.receive(repair, wire_json({"result_kind": "human_blocker", "summary": None, "human_blockers": ["Choose the exact formatting."]}))
    assert operations.once().kind == "paused"
    response = "Keep the original formatting.\r\n"
    scenario.kernel.control("resume", "answer", subject=repair.subject, message=response)
    continued = next_call(scenario, Role.DEVELOPMENT)
    assert continued.attempt_id == repair.attempt_id and continued.thread_id == repair.thread_id
    assert continued.context.get("semantic_continuation") is True
    assert continued.context["human_responses"] == [response]
    assert continued.context["acceptance_artifact"] == repair.context["acceptance_artifact"]
    scripted_return(scenario.kernel, operations.runner, continued, scenario.agent)
    for _ in range(20):
        step = operations.once()
        if step.call is not None and step.call.role == Role.REVIEW:
            second = step.call
            break
        if step.call is not None:
            scripted_return(scenario.kernel, operations.runner, step.call, scenario.agent)
    else:
        raise AssertionError("No second Review")
    frozen = second.context["review_context"]
    assert frozen["has_complete_baseline"] is True
    assert frozen["focus_range"]["base_sha"] == review.context["reviewed_commit"]
    assert frozen["full_range"]["base_sha"] == scenario.initial.sha
    assert frozen["facts"]["previous_acceptance_artifact"] == review_result("fail")


@pytest.mark.parametrize("kind", [DeliveryKind.PARENT_ONLY, DeliveryKind.FINAL_RUN])
def test_parent_publication_gets_the_actual_accepted_artifact(tmp_path: Path, kind: DeliveryKind) -> None:
    scenario = (final_scenario(tmp_path / kind.value) if kind == DeliveryKind.FINAL_RUN
                else Scenario(tmp_path / kind.value, kind))
    operations = CoreOperations(scenario.kernel, scenario.execution, scenario.make_ports)
    publication = next_call(scenario, Role.PUBLICATION)
    assert publication.context.get("acceptance_artifact") == review_result("pass")
    assert "fallback_publication_context" not in publication.context
    assert "ci_repair_publication_context" not in publication.context


def test_ticket_fallback_publication_discloses_the_unreviewed_candidate(tmp_path: Path) -> None:
    scenario = Scenario(tmp_path / "fallback", DeliveryKind.TICKET, reviews=("fail",), policy=Policy(ticket_rounds=1))
    operations = CoreOperations(scenario.kernel, scenario.execution, scenario.make_ports)
    publication = next_call(scenario, Role.PUBLICATION)
    facts = publication.context.get("fallback_publication_context")
    assert isinstance(facts, dict)
    assert facts["current_candidate_has_additional_review"] is False
    assert facts["current_candidate_identity"]["candidate_sha"] == publication.context["candidate_sha"]
    assert facts["last_review_artifact"] == review_result("fail")
    assert facts["development_summary"] == "Changed synthetic behavior 2"
    assert "acceptance_artifact" not in publication.context
