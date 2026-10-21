"""Behavioral probes for the isolated replacement kernel, never legacy fixtures."""

from pathlib import Path
import json
import subprocess
import sys

import pytest

from agent_run_kernel import Commit, Delivery, DeliveryKind, Kernel, Policy, RunSpec, World
from agent_run_kernel.offline import Scenario


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


@pytest.mark.parametrize(("kind", "expected_roles", "development_purpose"), [
    (DeliveryKind.TICKET, ["development", "review", "development", "review", "publication"], "requirements"),
    (DeliveryKind.PARENT_ONLY, ["development", "review", "development", "review", "publication"], "requirements"),
    (DeliveryKind.FINAL_RUN, ["review", "development", "review", "publication"], "integration"),
])
def test_three_deliveries_complete_with_required_review_and_exact_git_history(
    tmp_path: Path, kind: DeliveryKind, expected_roles: list[str], development_purpose: str,
) -> None:
    scenario = Scenario(tmp_path / "scenario", kind, reviews=("fail", "pass"))
    original_target = scenario.port.git.commit(scenario.delivery.target_ref)
    boundary = scenario.run()
    assert boundary.kind == ("complete" if kind == DeliveryKind.TICKET else "approval")
    if kind != DeliveryKind.TICKET:
        assert scenario.port.git.commit(scenario.delivery.target_ref) == original_target
        scenario.kernel.control("approve", "exact-approval", subject=scenario.delivery.key, world=scenario.world())
        boundary = scenario.run()
    assert boundary.kind == "complete"
    assert [call["role"] for call in scenario.role_calls] == expected_roles
    assert {call["purpose"] for call in scenario.role_calls if call["role"] == "development"} == {development_purpose}
    assert sum(write["operation"] == "pr_write" for write in scenario.port.writes()) == 1
    assert sum(write["operation"] == "merge" for write in scenario.port.writes()) == 1
    assert sum(write["operation"] == "completion_comment" for write in scenario.port.writes()) == 1
    target = scenario.port.git.commit(scenario.delivery.target_ref)
    assert len(scenario.port.git.parents(target.sha)) == (1 if kind == DeliveryKind.TICKET else 2)
    assert scenario.kernel.status(scenario.delivery.key)["phase"] == "complete"


def test_ticket_final_development_is_explicit_fallback_but_parent_cannot_skip_review(tmp_path: Path) -> None:
    ticket = Scenario(tmp_path / "ticket", DeliveryKind.TICKET, reviews=("fail",), policy=Policy(ticket_rounds=1))
    assert ticket.run().kind == "complete"
    assert [call["role"] for call in ticket.role_calls] == ["development", "review", "development", "publication"]
    assert ticket.kernel.status(ticket.delivery.key)["gate_mode"] == "deterministic_fallback"
    parent = Scenario(tmp_path / "parent", DeliveryKind.PARENT_ONLY, reviews=("fail",), policy=Policy(parent_rounds=1))
    assert parent.run().kind == "paused"
    assert "review_budget_exhausted" in parent.kernel.status(parent.delivery.key)["gates"]
    assert not any(write["operation"] == "pr_write" for write in parent.port.writes())


@pytest.mark.parametrize("kind", list(DeliveryKind))
def test_ci_uses_separate_cumulative_development_and_parent_review(tmp_path: Path, kind: DeliveryKind) -> None:
    scenario = Scenario(tmp_path / "scenario", kind, checks=("fail", "none"))
    assert scenario.run(approve=True).kind == "complete"
    ci = [call for call in scenario.role_calls if call["purpose"] == "ci"]
    assert [call["role"] for call in ci if call["role"] != "publication"] == (
        ["development"] if kind == DeliveryKind.TICKET else ["development", "review"])
    assert scenario.kernel.status(scenario.delivery.key)["budgets"]["ci"]["developments_used"] == 1
    assert sum(write["operation"] == "pr_write" for write in scenario.port.writes()) == 2
    if kind == DeliveryKind.FINAL_RUN:
        assert {write["subject"] for write in scenario.port.writes()} == {scenario.delivery.key}


def test_stop_late_raw_is_settlement_only_and_independent_process_can_read_it(tmp_path: Path) -> None:
    path = tmp_path / "kernel.sqlite3"
    kernel = Kernel.create(path, ticket_spec())
    token = kernel.claim_executor("executor-one")
    world = World("parent-v1", "graph-v1", "ticket-v1", Commit("base", "base-tree"))
    step = kernel.advance("ticket:2", token, world)
    assert step.call is not None
    kernel.control("stop", "stop-original")
    kernel.receive(step.call, b'{"result_kind":"development","summary":"retained","human_blockers":null}')
    assert kernel.advance("ticket:2", token, world).kind == "paused"
    command = "from pathlib import Path; from agent_run_kernel import Kernel; import json,sys; print(json.dumps(Kernel(Path(sys.argv[1])).status('ticket:2')))"
    process = subprocess.run([sys.executable, "-c", command, str(path)], text=True, capture_output=True, check=True, timeout=15)
    observed = json.loads(process.stdout)
    assert observed["stopped"] is True
    assert observed["pending_return"] is True
    assert observed["phase"] == "development"
    assert observed["budgets"]["requirements"]["developments_used"] == 1
    kernel.control("resume", "resume-original")
    assert Kernel(path).advance("ticket:2", token, world, allow_agent=False).kind == "progress"
    assert kernel.status("ticket:2")["budgets"]["requirements"]["developments_used"] == 1


def test_unknown_successful_pr_write_is_read_back_without_a_second_write(tmp_path: Path) -> None:
    scenario = Scenario(tmp_path / "scenario", DeliveryKind.TICKET)
    scenario.port.lose_response.add("pr_write")
    assert scenario.run().kind == "complete"
    assert sum(write["operation"] == "pr_write" for write in scenario.port.writes()) == 1
    assert scenario.publication_count == 1

