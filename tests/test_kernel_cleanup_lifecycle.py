"""Lifecycle resource observations grounded in the unchanged 02 cleanup callers.

Sources: delivery_cleanup.complete_ticket/complete_parent/complete_final_run;
test_cleanup_head_protection.test_every_completed_cleanup_uses_source_head_instead_of_merge_result;
test_ticket_closeout_does_not_attempt_another_ticket_cleanup. The independent
75c neutral observation additionally records absent Ticket local/published refs
and D/R/P checkouts. These tests are candidate regression probes, not a claim
that three new Scenario runs are an old/new same-input differential execution.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

import pytest

from agent_run_kernel import DeliveryKind, Effect, Kernel
from agent_run_kernel.offline import Scenario


CLEANUP_OPERATIONS = ("cleanup_worktree", "cleanup_local_ref", "cleanup_remote_ref")


def _local_head(scenario: Scenario, ref: str) -> str | None:
    output = scenario.port.git.run("for-each-ref", "--format=%(refname)%09%(objectname)", ref).stdout
    return next((line.partition("\t")[2] for line in output.splitlines()
                 if line.partition("\t")[0] == ref), None)


@pytest.mark.parametrize("kind", list(DeliveryKind))
def test_completed_delivery_retires_only_its_recorded_resources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: DeliveryKind,
) -> None:
    scenario = Scenario(tmp_path / "scenario", kind)
    main = "refs/heads/main"
    main_before = scenario.port.git.commit(main)
    sibling = "refs/heads/other-active-ticket"
    scenario.port.git.run("update-ref", sibling, main_before.sha)
    checkout = Path(scenario.delivery.checkout)
    actual_execute = scenario.port.execute
    retired: list[Effect] = []

    def execute(effect: Effect, authorize: Callable[[], None] | None = None) -> None:
        if effect.operation in CLEANUP_OPERATIONS:
            # Cleanup cannot be used as a substitute for durable business closeout.
            fresh = Kernel(scenario.port.root / "kernel.sqlite3")
            assert fresh.status(scenario.delivery.key)["phase"] == "complete"
            retired.append(effect)
            if effect.operation == "cleanup_worktree":
                assert Path(effect.payload["checkout"]) == checkout
                assert effect.payload["expected_head"] == scenario.port.git.run(
                    "rev-parse", "HEAD", cwd=checkout,
                ).stdout.strip()
            else:
                pr = scenario.port.pull_request(scenario.delivery.key)
                assert pr is not None and pr.merged is not None
                assert effect.payload["branch_ref"] == scenario.delivery.source_ref
                assert effect.payload["expected_head"] == pr.head.sha
                if kind != DeliveryKind.TICKET:
                    assert pr.head.sha != pr.merged.sha
        actual_execute(effect, authorize)

    monkeypatch.setattr(scenario.port, "execute", execute)
    assert scenario.run(approve=True).kind == "complete"
    assert [effect.operation for effect in retired] == list(CLEANUP_OPERATIONS)
    assert not checkout.exists()
    registry = scenario.port.git.run("worktree", "list", "--porcelain", "-z").stdout.split("\0")
    assert f"worktree {checkout}" not in registry
    assert _local_head(scenario, scenario.delivery.source_ref) is None
    assert _local_head(scenario, sibling) == main_before.sha
    pr = scenario.port.pull_request(scenario.delivery.key)
    assert pr is not None and pr.merged is not None
    assert scenario.port.git.commit(scenario.delivery.target_ref) == pr.merged
    if kind == DeliveryKind.TICKET:
        assert scenario.port.git.commit(main) == main_before
    for effect in retired:
        assert scenario.port.reconcile(effect).disposition == "target"
    calls_before = list(scenario.role_calls)
    writes_before = scenario.port.writes()
    scenario.kernel = Kernel(scenario.port.root / "kernel.sqlite3")
    assert scenario.run(approve=True).kind == "complete"
    assert scenario.role_calls == calls_before
    assert scenario.port.writes() == writes_before
