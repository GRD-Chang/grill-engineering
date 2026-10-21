"""A real managed revision reaches each allocated role's rendered instructions."""
from pathlib import Path

import pytest

from agent_run.prompt_resources import builtin_resources
from agent_run.user_defaults import UserDefaultsStore
from agent_run_kernel import AgentCall, DeliveryKind, Kernel, Policy
from agent_run_kernel.configuration import freeze_creation
from agent_run_kernel.model import Execution, HostObservation
from agent_run_kernel.offline import Scenario
from agent_run_kernel.role_prompt import render_role_prompt


def test_managed_revision_feedback_reaches_real_development_review_and_publication_prompts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = Scenario(tmp_path / "delivery", DeliveryKind.FINAL_RUN,
                        reviews=("fail", "pass"), policy=Policy(run_repairs=2))
    defaults = UserDefaultsStore(tmp_path / "defaults.json")
    defaults.configure(language="en", profile={"preset": "economy"})
    spec = freeze_creation(scenario.spec, defaults, prompt_resources=builtin_resources("en"))
    scenario.kernel = Kernel.create(tmp_path / "state" / "kernel.sqlite3", spec)
    kernel, subject = scenario.kernel, scenario.delivery.key
    request = kernel.admit_host("start", kind="run", command=("native-host",),
                                environment_carrier=tmp_path / "carrier", preflight=lambda: None)
    assert kernel.dispatch_host(request)
    assert kernel.observe_host(request, HostObservation("running", request.unit_name, "original", "/host", "boot",
                                                       42, "start", usage_ready=True)) == "running"
    scenario.execution = Execution(request.run_id, request.owner, request.epoch)
    assert scenario.run().kind == "approval"
    original_pr = scenario.port.pull_request(subject)
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
        if call.role == "publication":
            assert "current work only" in prompt
            assert "not proof of resolution or approval" in prompt
        roles.append(call.role.value)
        return actual_agent(call)

    monkeypatch.setattr(scenario, "agent", agent)
    assert scenario.run().kind == "approval"
    assert roles == ["development", "review", "publication"]
    revised_pr = scenario.port.pull_request(subject)
    assert revised_pr is not None and revised_pr.number == original_pr.number
    assert revised_pr.head != original_pr.head
