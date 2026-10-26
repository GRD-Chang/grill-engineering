"""A real managed revision reaches each allocated role's rendered instructions."""
from pathlib import Path

import pytest

from agent_run.config.resources import builtin_resources
from agent_run.config.defaults import UserDefaultsStore
from agent_run.model import AgentCall, Issue, ParentIssue, Policy, Requirements
from agent_run.core.kernel import Kernel
from agent_run.config.creation import freeze_creation
from agent_run.model import Execution, HostObservation
from support.offline import GraphScenario
from agent_run.core.prompts.role import render_role_prompt
from support.core_operations import CoreOperations, scripted_return


def test_managed_revision_feedback_reaches_real_development_review_and_publication_prompts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = Requirements(ParentIssue(1, "Parent", "Deliver the original behavior.", (2,)), (
        Issue(2, "Ticket", "Implement the original behavior.", "OPEN", frozenset({"ready-for-agent"})),
    ))
    scenario = GraphScenario(tmp_path / "delivery", source, policy=Policy(run_repairs=2))
    scenario.checks = ["pass"]
    defaults = UserDefaultsStore(tmp_path / "defaults.json")
    defaults.configure(language="en", profile={"preset": "economy"})
    spec = freeze_creation(scenario.spec, defaults, prompt_resources=builtin_resources("en"))
    # Freeze creation before any operation; preserve the real Ticket history
    # and Completion when the same admitted Host later reaches Final.
    scenario.kernel = Kernel.create(tmp_path / "state" / "kernel.sqlite3", spec, requirements=source)
    kernel = scenario.kernel
    request = kernel.admit_host("start", kind="run", command=("native-host",),
                                environment_carrier=tmp_path / "carrier", preflight=lambda: None)
    assert kernel.dispatch_host(request)
    assert kernel.observe_host(request, HostObservation("running", request.unit_name, "original", "/host", "boot",
                                                       42, "start", usage_ready=True)) == "running"
    scenario.execution = Execution(request.run_id, request.owner, request.epoch)
    operations = CoreOperations(kernel, scenario.execution, scenario.make_ports)
    for _ in range(100):
        step = operations.once()
        if step.call is not None:
            scripted_return(kernel, operations.runner, step.call, scenario.agent)
        if kernel.source_status()["final_subject"] is not None:
            scenario.select("final:1")
            break
    else:
        raise AssertionError("The managed Host never reached real Ticket Completion and Final admission")
    assert len(kernel.source_status()["completions"]) == 1
    assert kernel.status(scenario.delivery.key)["pending_attempt"] is None
    scenario.reviews = ["fail", "pass"]
    scenario.checks = ["none"]
    scenario.development_count = scenario.review_count = scenario.publication_count = 0
    subject = scenario.delivery.key
    assert scenario.run().kind == "approval"
    original_pr = scenario.port.pull_request_by_ref(scenario.kernel.delivery(subject).source_ref, scenario.kernel.delivery(subject).target_ref)
    assert original_pr is not None
    feedback = "revision-render-proof: preserve default-branch compatibility."
    assert kernel.control_host(request, "revise", "maintainer-revision", subject=subject,
                               world=scenario.world(), message=feedback) == "applied"
    actual_agent = scenario.agent
    roles: list[str] = []

    def agent(call: AgentCall) -> bytes:
        assert call.context["human_feedback"] == feedback
        assert not call.context.get("human_responses")
        if call.role == "review":
            assert call.context["review_context"]["facts"]["human_feedback"] == feedback
        prompt = render_role_prompt(call)
        assert prompt.count(feedback) == 1, (call.role, prompt)
        if call.role == 'review':
            assert 'three independent subagents for Spec, Standards and E2E' in prompt
            assert 'final structured response' in prompt and 'do not call tools afterwards' in prompt
            assert all(command in prompt for command in ('kill', 'pkill', 'killall'))
        if call.role == "publication":
            assert "current work only" in prompt
            assert "not proof of resolution or approval" in prompt
        roles.append(call.role.value)
        return actual_agent(call)

    monkeypatch.setattr(scenario, "agent", agent)
    assert scenario.run().kind == "approval"
    assert roles == ["development", "review", "publication"]
    revised_pr = scenario.port.pull_request_by_ref(scenario.kernel.delivery(subject).source_ref, scenario.kernel.delivery(subject).target_ref)
    assert revised_pr is not None and revised_pr.number == original_pr.number
    assert revised_pr.head != original_pr.head
    assert kernel.active_host_action() == "start"
    assert kernel.host_request("start") == request
