"""Behavioral probes for the isolated replacement kernel, never legacy fixtures."""

from pathlib import Path

from agent_run_kernel import Commit, Delivery, DeliveryKind, Kernel, Policy, RunSpec, World


def ticket_spec() -> RunSpec:
    base = Commit("base", "base-tree")
    return RunSpec(
        "offline-run", "example/repository", 1, "parent-v1", "graph-v1",
        (Delivery("ticket:2", DeliveryKind.TICKET, 2, "ticket-v1", base,
                  "refs/heads/ticket-2", "refs/heads/run"),),
        Policy(ticket_rounds=1),
    )


def test_return_is_durable_before_consumption_and_resume_does_not_charge_twice(tmp_path: Path) -> None:
    path = tmp_path / "kernel.sqlite3"
    kernel = Kernel.create(path, ticket_spec())
    token = kernel.claim_executor("executor-one")
    world = World("parent-v1", "graph-v1", "ticket-v1", Commit("base", "base-tree"))
    first = kernel.advance("ticket:2", token, world)
    assert first.kind == "agent"
    assert first.call is not None
    assert first.call.role.value == "development"
    kernel.receive(first.call, b'{"result_kind":"development","summary":"changed","human_blockers":null}')

    reopened = Kernel(path)
    observed = reopened.status("ticket:2")
    assert observed["pending_return"] is True
    assert observed["budgets"]["requirements"]["developments_used"] == 1
    assert observed["phase"] == "development"
    consumed = reopened.advance("ticket:2", token, world, allow_agent=False)
    assert consumed.kind == "progress"
    assert reopened.status("ticket:2")["phase"] == "candidate"
    assert reopened.status("ticket:2")["budgets"]["requirements"]["developments_used"] == 1

