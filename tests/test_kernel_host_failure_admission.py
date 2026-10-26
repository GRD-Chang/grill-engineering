"""Fatal Host shutdown fences admission before awaiting exact Worker settlement."""
from support.core_operations import prepare_scripted_agent
from contextlib import contextmanager
from dataclasses import replace
import json
import subprocess
import sys
import threading

import pytest

import agent_run.runtime.executor as executor
from agent_run.core.kernel import Kernel
from agent_run.core.operation_runner import OperationRunner
from agent_run.errors import KernelError
from agent_run.model import Execution, HostObservation, Policy
from agent_run.runtime.execution import _cgroup
from agent_run.runtime.process_identity import _process_start_token
from support.offline import GraphScenario
from test_kernel_operation_integration import _admit_host
from support.core_operations import Ports
from support.requirements_scenarios import independent_source
from test_kernel_host_admission import task_run


@pytest.mark.parametrize("storage_failure", ["none", "write_unavailable", "committed_reply_loss"])
def test_fatal_host_drain_rejects_new_and_repeated_resume_and_preserves_settlement(
    tmp_path, monkeypatch, storage_failure,
):
    scenario = GraphScenario(tmp_path / "scenario", independent_source(), policy=Policy(concurrency=1))
    witness = _admit_host(scenario, tmp_path)
    kernel, execution = scenario.kernel, scenario.execution
    request = kernel.host_request(kernel.active_host_action())
    ports = Ports(scenario)
    operations = OperationRunner(kernel, execution, lambda binding: ports)

    def allocate():
        for _ in range(60):
            step = prepare_scripted_agent(kernel, operations.run_once())
            if step.call is not None:
                return step.call
        pytest.fail("normal producer did not allocate the original work")

    first = allocate()
    assert kernel.dispatch_agent(first)
    kernel.agent_unavailable(first, "preparation_failed", "No physical process was started")
    arguments = {"subject": first.subject, "message": "Continue this original attempt"}
    assert kernel.control_host(request, "resume", "original-resume", **arguments) == "applied"
    continued = allocate()
    assert continued.attempt_id == first.attempt_id and kernel.dispatch_agent(continued)
    kernel.agent_unavailable(continued, "preparation_failed", "Controlled local preparation failure")
    unstarted_budget = kernel.status(first.subject)["budgets"]
    assert unstarted_budget["requirements"]["developments_used"] == 0

    entered, cancelled, release, fault, recovered = [threading.Event() for _ in range(5)]
    resume_entered, resume_finished = threading.Event(), threading.Event()
    failures, resume_results, calls = [], [], []
    actual_transaction = kernel.storage.transaction
    write_failed = False
    original_error = OSError("original shared storage read failure")

    @contextmanager
    def transaction(*, write=True):
        nonlocal write_failed
        if threading.current_thread().name == "failing-host" and entered.is_set() and not fault.is_set():
            fault.set()
            raise original_error
        if write and fault.is_set() and storage_failure == "write_unavailable" and not recovered.is_set():
            write_failed = True
            raise OSError("shutdown storage remains unavailable")
        with actual_transaction(write=write) as tx:
            yield tx
        if write and fault.is_set() and storage_failure == "committed_reply_loss" and not write_failed:
            write_failed = True
            raise OSError("revocation committed but its response was lost")

    def worker(core, preparation, **kwargs):
        call = prepare_scripted_agent(core, preparation).call
        assert call is not None
        assert core.dispatch_agent(call)
        kwargs["authorize_source"](call)
        process = {"pid": 6262, "start_token": "controlled-worker", "pgid": 6262, **witness}
        core.agent_started(call, process)
        kwargs["authorize_source"](call)
        core.agent_source_ready(call)
        scenario.select(call.subject)
        raw = scenario.agent(call)
        calls.append(call)
        entered.set()
        try:
            assert kwargs["cancelled"].wait(8)
            cancelled.set()
            assert release.wait(8)
            core.receive(call, raw)
        finally:
            core.agent_exited(call, process, 0)

    def host():
        try:
            executor.run(kernel, execution, {})
        except BaseException as error:
            failures.append(error)

    def resume():
        resume_entered.set()
        try:
            for action in ("new-resume", "original-resume"):
                try:
                    value = kernel.control_host(request, "resume", action, **arguments)
                except KernelError as error:
                    resume_results.append(error.code)
                else:
                    resume_results.append(value)
        finally:
            resume_finished.set()

    monkeypatch.setattr(executor, "_make_ports", lambda binding: ports)
    monkeypatch.setattr('agent_run.runtime.codex.execute_selected_codex', worker)
    monkeypatch.setattr(kernel.storage, "transaction", transaction)
    host_thread = threading.Thread(target=host, name="failing-host")
    resume_thread = threading.Thread(target=resume, name="concurrent-resume")
    try:
        host_thread.start()
        assert entered.wait(8) and cancelled.wait(8)
        budget = kernel.status(first.subject)["budgets"]
        assert budget == unstarted_budget
        assert calls[0].subject != first.subject
        assert kernel.status(calls[0].subject)["budgets"]["requirements"]["developments_used"] == 1
        assert {k:v for k,v in budget.items() if k != "requirements"} == {k:v for k,v in unstarted_budget.items() if k != "requirements"}
        assert host_thread.is_alive() and fault.is_set()
        recovered.set()
        resume_thread.start()
        assert resume_entered.wait(2)
        assert resume_finished.wait(3)
        expected = "executor_fenced" if storage_failure == "none" else "task_admission_busy"
        assert resume_results == [expected, expected]
        with actual_transaction(write=False) as tx:
            assert not tx.rows("SELECT id FROM actions WHERE id='new-resume'")
            assert tx.one("SELECT status FROM actions WHERE id='original-resume'")[0] == "applied"
            assert tx.one("SELECT invocation FROM leases")[0] == calls[0].invocation_id
            assert tx.one("SELECT dispatch_state FROM invocation_outputs WHERE invocation=?", (calls[0].invocation_id,))[0] == "started"
        if storage_failure == "none":
            with pytest.raises(KernelError, match="executor_exit_unproven"):
                kernel.admit_host("too-early", kind="run", command=("native-host",),
                                  environment_carrier=tmp_path / "next-carrier", preflight=lambda: None)
    finally:
        recovered.set()
        release.set()
        host_thread.join(12)
        if resume_thread.ident is not None:
            resume_thread.join(5)
    assert not host_thread.is_alive() and not resume_thread.is_alive()
    assert failures == [original_error]
    assert resume_results == [expected, expected]
    with pytest.raises(KernelError, match="executor_fenced"):
        kernel.control_host(request, "resume", "original-resume", **arguments)
    assert len(calls) == 1 and kernel.worker_settled(calls[0])
    assert kernel.status(first.subject)["budgets"] == budget
    assert kernel.status(calls[0].subject)["pending_return"]
    assert kernel.status(calls[0].subject)["gates"] == ["execution_failed"]
    with actual_transaction(write=False) as tx:
        assert not tx.rows("SELECT id FROM leases")
        assert not tx.rows("SELECT identity FROM history WHERE kind='role_consumed' AND identity=?", (calls[0].attempt_id,))
        assert not tx.rows("SELECT id FROM actions WHERE id='new-resume'")
        assert tx.one("SELECT epoch FROM runs")[0] == execution.epoch + 1
        assert tx.one("SELECT status FROM retired_executors WHERE owner=? AND epoch=?", (execution.owner, execution.epoch))[0] == "pending"
        events = tx.rows("SELECT material_id FROM history WHERE kind='executor_failed' AND identity=?", (execution.owner,))
        assert len(events) == 1
        evidence = json.loads(tx.get(events[0][0], first.subject))
        assert evidence == {"owner": execution.owner, "epoch": execution.epoch,
                            "type": "OSError", "message": str(original_error)}
    with pytest.raises(KernelError, match="executor_exit_unproven"):
        kernel.admit_host("unproven-exit", kind="run", command=("native-host",),
                          environment_carrier=tmp_path / "next-carrier", preflight=lambda: None)
    assert kernel.observe_host(request, HostObservation("exited", request.unit_name, "original", "/host", "boot",
        exit_status=1, ended_at=kernel.clock(), usage_released=True)) == "exited"
    successor = kernel.admit_host("resume-after-exit", kind="resume", subject=calls[0].subject,
        command=("native-host",), environment_carrier=tmp_path / "successor-carrier", preflight=lambda: None)
    assert successor.owner != execution.owner and successor.epoch > execution.epoch
    assert kernel.status(first.subject)["pending_attempt"] == first.attempt_id
    assert kernel.status(first.subject)["budgets"] == budget
    assert kernel.dispatch_host(successor)
    assert kernel.observe_host(successor, HostObservation("running", successor.unit_name, "successor", "/host", "boot",
        43, "successor-start", usage_ready=True)) == "running"
    next_execution = Execution(successor.run_id, successor.owner, successor.epoch)
    resumed = OperationRunner(kernel, next_execution, lambda binding: ports)
    for _ in range(30):
        step = prepare_scripted_agent(kernel, resumed.run_once(agent_capacity=0))
        assert step.call is None
        if any(event["kind"] == "role_consumed" and event["identity"] == calls[0].attempt_id
               for event in kernel.history(calls[0].subject)):
            break
    else:
        pytest.fail("the original late raw was not consumed after explicit recovery")
    assert kernel.status(calls[0].subject)["budgets"]["requirements"]["developments_used"] == 1
    assert kernel.control_host(successor, "resume", "resume-original-after-exit", **arguments) == "applied"
    if storage_failure != "none":
        assert write_failed
        assert any("not durably" in note for note in original_error.__notes__)


@pytest.mark.parametrize("dup_unavailable", [False, True])
def test_unrecorded_shutdown_retains_task_admission_until_the_actual_host_exits(tmp_path, dup_unavailable):
    kernel, _ = task_run(tmp_path, "unrecorded-shutdown")
    request = kernel.admit_host("start", kind="run", command=("controlled-host",),
        environment_carrier=tmp_path / "carrier", preflight=lambda: None)
    assert kernel.dispatch_host(request)
    execution = kernel.host_request("start")
    script = '''import errno,json,sys,os
from contextlib import contextmanager
from pathlib import Path
from agent_run.core.kernel import Kernel
from agent_run.model import Execution
from agent_run.contracts.task import TaskKey
from agent_run.storage.tasks import task_path
print('ready',flush=True)
assert sys.stdin.readline()=='enter\\n'
kernel=Kernel(Path(sys.argv[1]))
execution=Execution(sys.argv[2],sys.argv[3],int(sys.argv[4]))
actual=kernel.storage.transaction
error=OSError('original Host failure')
dup_calls=[]
if sys.argv[5]=='True':
    def unavailable_dup(fd):
        dup_calls.append(fd)
        raise OSError(errno.EMFILE,'controlled descriptor allocation failure')
    os.dup=unavailable_dup
lock_path=task_path(TaskKey(Path(sys.argv[1]).parent/'repository','example/project',1)).with_suffix('.lock')
def held_descriptors():
    held=[]
    for fd in Path('/proc/self/fd').iterdir():
        try:
            if fd.readlink()==lock_path: held.append(int(fd.name))
        except FileNotFoundError: pass
    return held
with kernel.executor_scope(execution) as close_admission:
    @contextmanager
    def unavailable(*,write=True):
        if write: raise OSError('all shutdown commits fail')
        with actual(write=False) as tx: yield tx
    kernel.storage.transaction=unavailable
    close_admission(error)
    original_descriptors=held_descriptors()
kernel.storage.transaction=actual
retained=held_descriptors()
print(json.dumps({'type':type(error).__name__,'message':str(error),'notes':error.__notes__,
    'original_descriptors':original_descriptors,'retained':retained,
    'inheritable':[os.get_inheritable(fd) for fd in retained],'dup_calls':dup_calls}),flush=True)
assert sys.stdin.readline()=='exit\\n'
'''
    child = subprocess.Popen([sys.executable, "-c", script, str(kernel.storage.path),
        execution.run_id, execution.owner, str(execution.epoch), str(dup_unavailable)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        assert child.stdout is not None
        import select
        assert select.select([child.stdout], [], [], 8)[0]
        assert child.stdout.readline() == "ready\n"
        from pathlib import Path
        running = HostObservation("running", request.unit_name, "original", _cgroup(child.pid),
            Path("/proc/sys/kernel/random/boot_id").read_text().strip(), child.pid,
            _process_start_token(child.pid), usage_ready=True)
        assert kernel.observe_host(request, running) == "running"
        assert kernel.control_host(request, "stop", "prepare-stop", subject="parent") == "applied"
        assert child.stdin is not None
        child.stdin.write("enter\n")
        child.stdin.flush()
        assert select.select([child.stdout], [], [], 8)[0]
        result = json.loads(child.stdout.readline())
        assert result["type"] == "OSError" and result["message"] == "original Host failure"
        # Storage is available to a separate actor, but the original failed
        # Host is still alive. Neither a new nor repeated command may queue.
        assert child.poll() is None
        with pytest.raises(KernelError, match="task_admission_busy"):
            kernel.control_host(request, "resume", "during-unrecorded-close", subject="parent")
        with pytest.raises(KernelError, match="task_admission_busy"):
            kernel.control_host(request, "stop", "prepare-stop", subject="parent")
        assert len(result["notes"]) == 2 and all("not durably" in note for note in result["notes"])
        assert len(result["original_descriptors"]) == 1
        assert result["retained"] == result["original_descriptors"]
        assert result["inheritable"] == [False]
        assert result["dup_calls"] == []
        with kernel.storage.transaction(write=False) as tx:
            assert tx.one("SELECT epoch FROM runs")[0] == execution.epoch
            assert not tx.rows("SELECT id FROM actions WHERE id='during-unrecorded-close'")
            assert not tx.rows("SELECT * FROM retired_executors")
        assert child.stdin is not None
        child.stdin.write("exit\n")
        child.stdin.flush()
        stdout, stderr = child.communicate(timeout=5)
        assert child.returncode == 0, (stdout, stderr)
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)
    with pytest.raises(KernelError, match="executor_exit_unproven"):
        kernel.admit_host("unobserved-exit", kind="run", command=("controlled-host",),
                          environment_carrier=tmp_path / "later-carrier", preflight=lambda: None)
    assert kernel.observe_host(request, replace(running, disposition="exited", exit_status=child.returncode,
        ended_at=kernel.clock(), usage_released=True)) == "exited"
    successor = kernel.admit_host("after-exit", kind="resume", subject="parent", command=("controlled-host",),
        environment_carrier=tmp_path / "successor-carrier", preflight=lambda: None)
    assert successor.owner != execution.owner and successor.epoch > execution.epoch
