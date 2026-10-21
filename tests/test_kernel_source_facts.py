"""External readers consume observed identities without duplicating decisions."""

from dataclasses import replace
from pathlib import Path

import pytest

from agent_run_kernel import Commit, Delivery, DeliveryKind, Kernel, KernelError, RunSpec, World
from agent_run_kernel.model import Issue, ParentIssue, Requirements
from agent_run_kernel.offline import Scenario


def test_refresh_receipt_preserves_observed_issue_and_completion_identities(tmp_path: Path) -> None:
    source = Requirements(ParentIssue(1, "Parent", "Whole requirement", (2,)),
                          (Issue(2, "Ticket", "Initial requirement", "OPEN", frozenset({"ready-for-agent"})),))
    base = Commit("base", "tree")
    deliveries = (Delivery("ticket:2", DeliveryKind.TICKET, 2, "untrusted", base, "ticket", "run"),
                  Delivery("final:1", DeliveryKind.FINAL_RUN, 1, "untrusted", base, "run", "main"))
    kernel = Kernel.create(tmp_path / "kernel.sqlite3", RunSpec("run", "example/repository", 1, "untrusted", "untrusted", deliveries), requirements=source)
    initial = kernel.source_status()
    revision = kernel.delivery("ticket:2").revision
    world = World(initial["parent_revision"], initial["graph_revision"], revision, base)
    assert kernel.advance("ticket:2", kernel.claim_executor("test-owner"), world).call is not None
    changed = replace(source, issues=(replace(source.issues[0], body="New exact requirement"),))
    receipt = kernel.refresh(lambda: changed)
    observed = receipt.get("observed_issues")
    assert isinstance(observed, list) and len(observed) == 1
    assert observed[0]["issue"] == 2 and observed[0]["revision"] != revision
    assert observed[0]["revision"] == kernel.status("ticket:2")["source_revision"]
    assert receipt.get("completion_revisions") == [{"subject": "ticket:2", "generation": 1, "completion_revision": "no-tickets"}]
    assert Kernel(kernel.storage.path).source_status() == receipt


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
