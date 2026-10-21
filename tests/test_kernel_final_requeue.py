"""Explicit Final requeue performs fresh whole-Run Acceptance, retaining budgets."""
from dataclasses import replace
from pathlib import Path

from test_kernel_abandon import managed_abandon_scenario


def test_managed_final_requeue_reaccepts_current_run_without_a_repair_pr_or_budget_refund(tmp_path: Path) -> None:
    scenario, request = managed_abandon_scenario(tmp_path)
    scenario.reviews[:] = ["pass", "pass", "fail", "pass"]
    assert scenario.run().kind == "complete"
    scenario.select("ticket:4")
    assert scenario.run().kind == "complete"
    scenario.select(scenario.activate_final())
    kernel, subject = scenario.kernel, scenario.delivery.key
    for _ in range(50):
        scenario.step()
        if kernel.status(subject)["phase"] == "development":
            break
    else:
        raise AssertionError("The original whole-Run Review did not request repair")
    before = kernel.status(subject)
    assert before["generation"] == 1
    assert not kernel.scheduling()["active"]
    completions = kernel.source_status()["completions"]
    original = scenario.delivery
    preserved = Path(original.checkout) / "old-repair-note.txt"
    preserved.write_text("Uncommitted original repair work must remain available\n")
    scenario.requirements = replace(scenario.requirements, parent=replace(scenario.requirements.parent,
        body=scenario.requirements.parent.body + "\nClarified current whole-Run acceptance."))
    current = scenario.port.external_remote_append(original.source_ref, {"fresh-run.txt": "Current RunBranch work\n"}, "fresh-run")
    world = scenario.world()
    assert world.source == current and kernel.source_status()["blocked"] == "final_refresh"
    writes = scenario.port.writes()
    assert kernel.control_host(request, "requeue", "reaccept-current-run", subject=subject, world=world) == "applied"
    assert kernel.control_host(request, "requeue", "reaccept-current-run", subject=subject, world=world) == "applied"
    after = kernel.status(subject)
    assert after["generation"] == 2 and after["budgets"] == before["budgets"]
    assert kernel.active_host_action() == "start"
    assert kernel.source_status()["completions"] == completions
    assert scenario.port.writes() == writes
    assert preserved.read_text() == "Uncommitted original repair work must remain available\n"
    delivery = kernel.delivery(subject)
    assert delivery.source_ref == original.source_ref and delivery.target_ref == original.target_ref
    assert delivery.checkout != original.checkout and delivery.base == current
    for _ in range(50):
        step = kernel.advance(subject, scenario.execution, scenario.world())
        if step.call is not None:
            fresh = step.call
            break
        scenario.handle(step)
    else:
        raise AssertionError("Explicit Final requeue never reached fresh whole-Run Review")
    assert fresh.role.value == "review" and fresh.context["generation"] == 2
    assert fresh.context["reviewed_commit"] == current.sha
    assert fresh.context["full_scope"] == "final_run"
    assert any("Clarified current whole-Run acceptance." in item["body"] for item in fresh.context["requirements"])
    kernel.receive(fresh, scenario.agent(fresh))
    assert scenario.run().kind == "approval"
    assert [call["role"] for call in scenario.role_calls if call["subject"] == subject] == ["review", "review", "publication"]
    assert kernel.status(subject)["budgets"]["integration"]["reviews_used"] == before["budgets"]["integration"]["reviews_used"] + 1
    final_prs = [write for write in scenario.port.writes() if write["subject"] == subject and write["operation"] == "pr_write"]
    assert len(final_prs) == 1
    assert scenario.port.pull_request(subject).source_ref == original.source_ref
