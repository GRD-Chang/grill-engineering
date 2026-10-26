"""Manual managed controls share Core observation without an Executor claim."""
from support.core_operations import prepare_scripted_agent
from agent_run.core.operation_runner import OperationRunner, observe_control
from dataclasses import replace
import json
import os
from pathlib import Path

import pytest

from agent_run.config.defaults import UserDefaultsStore
from agent_run.core.kernel import Kernel
from agent_run.errors import KernelError
import agent_run.cli.lifecycle as lifecycle_cli
from agent_run.model import DeliveryKind, Execution
from agent_run.errors import ObservationSuperseded
from support.offline import Scenario
from agent_run.core.operation_contract import ReadUnavailable
from agent_run.cli.main import _parser
from agent_run.adapters.remote import RemoteEffects
from support.systemd_manager import controlled_manager
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
    world = observe_control(kernel, "ticket:3", lambda binding: ports)
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
    with pytest.raises(ObservationSuperseded, match="automatic_step_superseded"):
        observe_control(kernel, "ticket:3", lambda binding: ports)


def test_control_read_failure_is_a_cli_error_without_a_work_wait_or_claim(tmp_path):
    scenario, ports, runner = setup(tmp_path)

    def unavailable(parent):
        raise ReadUnavailable("github_read_failed", "Actual source unavailable")

    ports.source = unavailable
    with pytest.raises(KernelError, match="github_read_failed"):
        observe_control(scenario.kernel, "ticket:3", lambda binding: ports)
    with scenario.kernel.storage.transaction(write=False) as tx:
        assert not tx.rows("SELECT id FROM attempts")
        assert not tx.rows("SELECT id FROM leases")
        assert not tx.rows("SELECT subject FROM waits")


def managed_approval(tmp_path, monkeypatch, *, tickets, language="en", interrupt_development=False):
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
    defaults.configure(language=language, profile={"preset": "economy"})
    database, spec, source = lifecycle_cli.prepare_creation(1, "example/project", source_directory=git.root,
        defaults=defaults, remote_url=str(transport / "origin.git"))
    kernel = Kernel.create_task(database, spec, requirements=source)
    manager_root = tmp_path / "manager"
    manager_root.mkdir()
    manager, _, response, manager_calls = controlled_manager(manager_root)
    request = kernel.admit_host("original-host", kind="run", command=("fixture-native-host",),
        environment_carrier=manager_root / "original-carrier", preflight=manager.preflight)
    assert kernel.dispatch_host(request)
    sealed = ""
    if interrupt_development:
        from agent_run.runtime.handoff import prepare_handoff
        from agent_run.runtime.package_lease import default_runner_lock_path, runner_usage_lease
        from agent_run.model import HostObservation
        manager_root.chmod(0o700)
        lock = default_runner_lock_path(dict(os.environ))
        with runner_usage_lease(lock) as terminal:
            handoff = prepare_handoff(request, {}, runner_lock=lock, terminal_lease=terminal, ttl_seconds=.5)
            try:
                sealed = json.dumps(handoff.receipt, sort_keys=True, separators=(",", ":"))
                assert kernel.observe_host(request, HostObservation("unknown", request.unit_name,
                    usage_pending=True, evidence=sealed)) == "unknown"
            finally:
                handoff.process.wait(timeout=3)
        assert not Path(f"/proc/{handoff.process.pid}").exists()
    document = json.loads(response.read_text())
    group = next(line[3:] for line in Path(f"/proc/{os.getpid()}/cgroup").read_text().splitlines() if line.startswith("0::"))
    document["fields"].update(Id=request.unit_name, Description=request.description,
        ActiveState="active", SubState="running", MainPID=str(os.getpid()), ControlGroup=group)
    response.write_text(json.dumps(document))
    observed = manager.observe(request)
    assert observed.disposition == "running"
    assert kernel.observe_host(request, replace(observed, usage_ready=True, evidence=sealed)) == "running"
    execution = Execution(request.run_id, request.owner, request.epoch)
    physical = RemoteEffects(Path(spec.repository_root), spec.repository, runner_identity=spec.inputs.runner_identity)
    runner = OperationRunner(kernel, execution, lambda binding: physical)
    # This existing fake Agent only makes files/raw bytes. All source, Git, PR,
    # Completion and operation decisions above/below remain production paths.
    agents = Scenario(tmp_path / "agents", DeliveryKind.PARENT_ONLY)
    agents.kernel, agents.port.git = kernel, physical.git
    subject = "final:1" if tickets else "parent:1"
    for _ in range(180):
        step = prepare_scripted_agent(kernel, runner.run_once())
        if step.call is not None:
            if interrupt_development:
                break
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
    import agent_run.adapters.systemd as adapter
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
    result = lifecycle_cli.execute(_parser("en").parse_args(arguments), language="en")
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


@pytest.mark.parametrize("proof", ["released", "fdstore", "populated", "foreign"])
def test_first_public_resume_applies_after_exact_systemd_exit(tmp_path, monkeypatch, proof):
    """Real Task/SQLite/guardian; manager and cgroup facts remain synthetic."""
    from contextlib import contextmanager
    kernel, runner, physical, _state, calls, request, subject, _source = managed_approval(
        tmp_path, monkeypatch, tickets=(), interrupt_development=True)
    before = kernel.status(subject)
    assert before["phase"] == "development" and before["gates"] == []
    with kernel.storage.transaction(write=False) as tx:
        original_attempt = tx.one("SELECT current_attempt FROM subjects WHERE id=?", (subject,))[0]
        original_actions = tx.one("SELECT count(*) FROM actions")[0]
    manager = lifecycle_cli._manager_for(kernel, _parser("en").parse_args(["resume", request.run_id]), dict(os.environ))
    response = tmp_path / "manager" / "manager-response.json"
    document = json.loads(response.read_text())
    document["fields"].update(ActiveState="inactive", SubState="dead", MainPID="0",
                              ControlPID="0", NFileDescriptorStore="1" if proof == "fdstore" else "0")
    if proof == "foreign":
        document["fields"]["InvocationID"] = "2" * 32
    response.write_text(json.dumps(document))
    group = manager.cgroup_root / document["fields"]["ControlGroup"].lstrip("/")
    group.mkdir(exist_ok=True, parents=True)
    (group / "cgroup.events").write_text("populated 1\n" if proof == "populated" else "populated 0\n")

    # No real systemd is present. The replacement manager only records launch;
    # bound the real guardian's unused startup, not any work/wait deadline.
    @contextmanager
    def controlled_handoff(*args, **kwargs):
        from agent_run.runtime.handoff import prepare_handoff
        handoff = prepare_handoff(*args, **kwargs, ttl_seconds=.5)
        try:
            yield handoff
        finally:
            handoff.process.wait(timeout=3)
    import agent_run.runtime.handoff as native
    monkeypatch.setattr(native, "handoff_scope", controlled_handoff)
    result = lifecycle_cli.execute(_parser("en").parse_args(
        ["resume", request.run_id, "--repo", physical.repository, "--state-dir", str(kernel.storage.path), "--json"]), language="en")
    if proof != "released":
        assert result["result"] == "attached" and result["requested_action_applied"] is False
        assert kernel.host_usage("original-host").usage_released is False
        with kernel.storage.transaction(write=False) as tx:
            assert tx.one("SELECT count(*) FROM actions")[0] == original_actions
        return
    assert result["result"] == "accepted" and result["action"] == "resume"
    assert result["action_id"] != "original-host"
    assert kernel.host_usage("original-host").usage_released is True
    assert kernel.host_dispatched(result["action_id"])
    assert kernel.status(subject)["budgets"] == before["budgets"]
    assert kernel.status(subject)["gates"] == []
    with kernel.storage.transaction(write=False) as tx:
        assert tx.one("SELECT current_attempt FROM subjects WHERE id=?", (subject,))[0] == original_attempt
        assert tuple(tx.one("SELECT kind,status FROM actions WHERE id=?", (result["action_id"],))) == ("resume", "applied")
        assert tx.one("SELECT count(*) FROM actions")[0] == original_actions + 1
    launches = [json.loads(line) for line in (tmp_path / "manager" / "helper-calls.jsonl").read_text().splitlines()
                if json.loads(line)[0] == "systemd-run"]
    assert len(launches) == 1
    assert [line for line in calls.read_text().splitlines() if "--method" in json.loads(line)] == []

    again = lifecycle_cli.execute(_parser("en").parse_args(
        ["resume", request.run_id, "--repo", physical.repository, "--state-dir", str(kernel.storage.path), "--json"]), language="en")
    assert again["result"] == "attached" and again["requested_action_applied"] is False
    assert again["action_id"] == result["action_id"]
    launches_after = [json.loads(line) for line in (tmp_path / "manager" / "helper-calls.jsonl").read_text().splitlines()
                      if json.loads(line)[0] == "systemd-run"]
    assert launches_after == launches
    with kernel.storage.transaction(write=False) as tx:
        assert tx.one("SELECT count(*) FROM actions")[0] == original_actions + 1
