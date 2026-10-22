"""A Ticket fixes its dependency-visible Run head at first workspace intent."""

from pathlib import Path

import pytest

from agent_run_kernel import Kernel, Requirements
from agent_run_kernel.model import Role
from agent_run_kernel.offline import GraphScenario
from test_kernel_requirements import native_input
from test_kernel_remote_effects import EffectBoundary, effect_boundary
from support.core_operations import CoreOperations, scripted_return


def dependent_ticket(tmp_path: Path):
    scenario = GraphScenario(tmp_path / "dependencies", Requirements.from_wire(native_input()["initial_requirements"]))
    assert scenario.kernel.source_status()["remaining"]["ticket:4"] == "blocked_by_incomplete_tickets"
    assert scenario.run().kind == "complete"
    head = scenario.port.remote_commit("refs/heads/run")
    assert head is not None and head != scenario.initial
    scenario.select("ticket:4")
    assert not Path(scenario.delivery.checkout).exists()
    assert not scenario.port.git.run("for-each-ref", "--format=%(refname)", scenario.delivery.source_ref).stdout
    return scenario, head


def workspace_intent(scenario, monkeypatch, *, sent=False):
    with effect_boundary(scenario, monkeypatch, "workspace", sent=sent):
        with pytest.raises(EffectBoundary) as boundary:
            CoreOperations(scenario.kernel, scenario.execution, scenario.make_ports).until(lambda step: False, limit=16)
    return boundary.value


def first_development(scenario):
    step = CoreOperations(scenario.kernel, scenario.execution, scenario.make_ports).until(
        lambda step: step.call is not None, limit=8)
    assert step.call.role == Role.DEVELOPMENT
    return step


def test_dependent_ticket_first_development_sees_delivered_dependency_code(tmp_path: Path, monkeypatch) -> None:
    scenario, head = dependent_ticket(tmp_path)
    kernel, subject = scenario.kernel, scenario.delivery.key
    prepared = workspace_intent(scenario, monkeypatch).effect
    assert prepared.operation == "workspace"
    assert prepared.before_sha == head.sha and prepared.base == head
    assert kernel.delivery(subject).base == head
    assert kernel.status(subject)["source_sha"] == head.sha
    scenario.kernel = Kernel(kernel.storage.path)
    assert workspace_intent(scenario, monkeypatch).effect == prepared
    assert scenario.runner().run_once().kind == "progress"
    development = first_development(scenario)
    assert development.call.context["base_sha"] == head.sha
    checkout = Path(development.call.context["checkout"])
    assert scenario.port.git.run("rev-parse", "HEAD", cwd=checkout).stdout.strip() == head.sha
    assert (checkout / "development-1.txt").read_text() == "Change 1\n"
    scripted_return(scenario.kernel, scenario.runner(), development.call, scenario.agent)


@pytest.mark.parametrize("dispatched", [False, True], ids=["prepared", "sent"])
def test_workspace_intent_keeps_allocated_base_after_run_branch_moves(tmp_path: Path, monkeypatch, dispatched: bool) -> None:
    scenario, allocated = dependent_ticket(tmp_path)
    kernel, subject = scenario.kernel, scenario.delivery.key
    boundary = workspace_intent(scenario, monkeypatch, sent=dispatched)
    original = boundary.effect
    assert original.operation == "workspace"
    later = scenario.port.external_remote_append(scenario.delivery.target_ref, {"later-run.txt": "Later Run work\n"}, "after-allocation")
    assert later != allocated
    scenario.kernel = kernel = Kernel(kernel.storage.path)
    repeated = workspace_intent(scenario, monkeypatch)
    assert repeated.phase == ("reconcile" if dispatched else "execute")
    assert repeated.effect == original and kernel.delivery(subject).base == allocated
    if dispatched:
        # The previously authorized physical call completes after restart.
        result = scenario.make_ports(scenario.runner().binding).execute(original, boundary.authorize)
        assert result.disposition == "target"
    assert scenario.runner().run_once().kind == "progress"
    development = first_development(scenario)
    assert development.call.context["base_sha"] == allocated.sha
    assert (Path(development.call.context["checkout"]) / "development-1.txt").exists()
    assert not (Path(development.call.context["checkout"]) / "later-run.txt").exists()
    with kernel.storage.transaction(write=False) as tx:
        workspaces = tx.rows("SELECT before_sha,attempts FROM effects WHERE subject=? AND operation='workspace'", (subject,))
        assert len(workspaces) == 1 and workspaces[0]["before_sha"] == allocated.sha and workspaces[0]["attempts"] == 1
    scripted_return(scenario.kernel, scenario.runner(), development.call, scenario.agent)


def test_new_workspace_never_resets_a_foreign_preexisting_ticket_ref(tmp_path: Path, monkeypatch) -> None:
    scenario, head = dependent_ticket(tmp_path)
    kernel, subject = scenario.kernel, scenario.delivery.key
    scenario.port.git.run("update-ref", scenario.delivery.source_ref, scenario.initial.sha, "0" * len(scenario.initial.sha))
    effect = workspace_intent(scenario, monkeypatch).effect
    assert effect.before_sha == head.sha
    assert scenario.port.local.reconcile(effect).disposition == "third"
    assert scenario.runner().run_once().kind == "paused"
    assert scenario.port.git.commit(scenario.delivery.source_ref) == scenario.initial
    assert not Path(scenario.delivery.checkout).exists()
    requests = [write for write in scenario.port.writes() if write["subject"] == subject and write["operation"] == "workspace"]
    assert len(requests) == 1 and requests[0]["intent"] == effect.effect_id
    assert kernel.status(subject)["gates"] == ["write_conflict"]
    assert scenario.runner().run_once().kind == "paused"
    with effect_boundary(scenario, monkeypatch, "workspace"):
        with pytest.raises(EffectBoundary) as readback:
            scenario.runner().run_once()
    assert readback.value.phase == "reconcile" and readback.value.effect == effect
    assert scenario.runner().run_once().kind == "paused"
    assert [write for write in scenario.port.writes() if write["subject"] == subject and write["operation"] == "workspace"] == requests
    assert scenario.port.git.commit(scenario.delivery.source_ref) == scenario.initial
    assert not Path(scenario.delivery.checkout).exists()
