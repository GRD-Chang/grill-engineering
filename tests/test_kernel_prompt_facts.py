"""Real role allocation freezes responsibility facts before prompt rendering."""

from dataclasses import replace
import json
from pathlib import Path

import pytest

from support.core_operations import CoreOperations, scripted_return, final_scenario
from agent_run.model import AgentCall, DeliveryKind, Policy, Role, Step
from agent_run.model import wire_json
from agent_run.core.kernel import Kernel
from agent_run.core.prompts.role import render_role_prompt
from agent_run.errors import KernelError
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
    assert context["review_range"] == {"base_sha": scenario.initial.sha, "candidate_sha": review.context["reviewed_commit"]}
    assert set(context) == {"facts", "review_range"}
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
    assert frozen["review_range"]["base_sha"] == review.context["reviewed_commit"]
    assert frozen["review_range"]["candidate_sha"] == second.context["reviewed_commit"]
    assert second.context["base_sha"] == scenario.initial.sha
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


@pytest.mark.parametrize("kind", [DeliveryKind.TICKET, DeliveryKind.PARENT_ONLY])
def test_findings_review_uses_previous_actual_result_and_current_development(tmp_path: Path, kind):
    scenario = Scenario(tmp_path / kind.value, kind, reviews=("fail", "pass"))
    runner = scenario.runner()
    development = next_call(scenario, Role.DEVELOPMENT)
    runner.authorize_agent(development)
    raw = json.loads(scenario.agent(development))
    raw["summary"] = "Initial development explanation, not a review conclusion."
    scenario.kernel.receive(development, wire_json(raw))
    review = next_call(scenario, Role.REVIEW)
    frozen = review.context["review_context"]
    assert set(frozen) == {"facts", "review_range"}
    assert frozen["review_range"] == {"base_sha": scenario.initial.sha,
                                      "candidate_sha": review.context["reviewed_commit"]}
    assert frozen["facts"]["development_summary"] == raw["summary"]
    assert frozen["facts"]["development_identity"]["attempt_id"] == development.attempt_id
    actual_prompt = render_role_prompt(review)
    assert raw["summary"] in actual_prompt
    assert 'Spec、Standards、E2E 三个独立子 Agent' in actual_prompt
    assert '最终结构化响应' in actual_prompt and '不得继续调用工具' in actual_prompt
    assert all(command in actual_prompt for command in ('kill', 'pkill', 'killall'))
    wrong_range = {**frozen, "review_range": {**frozen["review_range"], "candidate_sha": scenario.initial.sha}}
    with pytest.raises(KernelError, match="review_context_identity_unavailable"):
        render_role_prompt(replace(review, context={**review.context, "review_context": wrong_range}))
    old_resources = dict(review.context["_prompt_resources"])
    old_resources["review/context-ranges"] = old_resources.pop("review/fixed-range")
    with pytest.raises(ValueError):
        render_role_prompt(replace(review, context={**review.context, "_prompt_resources": old_resources}))
    runner.authorize_agent(review)
    scenario.kernel.receive(review, scenario.agent(review))
    repair = next_call(scenario, Role.DEVELOPMENT)
    runner.authorize_agent(repair)
    repaired = json.loads(scenario.agent(repair))
    repaired["summary"] = "Current repair explanation with directly affected regression evidence."
    scenario.kernel.receive(repair, wire_json(repaired))
    # New allocation after reopening must use the saved actual Review, not live HEAD.
    scenario.kernel = Kernel(scenario.kernel.storage.path)
    rereview = next_call(scenario, Role.REVIEW)
    facts = rereview.context["review_context"]["facts"]
    assert rereview.thread_id != review.thread_id
    assert rereview.context["review_context"]["review_range"] == {
        "base_sha": review.context["reviewed_commit"],
        "candidate_sha": rereview.context["reviewed_commit"],
    }
    assert rereview.context["base_sha"] == scenario.initial.sha
    assert facts["previous_acceptance_artifact"] == review_result("fail")
    assert facts["previous_review_identity"] == frozen["facts"]["current_review_identity"]
    assert facts["development_summary"] == repaired["summary"]
    prompt = render_role_prompt(rereview)
    assert repaired["summary"] in prompt
    assert review_result("fail")["checks"]["spec"]["findings"][0] in prompt
    assert "full_range" not in prompt and "has_complete_baseline" not in prompt
    assert "交付身份基准（背景，不是本轮比较起点）" in prompt
    checkout = Path(rereview.context["checkout"])
    assert scenario.port.git.run("rev-parse", "HEAD", cwd=checkout).stdout.strip() == rereview.context["reviewed_commit"]


@pytest.mark.parametrize("kind", [DeliveryKind.TICKET, DeliveryKind.PARENT_ONLY, DeliveryKind.FINAL_RUN])
@pytest.mark.parametrize("another_ci_failure", [False, True], ids=["findings", "next-ci-head"])
def test_ci_review_uses_failed_head_then_the_actual_findings_review(tmp_path: Path, kind, another_ci_failure):
    policy = Policy(ticket_ci_review=True)
    checks = ("fail", "fail", "none") if another_ci_failure else ("fail", "none")
    scenario = (final_scenario(tmp_path / kind.value, policy=policy, checks=checks)
                if kind == DeliveryKind.FINAL_RUN else
                Scenario(tmp_path / kind.value, kind, policy=policy, checks=checks))
    if kind == DeliveryKind.FINAL_RUN:
        # Its preparatory Ticket already created development-1.txt. Each new
        # development must make a real change, including subsequent CI repair.
        scenario.edits = tuple({f"final-ci-repair-{index}.txt": f"Actual repair {index}\n"}
                               for index in range(1, 5))
    runner = scenario.runner()
    previous_review = None
    development = None
    for _ in range(220):
        step = runner.run_once()
        call = step.call
        if call is None:
            assert step.kind not in {"paused", "waiting", "complete", "approval"}, step
            continue
        if call.role == Role.REVIEW and call.purpose == "ci":
            context = call.context["review_context"]
            failed_head = call.context["ci_evidence"]["head_sha"]
            expected_base = (failed_head if previous_review is None or another_ci_failure
                             else previous_review.context["reviewed_commit"])
            actual_range = context.get("review_range", context.get("focus_range"))
            assert actual_range == {"base_sha": expected_base,
                                    "candidate_sha": call.context["reviewed_commit"]}
            assert expected_base != scenario.initial.sha
            assert development is not None
            assert context["facts"]["development_summary"] == development[1]
            assert development[1] in render_role_prompt(call)
            if previous_review is not None:
                if another_ci_failure:
                    assert failed_head != previous_review.context["ci_evidence"]["head_sha"]
                else:
                    assert context["facts"]["previous_acceptance_artifact"] == review_result("fail")
                return
            previous_review = call
            runner.authorize_agent(call)
            scenario.agent(call)
            scenario.kernel.receive(call, wire_json(review_result("pass" if another_ci_failure else "fail")))
            continue
        runner.authorize_agent(call)
        raw = json.loads(scenario.agent(call))
        if call.role == Role.DEVELOPMENT:
            raw["summary"] = f"Current actual development {call.attempt_id}, purpose {call.purpose}."
            development = call.attempt_id, raw["summary"]
        scenario.kernel.receive(call, wire_json(raw))
    raise AssertionError("The existing workflow did not reach the second CI Review")
