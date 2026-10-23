"""External readers consume observed identities without duplicating decisions."""

from dataclasses import replace
from pathlib import Path

import pytest

from agent_run_kernel import DeliveryKind, Kernel, KernelError, Policy
from agent_run_kernel.model import Issue, ParentIssue, Requirements
from support.offline import GraphScenario, Scenario
from support.core_operations import CoreOperations, scripted_return


def test_refresh_receipt_preserves_observed_issue_and_completion_identities(tmp_path: Path) -> None:
    source = Requirements(ParentIssue(1, "Parent", "Whole requirement", (2, 3)), (
        Issue(2, "Ticket", "Initial requirement", "OPEN", frozenset({"ready-for-agent"})),
        Issue(3, "Sibling", "Independent work", "OPEN", frozenset({"ready-for-agent"})),
    ))
    scenario = GraphScenario(tmp_path / "source", source, policy=Policy(concurrency=2))
    kernel = scenario.kernel
    operations = CoreOperations(kernel, scenario.execution, scenario.make_ports)
    first = operations.until(lambda step: step.call is not None).call
    second = operations.until(lambda step: step.call is not None).call
    assert first is not None and second is not None
    assert (first.subject, second.subject) == ("ticket:2", "ticket:3")
    scripted_return(kernel, operations.runner, second, scenario.agent)
    sibling = kernel.status(second.subject)
    assert sibling["pending_return"] is True
    revision = kernel.delivery(first.subject).revision
    scenario.requirements = replace(source, issues=(
        replace(source.issues[0], body="New exact requirement"), source.issues[1]))
    operations.runner.observe(first.subject)
    receipt = kernel.source_status()
    observed = receipt.get("observed_issues")
    assert isinstance(observed, list) and [item["issue"] for item in observed] == [2, 3]
    assert observed[0]["revision"] != revision
    assert observed[0]["revision"] == kernel.status(first.subject)["source_revision"]
    assert receipt.get("completion_revisions") == [
        {"subject": "ticket:2", "generation": 1, "completion_revision": "no-tickets"},
        {"subject": "ticket:3", "generation": 1, "completion_revision": "no-tickets"},
    ]
    assert Kernel(kernel.storage.path).source_status() == receipt
    assert kernel.status(second.subject) == sibling


@pytest.mark.parametrize("head_repository", [None, "foreign/repository"])
def test_same_branch_in_unknown_or_foreign_repository_cannot_authorize_merge(tmp_path: Path, head_repository: str | None) -> None:
    scenario = Scenario(tmp_path / "scenario", DeliveryKind.PARENT_ONLY)
    assert scenario.run().kind == "approval"
    world = scenario.world()
    assert world.pr is not None
    changed = replace(world, pr=replace(world.pr, head_repository=head_repository))
    with pytest.raises(KernelError, match="approval_currentness"):
        scenario.kernel.control("approve", "fork-head", subject=scenario.delivery.key, world=changed)
    assert not any(write["operation"] in {"merge", "close"} for write in scenario.port.writes())
