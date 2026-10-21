"""Frozen policy controls real domain allocation and continuation authority."""

from dataclasses import replace
from pathlib import Path

import pytest

from agent_run_kernel import Commit, Delivery, DeliveryKind, Kernel, KernelError, Policy, RunSpec, World


@pytest.mark.parametrize(("kind", "expected"), [(DeliveryKind.TICKET, 1), (DeliveryKind.PARENT_ONLY, 2), (DeliveryKind.FINAL_RUN, 2)])
def test_ci_allowance_uses_the_actual_ticket_or_parent_owner(tmp_path: Path, kind: DeliveryKind, expected: int) -> None:
    base = Commit("base", "tree")
    delivery = Delivery("delivery", kind, 1, "requirements", base, "refs/heads/source", "refs/heads/target")
    policy = Policy(ticket_ci_developments=1, parent_ci_developments=2)
    kernel = Kernel.create(tmp_path / "run.sqlite3", RunSpec("run", "example/project", 1, "parent", "graph", (delivery,), policy))
    assert kernel.status(delivery.key)["budgets"]["ci"]["development_limit"] == expected


def test_frozen_call_deadline_cannot_be_extended_or_used_after_expiry(tmp_path: Path) -> None:
    base = Commit("base", "tree")
    delivery = Delivery("ticket:3", DeliveryKind.TICKET, 3, "requirements", base, "refs/heads/source", "refs/heads/target")
    policy = Policy(development_deadline_seconds=7.0, review_deadline_seconds=11.0, publication_deadline_seconds=13.0)
    kernel = Kernel.create(tmp_path / "run.sqlite3", RunSpec("run", "example/project", 1, "parent", "graph", (delivery,), policy))
    now = [100.25]
    kernel.clock = lambda: now[0]
    executor = kernel.claim_executor("executor")
    call = kernel.advance(delivery.key, executor, World("parent", "graph", "requirements", base)).call
    assert call is not None and call.deadline_seconds == 7.0 and call.deadline_at == 107.25
    with pytest.raises(KernelError, match="invocation_binding"):
        kernel.dispatch_agent(replace(call, deadline_at=108.25))
    assert kernel.dispatch_agent(call)
    now[0] = 107.25
    with pytest.raises(KernelError, match="invocation_deadline_exceeded"):
        kernel.guard_agent(call)
    assert kernel.status(delivery.key)["budgets"]["requirements"]["developments_used"] == 1
