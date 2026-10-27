"""Public Approve admits ordinary recovery without authorizing a stale merge."""
from dataclasses import replace
import json
from pathlib import Path

import pytest

from agent_run.adapters.git_objects import LocalGit
import agent_run.cli.lifecycle as lifecycle
from agent_run.cli.main import _parser
from agent_run.core.kernel import Kernel
from agent_run.core.operation_runner import OperationRunner
from agent_run.errors import KernelError
from agent_run.model import Checks, DeliveryKind, Execution
from support.offline import Scenario
from support.core_operations import prepare_scripted_agent
from support.control_scenarios import managed_approval
from support.control_scenarios import managed_parent
from support.remote_effects import _change


@pytest.mark.parametrize("change", ["target", "ci"])
@pytest.mark.parametrize("tickets", [(), (2,)])
def test_idle_public_approve_starts_existing_recovery_without_a_merge_grant(tmp_path, monkeypatch, change, tickets):
    kernel, _old_runner, physical, state, calls, original, subject, _source = managed_approval(
        tmp_path, monkeypatch, tickets=tickets)
    fake = tmp_path / "transport" / "bin" / "gh"
    # GitHub moves an open PR's head after a real push. This older transport
    # fixture otherwise leaves the creation-time SHA frozen indefinitely.
    fake.write_text(fake.read_text().replace("if args[:2]==['repo','view']:",
        "for pr in d.get('prs', []):\n"
        "    if pr['state']=='open': pr['head']['sha']=head('refs/heads/'+pr['head']['ref'])\n"
        "if args[:2]==['repo','view']:"))
    before = kernel.status(subject)
    completions = kernel.source_status()["completions"]
    observed = kernel.host_usage("original-host")
    assert observed is not None
    assert kernel.observe_host(original, replace(observed, disposition="exited", exit_status=0,
        ended_at=111.0, usage_ready=False, usage_released=True)) == "exited"
    assert kernel.active_host_action() is None and kernel.operation_state().active == ()
    if change == "target":
        external = tmp_path / "external"
        physical.git.run("clone", "--branch", "main", str(tmp_path / "transport" / "origin.git"), str(external))
        git = LocalGit(external)
        git.run("config", "user.name", "Independent contributor")
        git.run("config", "user.email", "independent@example.invalid")
        (external / "target-append.txt").write_text("Independent target change\n")
        git.run("add", "target-append.txt")
        git.run("commit", "-m", "Advance the legitimate target")
        git.run("push", "origin", "HEAD:refs/heads/main")
    else:
        _change(state, checks=[{"name": "required", "workflow": "CI", "bucket": "fail",
            "state": "FAILURE", "link": "https://ci.example.invalid/failure", "description": "Original failure"}])
    requests = []

    def admit(selected, kind, *, action_id, command, manager, environment, runner_lock, **intent):
        # Only the physical handoff is controlled. Core admission, Action and
        # all subsequent selection/reads/Git operations are the real interfaces.
        request = selected.admit_host(action_id, kind=kind, command=command,
            environment_carrier=tmp_path / "recovery-carrier", preflight=manager.preflight, **intent)
        assert selected.admit_host(action_id, kind=kind, command=command,
            environment_carrier=tmp_path / "recovery-carrier", preflight=manager.preflight, **intent) == request
        requests.append(request)
        with selected.storage.transaction(write=False) as tx:
            status = tx.one("SELECT status FROM actions WHERE id=?", (action_id,))[0]
        return {"result": "accepted", "status": "action_" + status, "run_id": request.run_id,
                "action_id": action_id, "action": kind, "host": "unknown"}

    monkeypatch.setattr(lifecycle, "submit", admit)
    arguments = ["approve", original.run_id, "--repo", physical.repository,
                 "--state-dir", str(kernel.storage.path), "--json"]
    writes = [line for line in calls.read_text().splitlines() if "--method" in json.loads(line)]
    result = lifecycle.execute(_parser("en").parse_args(arguments), language="en")
    assert result["status"] == "action_updating" and len(requests) == 1
    assert [line for line in calls.read_text().splitlines() if "--method" in json.loads(line)] == writes
    with kernel.storage.transaction(write=False) as tx:
        assert not tx.rows("SELECT id FROM grants WHERE subject=?", (subject,))
        assert tx.one("SELECT final_action FROM runs")[0] is None
        action = tx.one("SELECT kind,subject,status,receipt_id FROM actions WHERE id=?", (result["action_id"],))
        assert tuple(action)[:3] == ("approve", subject, "updating") and action["receipt_id"] is not None
    request, = requests
    # A lost caller response reopens the same durable request; it neither
    # reapplies the control nor allocates another execution identity.
    assert Kernel(kernel.storage.path, run_id=kernel.storage.run_id).host_request(result["action_id"]) == request
    assert kernel.dispatch_host(request) and not kernel.dispatch_host(request)
    running = replace(observed, unit_name=request.unit_name, invocation_id="recovery-" + request.nonce)
    assert kernel.observe_host(request, running) == "running"
    if change == "target" and not tickets:
        assert kernel.stop_task("stop-the-update") == "applied"
        assert kernel.source_status()["stopped"] is True
        with pytest.raises(KernelError, match="executor_exit_unproven"):
            kernel.admit_host("premature-resume", kind="resume", command=("host",),
                environment_carrier=tmp_path / "premature", preflight=lambda: None)
        assert kernel.observe_host(request, replace(running, disposition="exited", exit_status=0,
            ended_at=112.0, usage_ready=False, usage_released=True)) == "exited"
        resumed = lifecycle.execute(_parser("en").parse_args(["resume", *arguments[1:]]), language="en")
        assert resumed["status"] == "action_applied"
        request = requests[-1]
        assert kernel.dispatch_host(request)
        assert kernel.observe_host(request, replace(running, unit_name=request.unit_name,
            invocation_id="resumed-" + request.nonce)) == "running"
    execution = Execution(request.run_id, request.owner, request.epoch)
    runner = OperationRunner(kernel, execution, lambda binding: physical)
    runner.activate_observation()
    assert runner.state().disposition == "ready"
    agents = Scenario(tmp_path / "recovery-agent", DeliveryKind.PARENT_ONLY,
                      edits=({"recovery.txt": "Actual controlled repair\n"},))
    agents.kernel, agents.port.git = kernel, physical.git
    ci_calls = []
    for _ in range(180):
        step = prepare_scripted_agent(kernel, runner.run_once())
        assert step.kind not in {"paused", "waiting", "complete"}, step
        if step.call is not None:
            assert step.call.subject == subject
            runner.authorize_agent(step.call)
            if step.call.role == "development" and step.call.purpose == "ci":
                ci_calls.append(step.call)
                assert step.call.context["ci_evidence"]["result"] == "fail"
                _change(state, checks=[])
            kernel.receive(step.call, agents.agent(step.call))
        if kernel.status(subject)["phase"] == "approval":
            assert runner.state().disposition == "quiescent"
            break
    else:
        raise AssertionError("Ordinary recovery never returned to fresh approval")
    after = kernel.status(subject)
    assert kernel.source_status()["completions"] == completions
    assert after["generation"] == before["generation"] and after["pr_number"] == before["pr_number"]
    assert after["phase"] == "approval" and after["budgets"]["requirements"] == before["budgets"]["requirements"]
    assert agents.review_count >= 1
    assert len(ci_calls) == (1 if change == "ci" else 0)
    with kernel.storage.transaction(write=False) as tx:
        assert not tx.rows("SELECT id FROM grants WHERE subject=?", (subject,))
        assert not tx.rows("SELECT id FROM effects WHERE subject=? AND operation='merge'", (subject,))
    if change == "ci":
        assert after["budgets"]["ci"]["developments_used"] == before["budgets"]["ci"]["developments_used"] + 1


def test_approval_recovery_rejects_inapplicable_facts_without_an_action_or_grant(tmp_path):
    scenario = managed_parent(tmp_path)
    assert scenario.run().kind == "approval"
    kernel, subject = scenario.kernel, scenario.delivery.key
    world = scenario.world()
    assert world.pr is not None

    def snapshot():
        with kernel.storage.transaction(write=False) as tx:
            actions = [tuple(row) for row in tx.all("SELECT * FROM actions ORDER BY rowid")]
        return kernel.status(subject), kernel.history(subject), actions

    before = snapshot()
    invalid_checks = [None, Checks(world.pr.number, world.pr.head.sha, "pending"),
        Checks(world.pr.number, world.pr.head.sha, "unknown"),
        Checks(world.pr.number, "wrong-head", "fail", (("required", "fail"),))]
    for index, checks in enumerate(invalid_checks):
        with pytest.raises(KernelError, match="approval_checks"):
            kernel.control("approve", f"invalid-checks-{index}", subject=subject, world=replace(world, checks=checks))
        assert snapshot() == before
    scenario.port.external_append(scenario.delivery.target_ref, {"external.txt": "new target\n"}, "allowed-append")
    fresh = scenario.world()
    assert fresh.pr is not None
    for index, invalid in enumerate((replace(fresh, ancestry=()),
            replace(fresh, pr=replace(fresh.pr, number=fresh.pr.number + 1)),
            replace(fresh, revision="different-requirement"))):
        with pytest.raises(KernelError, match="approval_currentness"):
            kernel.control("approve", f"invalid-update-{index}", subject=subject, world=invalid)
        assert snapshot() == before
    with kernel.storage.transaction(write=False) as tx:
        assert not tx.rows("SELECT id FROM grants")
        assert tx.one("SELECT final_action FROM runs")[0] is None


def test_checks_failure_after_an_accepted_merge_still_uses_the_original_recovery(tmp_path):
    scenario = Scenario(tmp_path / "accepted", DeliveryKind.PARENT_ONLY)
    assert scenario.run().kind == "approval"
    kernel, subject = scenario.kernel, scenario.delivery.key
    assert kernel.control("approve", "original-grant", subject=subject, world=scenario.world()) == "pending"
    scenario.checks[:] = ["fail"]
    assert scenario.step().kind == "progress"
    assert kernel.status(subject)["phase"] == "checks"
    with kernel.storage.transaction(write=False) as tx:
        assert tx.one("SELECT status FROM actions WHERE id='original-grant'")[0] == "updating"
        assert tx.one("SELECT final_action FROM runs")[0] is None
        assert tx.one("SELECT grant_id FROM subjects WHERE id=?", (subject,))[0] is None
        assert not tx.rows("SELECT id FROM effects WHERE operation='merge'")
    assert scenario.step().kind == "progress"
    repaired = kernel.status(subject)
    assert repaired["phase"] == "development" and repaired["purpose"] == "ci"
    assert repaired["budgets"]["ci"]["developments_used"] == 0
    with kernel.storage.transaction(write=False) as tx:
        failure = tx.one("SELECT ci_failure FROM subjects WHERE id=?", (subject,))[0]
        assert json.loads(tx.get(failure, subject))["result"] == "fail"
