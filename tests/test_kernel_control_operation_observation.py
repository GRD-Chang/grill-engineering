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
from support.ticket_process import isolated_ticket_test
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


def managed_approval(tmp_path, monkeypatch, *, tickets, language="en", interrupt_development=False, save_development=False):
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
                if save_development:
                    runner.authorize_agent(step.call)
                    kernel.receive(step.call, agents.agent(step.call))
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


@pytest.mark.parametrize("missing", ["frozen-material", "managed-checkout"])
@isolated_ticket_test
def test_public_resume_after_trusted_material_or_checkout_restoration(tmp_path, monkeypatch, missing):
    """Corrupt only isolated fixture bytes; restore the exact original backup."""
    import subprocess
    import sys
    from agent_run.config.paths import app_data_root
    kernel, _runner, physical, _state, calls, request, subject, _source = managed_approval(
        tmp_path, monkeypatch, tickets=(), interrupt_development=True)
    database = kernel.storage.path
    before = kernel.status(subject)
    with kernel.storage.transaction(write=False) as tx:
        inputs = tx.one("SELECT subject,material_id FROM run_inputs")
        frozen = tx.get(inputs["material_id"], inputs["subject"])
        attempt = tx.one("SELECT current_attempt FROM subjects WHERE id=?", (subject,))[0]
        count = tx.one("SELECT count(*) FROM actions")[0]
    backup = database.read_bytes()
    checkout = physical.git.root
    saved_checkout = tmp_path / "trusted-checkout"
    if missing == "frozen-material":
        # Physical corruption, not a fabricated business row or a new digest.
        # SQLite still reads the record; original immutable hash must reject it.
        prefix = frozen[:128]
        assert backup.count(prefix) >= 1
        database.write_bytes(backup.replace(prefix, b"[" + prefix[1:]))
    else:
        checkout.rename(saved_checkout)
    damaged = database.read_bytes()
    active = app_data_root() / "active" / "current"
    active.parent.mkdir(parents=True, exist_ok=True)
    active.symlink_to(Path(lifecycle_cli.__file__).resolve().parents[3], target_is_directory=True)
    manager_root = tmp_path / "manager"
    environment = dict(os.environ, PATH=str(manager_root) + os.pathsep + os.environ["PATH"])
    response = manager_root / "manager-response.json"
    document = json.loads(response.read_text())
    document["fields"].update(ActiveState="inactive", SubState="dead", MainPID="0", ControlPID="0", NFileDescriptorStore="0")
    response.write_text(json.dumps(document))
    group = manager_root / "cgroups" / document["fields"]["ControlGroup"].lstrip("/")
    group.mkdir(exist_ok=True, parents=True)
    (group / "cgroup.events").write_text("populated 0\n")
    # Separate interpreter, installed public main; only the external systemd
    # adapter's sysfs root and unused synthetic launch startup are controlled.
    bootstrap = tmp_path / "resume.py"
    bootstrap.write_text("""from contextlib import contextmanager
import sys
from pathlib import Path
import agent_run.adapters.systemd as adapter
import agent_run.runtime.handoff as native
from agent_run.cli.main import main
original = adapter.SystemdHost
adapter.SystemdHost = lambda **kw: original(**kw, cgroup_root=Path(sys.argv[1]))
@contextmanager
def handoff(*args, **kwargs):
    proof = native.prepare_handoff(*args, **kwargs, ttl_seconds=.5)
    try:
        yield proof
    finally:
        proof.process.wait(timeout=3)
native.handoff_scope = handoff
raise SystemExit(main(sys.argv[2:]))
""")
    command = [sys.executable, str(bootstrap), str(manager_root / "cgroups"), "resume", request.run_id,
               "--repo", physical.repository, "--state-dir", str(database), "--json"]
    failed = subprocess.run(command, env=environment, cwd=tmp_path, text=True, capture_output=True, timeout=15)
    assert failed.returncode == 2, (failed.stdout, failed.stderr)
    error = json.loads(failed.stdout)
    assert error["result"] == "error"
    diagnostic = error["diagnostics"][0]
    if missing == "frozen-material":
        assert diagnostic["code"] == "material_corrupt" and inputs["material_id"] in diagnostic["message"]
    else:
        assert diagnostic["code"] == "command_failed"
        assert diagnostic["message"] in {"The Runner workspace is incomplete", "Runner 工作区不完整"}, diagnostic
    assert database.read_bytes() == damaged  # Failed request did not rewrite evidence/Actions.
    assert not (manager_root / "helper-calls.jsonl").exists() or not any(
        json.loads(line)[0] == "systemd-run" for line in (manager_root / "helper-calls.jsonl").read_text().splitlines())
    if missing == "frozen-material":
        database.write_bytes(backup)
    else:
        saved_checkout.rename(checkout)
    assert database.read_bytes() == backup
    resumed = subprocess.run(command, env=environment, cwd=tmp_path, text=True, capture_output=True, timeout=15)
    assert resumed.returncode == 0, (resumed.stdout, resumed.stderr)
    result = json.loads(resumed.stdout)
    assert result["result"] == "accepted" and result["action"] == "resume"
    successor = kernel.host_request(result["action_id"])
    assert kernel.host_dispatched(result["action_id"])
    assert kernel.status(subject)["budgets"] == before["budgets"]
    with kernel.storage.transaction(write=False) as tx:
        assert tx.one("SELECT current_attempt FROM subjects WHERE id=?", (subject,))[0] == attempt
        assert tx.one("SELECT count(*) FROM actions")[0] == count + 1
        assert tx.get(inputs["material_id"], inputs["subject"]) == frozen
    # The external manager can now prove the successor; the real Core produces
    # the existing Attempt's next preparation, not another development charge.
    manager = lifecycle_cli._manager_for(kernel, _parser("en").parse_args(["resume", request.run_id]), environment)
    document["fields"].update(Id=successor.unit_name, Description=successor.description,
        InvocationID="3" * 32, ActiveState="active", SubState="running", MainPID=str(os.getpid()))
    response.write_text(json.dumps(document))
    observed = manager.observe(successor)
    assert observed.disposition == "running"
    assert kernel.observe_host(successor, replace(observed, usage_ready=True,
        evidence=kernel.host_usage(result["action_id"]).evidence)) == "running"
    runner = OperationRunner(kernel, Execution(successor.run_id, successor.owner, successor.epoch), lambda binding: physical)
    for _ in range(20):
        step = runner.run_once()
        if step.preparation is not None:
            break
    else:
        pytest.fail("Restored public Resume did not reach its original successor work")
    assert step.preparation.attempt_id == attempt and step.preparation.subject == subject
    assert kernel.status(subject)["budgets"] == before["budgets"]
    assert kernel.status(subject)["gates"] == []


@isolated_ticket_test
def test_missing_delivery_checkout_public_resume_preserves_return_and_recovers_in_new_interpreter(tmp_path, monkeypatch):
    """Real Git/SQLite/public main and child exit; manager/sysfs are synthetic."""
    import subprocess
    import sys
    from agent_run.config.paths import app_data_root
    kernel, _runner, physical, _state, calls, original, subject, _source = managed_approval(
        tmp_path, monkeypatch, tickets=(), interrupt_development=True, save_development=True)
    database = kernel.storage.path
    delivery = kernel.delivery(subject)
    checkout = Path(delivery.checkout)
    original_work = (checkout / "development-1.txt").read_bytes()
    before = kernel.status(subject)
    assert before["pending_return"] and not before["gates"]
    with kernel.storage.transaction(write=False) as tx:
        attempt = tx.one("SELECT * FROM attempts WHERE id=?", (before["pending_attempt"],))
        attempt_id, invocation_id, raw_id = attempt["id"], attempt["current_invocation"], attempt["result_id"]
        raw = tx.get(raw_id, subject)
    trusted = tmp_path / "trusted-development-checkout"
    checkout.rename(trusted)
    assert not checkout.exists() and (trusted / "development-1.txt").read_bytes() == original_work
    active = app_data_root() / "active" / "current"
    active.parent.mkdir(parents=True, exist_ok=True)
    active.symlink_to(Path(lifecycle_cli.__file__).resolve().parents[3], target_is_directory=True)
    manager_root = tmp_path / "manager"
    response = manager_root / "manager-response.json"
    document = json.loads(response.read_text())
    document["fields"].update(ActiveState="inactive", SubState="dead", MainPID="0", ControlPID="0", NFileDescriptorStore="0")
    response.write_text(json.dumps(document))
    group = manager_root / "cgroups" / document["fields"]["ControlGroup"].lstrip("/")
    group.mkdir(parents=True, exist_ok=True)
    (group / "cgroup.events").write_text("populated 0\n")
    environment = dict(os.environ, PATH=str(manager_root) + os.pathsep + os.environ["PATH"])
    driver = tmp_path / "public-resume-driver.py"
    driver.write_text('''from contextlib import contextmanager
from dataclasses import replace
import io,json,os,sys
from contextlib import redirect_stdout
from pathlib import Path
import agent_run.adapters.systemd as adapter
import agent_run.runtime.handoff as native
from agent_run.cli.main import main
from agent_run.core.kernel import Kernel
from agent_run.core.operation_runner import OperationRunner
from agent_run.adapters.remote import RemoteEffects
from agent_run.model import Execution
manager_root=Path(sys.argv[1]); original=adapter.SystemdHost
adapter.SystemdHost=lambda **kw:original(**kw,cgroup_root=manager_root/'cgroups')
@contextmanager
def handoff(*args,**kwargs):
    proof=native.prepare_handoff(*args,**kwargs,ttl_seconds=.5)
    try:yield proof
    finally:proof.process.wait(timeout=3)
native.handoff_scope=handoff
output=io.StringIO()
with redirect_stdout(output):code=main(sys.argv[2:])
control=json.loads(output.getvalue())
if code:print(json.dumps({'control':control}));raise SystemExit(code)
kernel=Kernel(Path(sys.argv[sys.argv.index('--state-dir')+1]),run_id=control['run_id'])
request=kernel.host_request(control['action_id'])
response=manager_root/'manager-response.json';state=json.loads(response.read_text())
state['fields'].update(Id=request.unit_name,Description=request.description,InvocationID=request.nonce,
    ActiveState='active',SubState='running',MainPID=str(os.getpid()),ControlPID='0',NFileDescriptorStore='0')
response.write_text(json.dumps(state))
manager=adapter.SystemdHost(systemctl=str(manager_root/'systemctl'),systemd_run=str(manager_root/'systemd-run'),environment=os.environ)
observed=manager.observe(request)
assert observed.disposition=='running'
assert kernel.observe_host(request,replace(observed,usage_ready=True,evidence=kernel.host_usage(control['action_id']).evidence))=='running'
runner=OperationRunner(kernel,Execution(request.run_id,request.owner,request.epoch),lambda binding:RemoteEffects(
    binding.repository_root,binding.repository,runner_identity=binding.inputs.runner_identity,remote=binding.inputs.repository_remote))
trace=[]
for _ in range(25):
    step=runner.run_once();trace.append([step.kind,step.reason])
    if step.preparation is not None or step.kind in {'paused','waiting','approval','complete','quiescent'}:break
else:raise AssertionError('Bounded successor did not reach an observable boundary')
print(json.dumps({'control':control,'pid':os.getpid(),'trace':trace,'kind':step.kind,'reason':step.reason,
    'preparation':None if step.preparation is None else {'subject':step.preparation.subject,'role':step.preparation.role,
        'attempt':step.preparation.attempt_id,'context':step.preparation.context}}))
''')
    command = [sys.executable, str(driver), str(manager_root), "resume", original.run_id,
               "--repo", physical.repository, "--state-dir", str(database), "--json"]
    first = subprocess.run(command, cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=25)
    assert first.returncode == 0, (first.stdout, first.stderr)
    refused = json.loads(first.stdout)
    assert refused["control"]["result"] == "accepted" and refused["kind"] == "paused", refused
    assert refused["reason"] == "write_not_dispatched", refused
    assert not Path(f"/proc/{refused['pid']}").exists()
    # Public status/history supply the exact failed local read and its original
    # subject, intent and checkout. No synthetic diagnostic is inserted in Core.
    selection = ["--repo", physical.repository, "--state-dir", str(database), "--json"]
    def query(kind):
        result = subprocess.run([sys.executable, "-m", "agent_run", kind, original.run_id, *selection],
            cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, (result.stdout, result.stderr)
        return json.loads(result.stdout)
    status, history = query("status"), query("history")
    current = next(item for item in status["subjects"] if item["subject"] == subject)
    gate, = current["gates"]
    assert gate["code"] == "write_result_unknown" and gate["detail"]["raw"]["reason"] == "local_checkout_unavailable"
    events = history["history"]["events"]
    refusal, = [item for item in events if item["kind"] == "effect_pre_dispatch_observed"]
    prepared, = [item for item in events if item["kind"] == "effect_prepared" and item["identity"] == refusal["identity"]]
    assert prepared["subject"] == subject and prepared["evidence"]["checkout"] == str(checkout)
    with kernel.storage.transaction(write=False) as tx:
        pending = dict(tx.one("SELECT * FROM effects WHERE id=?", (refusal["identity"],)))
        assert pending["operation"] == "candidate" and pending["status"] == "prepared" and pending["attempts"] == 0
        assert tx.get(raw_id, subject) == raw
    assert (trusted / "development-1.txt").read_bytes() == original_work
    assert kernel.status(subject)["budgets"] == before["budgets"]
    # Restore the exact saved directory/Git identity, not a new empty checkout.
    trusted.rename(checkout)
    document = json.loads(response.read_text())
    document["fields"].update(ActiveState="inactive", SubState="dead", MainPID="0", ControlPID="0", NFileDescriptorStore="0")
    response.write_text(json.dumps(document))
    second = subprocess.run(command, cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=25)
    assert second.returncode == 0, (second.stdout, second.stderr)
    recovered = json.loads(second.stdout)
    assert recovered["control"]["action"] == "resume" and recovered["control"]["result"] == "accepted"
    assert recovered["pid"] != refused["pid"] and not Path(f"/proc/{recovered['pid']}").exists()
    assert recovered["preparation"]["subject"] == subject and recovered["preparation"]["role"] == "review", recovered
    assert prepared["evidence"]["summary_attempt"] == attempt_id
    final_history = query("history")["history"]["events"]
    consumed, = [item for item in final_history if item["kind"] == "role_consumed" and item["identity"] == attempt_id]
    assert consumed["material_id"] == raw_id and consumed["evidence"] == json.loads(raw)
    assert kernel.status(subject)["budgets"] == before["budgets"] and not kernel.status(subject)["gates"]
    with kernel.storage.transaction(write=False) as tx:
        assert tx.get(raw_id, subject) == raw
        assert tx.one("SELECT current_invocation FROM attempts WHERE id=?", (attempt_id,))[0] == invocation_id
        assert tx.one("SELECT count(*) FROM attempts WHERE role='development'")[0] == 1
        settled = tx.one("SELECT status,attempts FROM effects WHERE id=?", (pending["id"],))
        assert tuple(settled) == ("confirmed", 1)
    candidate = recovered["preparation"]["context"]["candidate_sha"]
    assert physical.git.run("show", candidate + ":development-1.txt").stdout.encode() == original_work
    assert not any("--method" in json.loads(line) for line in calls.read_text().splitlines())


@isolated_ticket_test
def _delayed_public_recovery_case(tmp_path, monkeypatch):
    """Hold only the test CLI after it has created a real original guardian."""
    original_write = Path.write_text
    bootstrap = tmp_path / "resume.py"
    receipt = tmp_path / "guardian-owner.json"

    def delayed(path, text, *args, **kwargs):
        if path == bootstrap:
            point = "    try:\n        yield proof"
            assert text.count(point) == 1
            text = text.replace(point, "    try:\n        import json, os, signal\n"
                + "        Path(" + repr(str(receipt)) + ").write_text(json.dumps({'guardian': proof.receipt['guardian'], 'cli': os.getpid()}))\n"
                + "        signal.pause()\n        yield proof")
        return original_write(path, text, *args, **kwargs)
    monkeypatch.setattr(Path, "write_text", delayed)
    target = getattr(test_public_resume_after_trusted_material_or_checkout_restoration,
                     "__wrapped__", test_public_resume_after_trusted_material_or_checkout_restoration)
    target(tmp_path, monkeypatch, "managed-checkout")


def test_public_resume_timeout_reaps_exact_guardian_without_touching_unrelated_child(tmp_path, monkeypatch):
    import ctypes
    import select
    import signal
    import subprocess
    import sys
    from agent_run.runtime.process_cleanup import child_subreaper
    from agent_run.runtime.process_identity import _process_start_token

    receipt = tmp_path / "guardian-owner.json"
    unrelated = subprocess.Popen([sys.executable, "-I", "-c", "import signal; signal.pause()"], start_new_session=True)
    try:
        unrelated_token = _process_start_token(unrelated.pid)
        with child_subreaper():
            try:
                active = ctypes.c_int()
                assert ctypes.CDLL(None).prctl(37, ctypes.byref(active), 0, 0, 0) == 0 and active.value == 1
                with pytest.raises((subprocess.TimeoutExpired, pytest.fail.Exception)) as failed:
                    _delayed_public_recovery_case(tmp_path, monkeypatch)
                assert "timed out after 15 seconds" in str(failed.value)
                assert not getattr(failed.value, "__notes__", ()), "Owned cleanup must not fail behind the expected timeout"
                recorded = json.loads(receipt.read_text())
                guardian = recorded["guardian"]
                assert type(guardian["pid"]) is int and guardian["start_token"]
                assert _process_start_token(recorded["cli"]) is None
                assert _process_start_token(guardian["pid"]) != guardian["start_token"], "Original guardian was orphaned after CLI timeout"
                assert unrelated.poll() is None and _process_start_token(unrelated.pid) == unrelated_token
            finally:
                # A failed regression still owns its exact orphan. No global PID
                # sweep or unrelated process group is used as the safety net.
                if receipt.exists():
                    guardian = json.loads(receipt.read_text())["guardian"]
                    pid, token = guardian["pid"], guardian["start_token"]
                    if _process_start_token(pid) == token:
                        fd = os.pidfd_open(pid)
                        try:
                            assert _process_start_token(pid) == token
                            if not select.select([fd], [], [], 3)[0]:
                                signal.pidfd_send_signal(fd, signal.SIGKILL)
                            os.waitpid(pid, 0)
                        finally:
                            os.close(fd)
    finally:
        # Guardian cleanup failure must not skip this independently owned child.
        try:
            if unrelated.poll() is None:
                unrelated.terminate()
        finally:
            unrelated.wait(timeout=3)


@pytest.mark.parametrize("failure", ["subreaper-capability", "guardian-cleanup"])
def test_public_resume_timeout_owner_cleans_unrelated_child_on_early_failure(tmp_path, monkeypatch, failure):
    """Keep real child ownership even if capability or guardian cleanup fails."""
    import ctypes
    import subprocess
    from types import SimpleNamespace
    from agent_run.runtime.process_identity import _process_start_token

    created = []
    reached_delay = []
    original_popen, original_cdll, original_exists = subprocess.Popen, ctypes.CDLL, Path.exists
    receipt = tmp_path / "guardian-owner.json"

    def owned_child(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        created.append((process, _process_start_token(process.pid)))
        return process

    def stopped_case(*args):
        reached_delay.append(True)
        raise RuntimeError("test body interrupted")

    def capability(name, *args, **kwargs):
        # child_subreaper's own save/restore stays real; fail its caller's
        # explicit capability observation before the delayed case can start.
        if not args and not kwargs:
            return SimpleNamespace(prctl=lambda *args: -1)
        return original_cdll(name, *args, **kwargs)

    def cleanup_error(path):
        if path == receipt:
            raise OSError("guardian cleanup interrupted")
        return original_exists(path)

    monkeypatch.setattr(subprocess, "Popen", owned_child)
    monkeypatch.setattr(__import__(__name__), "_delayed_public_recovery_case", stopped_case)
    if failure == "subreaper-capability":
        monkeypatch.setattr(ctypes, "CDLL", capability)
    else:
        monkeypatch.setattr(Path, "exists", cleanup_error)
    try:
        expected = AssertionError if failure == "subreaper-capability" else OSError
        with pytest.raises(expected) as failed:
            test_public_resume_timeout_reaps_exact_guardian_without_touching_unrelated_child(tmp_path, monkeypatch)
        if failure == "guardian-cleanup":
            assert "guardian cleanup interrupted" in str(failed.value)
        assert reached_delay == ([] if failure == "subreaper-capability" else [True])
        assert len(created) == 1
        process, token = created[0]
        assert token is not None
        assert process.poll() is not None, "Early failure leaked the owned unrelated child"
        assert _process_start_token(process.pid) != token
    finally:
        # Regression safety net, after the ownership assertions above.
        for process, token in created:
            try:
                if process.poll() is None and _process_start_token(process.pid) == token:
                    process.kill()
            finally:
                process.wait(timeout=3)
