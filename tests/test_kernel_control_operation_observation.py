"""Manual managed controls share Core observation without an Executor claim."""
from dataclasses import replace
import json
import os
from pathlib import Path

import pytest

from agent_run.user_defaults import UserDefaultsStore
from agent_run_kernel import Kernel, KernelError
from agent_run_kernel import lifecycle_cli
from agent_run_kernel.model import DeliveryKind, Execution
from agent_run_kernel.offline import Scenario
from agent_run_kernel.operation_runner import ReadUnavailable
from agent_run_kernel.query_cli import _parser
from agent_run_kernel.remote_effects import RemoteEffects
from test_kernel_host_adapter import controlled_manager
from test_kernel_operation_runner import setup
from test_kernel_remote_effects import _fixture


def test_control_observation_never_claims_executor_or_allocates_work(tmp_path, monkeypatch):
    scenario, ports, runner = setup(tmp_path)
    kernel = scenario.kernel
    kernel.control("stop", "stop-original")
    kernel.confirm_executor_exit(scenario.execution)
    before = kernel.status("ticket:3")
    with kernel.storage.transaction(write=False) as tx:
        owner = tuple(tx.one("SELECT executor,epoch FROM runs"))
        counts = tuple(tx.one("SELECT (SELECT count(*) FROM attempts),(SELECT count(*) FROM leases)"))
    monkeypatch.setattr(kernel, "claim_executor", lambda *a, **kw: pytest.fail("observation must not claim"))
    world = kernel.observe_control("ticket:3", lambda binding: ports)
    assert world.target == ports.scenario.port.remote_commit("refs/heads/run")
    after = kernel.status("ticket:3")
    assert after["phase"] == before["phase"] == "workspace"
    assert after["budgets"] == before["budgets"] and after["stopped"]
    with kernel.storage.transaction(write=False) as tx:
        assert tuple(tx.one("SELECT executor,epoch FROM runs")) == owner
        assert tuple(tx.one("SELECT (SELECT count(*) FROM attempts),(SELECT count(*) FROM leases)")) == counts


@pytest.mark.parametrize("change", ["source", "epoch", "stop"])
def test_control_observation_rejects_changes_during_physical_reads(tmp_path, change):
    scenario, ports, runner = setup(tmp_path)
    kernel = scenario.kernel
    read = ports.delivery

    def changed(request):
        facts = read(request)
        if change == "source":
            kernel.refresh(lambda: replace(scenario.requirements, parent=replace(scenario.requirements.parent, body="Changed actual requirement")))
        elif change == "epoch":
            kernel.control("stop", "stop-old-owner")
            kernel.confirm_executor_exit(scenario.execution)
            kernel.control("resume", "resume-new-owner")
            kernel.claim_executor("new-owner")
        else:
            kernel.control("stop", "stop-in-read")
        return facts

    ports.delivery = changed
    with pytest.raises(KernelError, match="automatic_step_superseded"):
        kernel.observe_control("ticket:3", lambda binding: ports)


def test_control_read_failure_is_a_cli_error_without_a_work_wait_or_claim(tmp_path):
    scenario, ports, runner = setup(tmp_path)

    def unavailable(parent):
        raise ReadUnavailable("github_read_failed", "Actual source unavailable")

    ports.source = unavailable
    with pytest.raises(KernelError, match="github_read_failed"):
        scenario.kernel.observe_control("ticket:3", lambda binding: ports)
    with scenario.kernel.storage.transaction(write=False) as tx:
        assert not tx.rows("SELECT id FROM attempts")
        assert not tx.rows("SELECT id FROM leases")
        assert not tx.rows("SELECT subject FROM waits")


def managed_approval(tmp_path, monkeypatch, *, tickets):
    transport = tmp_path / "transport"
    transport.mkdir()
    port, git, push, state, calls = _fixture(transport, monkeypatch)
    fake = transport / "bin" / "gh"
    fake.write_text(fake.read_text().replace("'number':17,", "'number':17+len(d['prs']),"))
    graph = {"parent": {"number": 1, "title": "Parent", "body": "Original requirement", "sub_issues": list(tickets)},
             "issues": {str(n): {"number": n, "title": "Ticket", "body": "Ticket requirement", "state": "OPEN",
                                    "labels": ["ready-for-agent"]} for n in tickets}}
    state.write_text(json.dumps({"prs": [], "comments": [], "events": [], "issue_state": "open", "source_graph": graph}))
    defaults = UserDefaultsStore()
    defaults.configure(language="en", profile={"preset": "economy"})
    database, spec, source = lifecycle_cli.prepare_creation(1, "example/project", source_directory=git.root,
        defaults=defaults, remote_url=str(transport / "origin.git"))
    kernel = Kernel.create_task(database, spec, requirements=source)
    manager_root = tmp_path / "manager"
    manager_root.mkdir()
    manager, _, response, manager_calls = controlled_manager(manager_root)
    request = kernel.admit_host("original-host", kind="run", command=("fixture-native-host",),
        environment_carrier=manager_root / "original-carrier", preflight=manager.preflight)
    assert kernel.dispatch_host(request)
    document = json.loads(response.read_text())
    group = next(line[3:] for line in Path(f"/proc/{os.getpid()}/cgroup").read_text().splitlines() if line.startswith("0::"))
    document["fields"].update(Id=request.unit_name, Description=request.description,
        ActiveState="active", SubState="running", MainPID=str(os.getpid()), ControlGroup=group)
    response.write_text(json.dumps(document))
    observed = manager.observe(request)
    assert observed.disposition == "running"
    assert kernel.observe_host(request, replace(observed, usage_ready=True)) == "running"
    execution = Execution(request.run_id, request.owner, request.epoch)
    physical = RemoteEffects(Path(spec.repository_root), spec.repository, runner_identity=spec.inputs.runner_identity)
    runner = kernel.operation_runner(execution, lambda binding: physical)
    # This existing fake Agent only makes files/raw bytes. All source, Git, PR,
    # Completion and operation decisions above/below remain production paths.
    agents = Scenario(tmp_path / "agents", DeliveryKind.PARENT_ONLY)
    agents.kernel, agents.port.git = kernel, physical.git
    subject = "final:1" if tickets else "parent:1"
    for _ in range(180):
        step = runner.run_once()
        if step.call is not None:
            runner.authorize_agent(step.call)
            kernel.receive(step.call, agents.agent(step.call))
        if kernel.source_status()["final_subject"] == subject or not tickets:
            status = kernel.status(subject)
            assert not status["gates"], (step, status["gates"])
            if status["phase"] == "approval":
                break
    else:
        raise AssertionError("The managed Run never reached its real approval")
    monkeypatch.setattr(lifecycle_cli, "_active_runner", lambda: True)
    import agent_run_kernel.host_adapter as adapter
    monkeypatch.setattr(adapter, "SystemdHost", lambda **kwargs: manager)
    return kernel, runner, physical, state, calls, request, subject, source


@pytest.mark.parametrize("command", ["approve", "revise", "requeue"])
def test_managed_control_observes_without_replacing_a_running_host(tmp_path, monkeypatch, command):
    kernel, runner, physical, state, calls, request, subject, source = managed_approval(
        tmp_path, monkeypatch, tickets=(2,) if command == "revise" else ())
    if command == "requeue":
        document = json.loads(state.read_text())
        document["source_graph"]["parent"]["body"] = "Changed authoritative requirement"
        state.write_text(json.dumps(document))
    before = kernel.status(subject)
    with kernel.storage.transaction(write=False) as tx:
        owner = tuple(tx.one("SELECT executor,epoch FROM runs"))
        counts = tuple(tx.one("SELECT (SELECT count(*) FROM attempts),(SELECT count(*) FROM leases)"))
    writes = [line for line in calls.read_text().splitlines() if "--method" in json.loads(line)]
    monkeypatch.setattr(Kernel, "claim_executor", lambda *a, **kw: pytest.fail("control observation must not claim"))
    arguments = [command, request.run_id, "--repo", physical.repository, "--state-dir", str(kernel.storage.path), "--json"]
    if command == "revise":
        arguments.extend(["--message", " Preserve the requested revision "])
    result = lifecycle_cli.execute(_parser("en").parse_args(arguments))
    assert result["result"] == "accepted" and result["host"] == "running"
    assert result["action"] == command
    assert result["status"] == ("action_applied" if command == "revise" else "action_pending")
    assert kernel.host_request("original-host") == request
    assert [line for line in calls.read_text().splitlines() if "--method" in json.loads(line)] == writes
    with kernel.storage.transaction(write=False) as tx:
        assert tuple(tx.one("SELECT executor,epoch FROM runs")) == owner
        assert tuple(tx.one("SELECT (SELECT count(*) FROM attempts),(SELECT count(*) FROM leases)")) == counts
        action = tx.one("SELECT kind,subject FROM actions WHERE id=?", (result["action_id"],))
        assert tuple(action) == (command, subject)
        if command == "approve":
            grant = tx.one("SELECT action,head_sha,pr_number FROM grants WHERE subject=?", (subject,))
            assert tuple(grant) == (result["action_id"], before["source_sha"], before["pr_number"])
        elif command == "revise":
            assert kernel.status(subject)["phase"] == "development"
            assert not tx.rows("SELECT id FROM grants WHERE subject=?", (subject,))
        else:
            assert kernel.status(subject)["generation"] == before["generation"]
            assert kernel.status(subject)["pending_write"] is None
