"""A Ticket fixes its dependency-visible Run head at first workspace intent."""

from pathlib import Path

import pytest

from agent_run_kernel import Kernel, Requirements
from agent_run_kernel.model import Role
from agent_run_kernel.offline import GraphScenario
from test_kernel_requirements import native_input


def dependent_ticket(tmp_path: Path):
    scenario = GraphScenario(tmp_path / "dependencies", Requirements.from_wire(native_input()["initial_requirements"]))
    assert scenario.kernel.advance("ticket:4", scenario.execution).reason == "blocked_by_incomplete_tickets"
    assert scenario.run().kind == "complete"
    head = scenario.port.remote_commit("refs/heads/run")
    assert head is not None and head != scenario.initial
    scenario.select("ticket:4")
    assert not Path(scenario.delivery.checkout).exists()
    assert not scenario.port.git.run("for-each-ref", "--format=%(refname)", scenario.delivery.source_ref).stdout
    return scenario, head


def first_development(scenario):
    for _ in range(8):
        step = scenario.kernel.advance(scenario.delivery.key, scenario.execution, scenario.world())
        if step.call is not None:
            assert step.call.role == Role.DEVELOPMENT
            return step
        scenario.handle(step)
    raise AssertionError("The allocated Ticket never reached its first Development")


def test_dependent_ticket_first_development_sees_delivered_dependency_code(tmp_path: Path) -> None:
    scenario, head = dependent_ticket(tmp_path)
    kernel, subject = scenario.kernel, scenario.delivery.key
    prepared = kernel.advance(subject, scenario.execution, scenario.world())
    assert prepared.effect is not None and prepared.effect.operation == "workspace"
    assert prepared.effect.before_sha == head.sha and prepared.effect.base == head
    assert kernel.delivery(subject).base == head
    assert kernel.status(subject)["source_sha"] == head.sha
    scenario.kernel = Kernel(kernel.storage.path)
    assert scenario.kernel.advance(subject, scenario.execution, scenario.world()).effect == prepared.effect
    scenario.handle(prepared)
    development = first_development(scenario)
    assert development.call.context["base_sha"] == head.sha
    checkout = Path(development.call.context["checkout"])
    assert scenario.port.git.run("rev-parse", "HEAD", cwd=checkout).stdout.strip() == head.sha
    assert (checkout / "development-1.txt").read_text() == "Change 1\n"
    scenario.handle(development)


@pytest.mark.parametrize("dispatched", [False, True], ids=["prepared", "sent"])
def test_workspace_intent_keeps_allocated_base_after_run_branch_moves(tmp_path: Path, dispatched: bool) -> None:
    scenario, allocated = dependent_ticket(tmp_path)
    kernel, subject = scenario.kernel, scenario.delivery.key
    step = kernel.advance(subject, scenario.execution, scenario.world())
    original = step.effect
    assert original is not None and original.operation == "workspace"
    if dispatched:
        assert kernel.dispatch(original, scenario.execution, scenario.world())
    later = scenario.port.external_remote_append(scenario.delivery.target_ref, {"later-run.txt": "Later Run work\n"}, "after-allocation")
    assert later != allocated
    scenario.kernel = kernel = Kernel(kernel.storage.path)
    repeated = kernel.advance(subject, scenario.execution, scenario.world())
    assert repeated.kind == ("reconcile" if dispatched else "effect")
    assert repeated.effect == original and kernel.delivery(subject).base == allocated
    if dispatched:
        result = scenario.port.execute(original, lambda: kernel.guard_effect(original, scenario.execution, scenario.world()))
        assert result.disposition == "target"
        kernel.reconcile(original, result, scenario.execution, scenario.world())
    else:
        scenario.handle(repeated)
    development = first_development(scenario)
    assert development.call.context["base_sha"] == allocated.sha
    assert (Path(development.call.context["checkout"]) / "development-1.txt").exists()
    assert not (Path(development.call.context["checkout"]) / "later-run.txt").exists()
    with kernel.storage.transaction(write=False) as tx:
        workspaces = tx.rows("SELECT before_sha,attempts FROM effects WHERE subject=? AND operation='workspace'", (subject,))
        assert len(workspaces) == 1 and workspaces[0]["before_sha"] == allocated.sha and workspaces[0]["attempts"] == 1
    scenario.handle(development)


def test_new_workspace_never_resets_a_foreign_preexisting_ticket_ref(tmp_path: Path) -> None:
    scenario, head = dependent_ticket(tmp_path)
    kernel, subject = scenario.kernel, scenario.delivery.key
    scenario.port.git.run("update-ref", scenario.delivery.source_ref, scenario.initial.sha, "0" * len(scenario.initial.sha))
    step = kernel.advance(subject, scenario.execution, scenario.world())
    effect = step.effect
    assert effect is not None and effect.before_sha == head.sha
    assert scenario.port.local.reconcile(effect).disposition == "third"
    scenario.handle(step)
    assert scenario.port.git.commit(scenario.delivery.source_ref) == scenario.initial
    assert not Path(scenario.delivery.checkout).exists()
    assert not [write for write in scenario.port.writes() if write["subject"] == subject and write["operation"] == "workspace"]
    assert kernel.advance(subject, scenario.execution, scenario.world()).kind == "paused"
