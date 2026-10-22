"""A pending requeue may read its own facts without acquiring write authority."""
from dataclasses import replace
from pathlib import Path

import pytest

from agent_run_kernel import Execution, Kernel, KernelError
from test_kernel_exploration import ticket_spec


def pending_requeue(tmp_path: Path) -> tuple[Kernel, Execution]:
    spec = ticket_spec()
    sibling = replace(spec.deliveries[0], key="ticket:3", issue=3)
    kernel = Kernel.create(tmp_path / "kernel.sqlite3", replace(spec, deliveries=(*spec.deliveries, sibling)))
    execution = kernel.claim_executor("original")
    with kernel.storage.transaction() as tx:
        tx.execute("UPDATE runs SET blocked='requeue_required'")
        tx.execute("INSERT INTO gates(subject,code) VALUES ('ticket:2','requeue_required')")
        tx.execute("INSERT INTO actions(id,run_id,kind,subject,status) VALUES ('requeue-original',?,'requeue','ticket:2','pending')",
            (spec.run_id,))
    return kernel, execution


def test_pending_requeue_can_read_and_keep_one_bounded_read_outage(tmp_path: Path) -> None:
    kernel, execution = pending_requeue(tmp_path)
    token = kernel.begin_automatic_step(execution, "ticket:2")
    assert kernel.guard_automatic_step(token, execution) is None
    assert kernel.guard_automatic_step(token, execution, after_read=True) is None
    waiting = kernel.wait_for_read(token, execution, "github_unavailable", "Synthetic read outage")
    assert (waiting.kind, waiting.reason) == ("waiting", "github_convergence")
    assert kernel.status("ticket:2")["gates"] == ["requeue_required"]


def test_requeue_read_outage_keeps_its_original_deadline_across_restart(tmp_path: Path) -> None:
    kernel, execution = pending_requeue(tmp_path)
    kernel.clock = lambda: 100.25
    token = kernel.begin_automatic_step(execution, "ticket:2")
    assert kernel.wait_for_read(token, execution, "github_unavailable", "First outage").retry_at == 105.25
    kernel.clock = lambda: 200.25
    kernel.wait_for_read(token, execution, "github_unavailable", "Repeated outage")
    reopened = Kernel(kernel.storage.path)
    reopened.clock = lambda: 700.25
    token = reopened.begin_automatic_step(execution, "ticket:2")
    expired = reopened.guard_automatic_step(token, execution)
    assert expired is not None and (expired.kind, expired.reason) == ("paused", "supervision_timeout")
    assert "supervision_timeout" in reopened.status("ticket:2")["gates"]


@pytest.mark.parametrize("blocked", ["sibling", "cleanup", "other_gate", "other_block"])
def test_requeue_read_permission_never_opens_other_work(tmp_path: Path, blocked: str) -> None:
    kernel, execution = pending_requeue(tmp_path)
    if blocked in {"other_gate", "other_block"}:
        with kernel.storage.transaction() as tx:
            if blocked == "other_gate":
                tx.execute("INSERT INTO gates(subject,code) VALUES ('ticket:2','human_blocked')")
            else:
                tx.execute("UPDATE runs SET blocked='final_refresh'")
    subject = "ticket:3" if blocked == "sibling" else "ticket:2"
    token = kernel.begin_automatic_step(execution, subject, cleanup=blocked == "cleanup")
    with pytest.raises(KernelError, match="automatic_step_paused"):
        kernel.guard_automatic_step(token, execution)


@pytest.mark.parametrize("change", ["stop", "replacement"])
def test_requeue_read_is_bound_to_the_same_action_after_external_io(tmp_path: Path, change: str) -> None:
    kernel, execution = pending_requeue(tmp_path)
    token = kernel.begin_automatic_step(execution, "ticket:2")
    if change == "stop":
        kernel.control("stop", "stop-during-read")
        error = "executor_fenced"
    else:
        with kernel.storage.transaction() as tx:
            tx.execute("UPDATE actions SET status='superseded' WHERE id='requeue-original'")
            tx.execute("INSERT INTO actions(id,run_id,kind,subject,status) VALUES ('requeue-replacement',?,'requeue','ticket:2','pending')",
                (execution.run_id,))
        error = "automatic_step_superseded"
    with pytest.raises(KernelError, match=error):
        kernel.guard_automatic_step(token, execution, after_read=True)
