"""Real process/lease exits with controlled manager and cgroup facts."""
import json
import os
from pathlib import Path
import select
import signal
import socket
import subprocess
import sys
import threading

import pytest

from agent_run.model import HostObservation
from agent_run.runtime.handoff import _encode, prepare_handoff, reconcile_handoff
from agent_run.runtime.package_lease import RunnerLeaseBusy, runner_management_lease, runner_usage_lease
from agent_run.runtime.process_identity import _process_start_token
from agent_run.runtime.systemd_exit import witness_path
from support.core_operations import host_kernel
from support.systemd_manager import controlled_manager


@pytest.fixture
def main_exit():
    return -9


@pytest.fixture
def stopped_host(tmp_path, main_exit):
    kernel = host_kernel(tmp_path)
    manager, fixture, response, _ = controlled_manager(tmp_path)
    private = tmp_path / "exit-private"
    private.mkdir(mode=0o700)
    request = kernel.admit_host("original", kind="run", command=fixture.command,
        environment_carrier=private / "environment.json", preflight=manager.preflight)
    lock = tmp_path / "runner.lock"
    child = subprocess.Popen([sys.executable, "-I", "-c", "import sys;sys.stdin.buffer.read(1)"],
                             stdin=subprocess.PIPE)
    hook = handoff = None
    stored: list[int] = []
    ready_read = ready_write = finish_read = finish_write = -1
    errors = []
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    listener.bind("")
    listener.settimeout(5)
    address = "@" + listener.getsockname()[1:].decode()
    thread = None
    try:
        with runner_usage_lease(lock) as lease:
            stored.append(os.dup(lease.fileno()))  # A real manager-retained open-file description.
            handoff = prepare_handoff(request, {"PATH": os.defpath}, runner_lock=lock,
                terminal_lease=lease)
            assert kernel.dispatch_host(request)
            kernel.observe_host(request, HostObservation("unknown", request.unit_name,
                evidence=_encode(handoff.receipt).decode(), usage_pending=True))
            token = _process_start_token(child.pid)
            group = next(line[3:] for line in Path(f"/proc/{child.pid}/cgroup").read_text().splitlines()
                         if line.startswith("0::"))
            original = HostObservation("running", request.unit_name, "1" * 32, group,
                handoff.receipt["boot_id"], child.pid, token,
                evidence=_encode(handoff.receipt).decode(), usage_ready=True)
            assert kernel.observe_host(request, original) == "running"
            if main_exit == 0:
                child.communicate(input=b"1", timeout=3)
            else:
                child.kill()
            assert child.wait(timeout=3) == main_exit
            # This test checks retained exit proof, not guardian timeout. End
            # the acknowledged guardian explicitly so interpreter startup is
            # outside an artificial 200 ms scheduling race and adds no sleep.
            handoff.process.terminate()
            assert handoff.process.wait(timeout=3) == -signal.SIGTERM

        def remove_store():
            try:
                assert listener.recv(1024) == b"FDSTOREREMOVE=1\nFDNAME=runner-exit-lease"
                os.close(stored.pop())
                state = json.loads(response.read_text())
                state["fields"]["NFileDescriptorStore"] = "0"
                response.write_text(json.dumps(state))
                import array
                data, ancillary, _, _ = listener.recvmsg(1024, socket.CMSG_SPACE(array.array("i").itemsize))
                assert data == b"BARRIER=1"
                descriptors = array.array("i")
                for level, kind, value in ancillary:
                    assert (level, kind) == (socket.SOL_SOCKET, socket.SCM_RIGHTS)
                    descriptors.frombytes(value[:len(value) - len(value) % descriptors.itemsize])
                assert len(descriptors) == 1
                os.close(descriptors[0])
            except BaseException as error:
                errors.append(error)

        thread = threading.Thread(target=remove_store)
        thread.start()
        ready_read, ready_write = os.pipe()
        finish_read, finish_write = os.pipe()
        script = '''import json, os, sys, time
from pathlib import Path
from agent_run.core.kernel import Kernel
from agent_run.adapters.systemd import SystemdHost
from agent_run.runtime.systemd_exit import record_exit_witness
from agent_run.runtime.package_lease import runner_usage_lease
database, response, root, lock, systemctl, address = sys.argv[1:7]
kernel = Kernel(Path(database), run_id=sys.argv[9])
original = kernel.host_usage('original')
state = json.loads(Path(response).read_text())
state['fields'].update(Id=kernel.host_request('original').unit_name,
    Description=kernel.host_request('original').description,
    ActiveState='deactivating', SubState='stop-post', MainPID='0', ControlPID=str(os.getpid()),
    ControlGroup=original.cgroup, ExecMainCode='1' if sys.argv[10]=='0' else '2',
    ExecMainStatus='0' if sys.argv[10]=='0' else '9',
    ExecMainExitTimestamp='@'+str(time.time()), NFileDescriptorStore='1')
Path(response).write_text(json.dumps(state))
group = Path(root) / original.cgroup.lstrip('/')
group.mkdir(parents=True)
(group / 'cgroup.procs').write_text(str(os.getpid())+'\\n')
manager=SystemdHost(systemctl=systemctl,systemd_run='unused',environment={'PATH':os.defpath},cgroup_root=Path(root))
with runner_usage_lease(Path(lock)):
    record_exit_witness(kernel,'original',manager,json.loads(original.evidence),
        invocation_id='1'*32,notify_socket=address)
    os.write(int(sys.argv[7]),b'1')
    os.read(int(sys.argv[8]),1)
'''
        hook = subprocess.Popen([sys.executable, "-c", script, str(kernel.storage.path), str(response),
            str(manager.cgroup_root), str(lock), manager.systemctl, address, str(ready_write), str(finish_read),
            request.run_id, str(main_exit)],
            pass_fds=(ready_write, finish_read), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        os.close(ready_write); ready_write = -1
        os.close(finish_read); finish_read = -1
        assert select.select([ready_read], [], [], 8)[0], "Stop hook did not finish evidence"
        assert os.read(ready_read, 1) == b"1", hook.communicate(timeout=3)
        thread.join(timeout=3)
        assert not thread.is_alive() and not errors
        yield kernel, request, manager, response, hook, finish_write, lock
    finally:
        for process in (hook, child, None if handoff is None else handoff.process):
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=3)
        if hook is not None:
            hook.communicate(timeout=3)
        if child.stdin is not None:
            child.stdin.close()
        listener.close()
        if thread is not None:
            thread.join(timeout=6)
            assert not thread.is_alive()
        for descriptor in (*stored, ready_read, ready_write, finish_read, finish_write):
            if descriptor >= 0:
                os.close(descriptor)


def _finish_hook(stopped_host, state="collected"):
    kernel, request, manager, response, hook, finish_write, lock = stopped_host
    os.write(finish_write, b"1")
    stdout, stderr = hook.communicate(timeout=3)
    assert hook.returncode == 0, stderr
    assert not stdout and not stderr
    with runner_management_lease(lock):
        pass
    fields = json.loads(response.read_text())
    fields["fields"].update(ControlPID="0", ActiveState="failed", SubState="failed", ControlGroup="")
    if state == "collected":
        fields["fields"]["LoadState"] = "not-found"
    response.write_text(json.dumps(fields))
    return kernel, request, manager, response


@pytest.mark.parametrize("state", ["collected", "failed"])
@pytest.mark.parametrize("main_exit", [0, -9])
def test_original_exit_and_lease_proof_survives_unit_or_cgroup_collection(stopped_host, state, main_exit):
    kernel, request, manager, response, hook, _, lock = stopped_host
    assert witness_path(request).exists()
    with pytest.raises(RunnerLeaseBusy):
        with runner_management_lease(lock):
            pytest.fail("The live stop hook still owns a usage lease")
    assert not reconcile_handoff(kernel, "original", manager).usage_released
    _finish_hook(stopped_host, state)
    released = reconcile_handoff(kernel, "original", manager)
    assert released.disposition == "exited" and released.usage_released and released.exit_status == main_exit
    assert not witness_path(request).exists()
    assert json.loads(released.exit_evidence)["binding"] == [
        request.run_id, "original", request.epoch, request.owner, request.nonce]
    assert kernel.host_usage("original") == released
    assert reconcile_handoff(kernel, "original", manager) == released


@pytest.mark.parametrize("changed", ["package_root", "python", "both"])
def test_retained_exit_proof_can_be_consumed_by_another_active_runner(stopped_host, monkeypatch, changed):
    from agent_run.runtime import systemd_exit
    kernel, request, manager, _ = _finish_hook(stopped_host)
    original = kernel.host_usage("original")
    path = witness_path(request)
    witness = path.read_bytes()
    runner = systemd_exit._runner_witness

    def active_runner(lock):
        current = runner(lock)
        if changed in {"package_root", "both"}:
            current["package_root"] = "/another-active-runner/site-packages"
        if changed in {"python", "both"}:
            current["python"] = "/another-active-runner/bin/python"
        return current

    monkeypatch.setattr(systemd_exit, "_runner_witness", active_runner)
    released = reconcile_handoff(kernel, "original", manager)
    assert released.disposition == "exited" and released.usage_released
    # The new observer keeps the old proof exactly, including its original
    # Snapshot/interpretation facts; it does not rewrite the receipt to fit.
    assert released.evidence == original.evidence
    assert json.loads(released.exit_evidence) == json.loads(witness)
    assert not path.exists()


def test_replacing_the_original_runner_lock_rejects_retained_exit_proof(stopped_host):
    kernel, request, manager, _ = _finish_hook(stopped_host)
    lock = stopped_host[-1]
    old_identity = (lock.stat().st_dev, lock.stat().st_ino)
    replacement = lock.with_name("replacement-runner.lock")
    replacement.write_text("")
    replacement.replace(lock)
    assert (lock.stat().st_dev, lock.stat().st_ino) != old_identity
    assert not reconcile_handoff(kernel, "original", manager).usage_released
    assert witness_path(request).exists()
    assert kernel.active_host_action() == "original"


@pytest.mark.parametrize("changed", ["lock", "device"])
def test_different_current_runner_lease_identity_rejects_retained_exit_proof(stopped_host, monkeypatch, changed):
    from agent_run.runtime import systemd_exit
    kernel, request, manager, _ = _finish_hook(stopped_host)
    runner = systemd_exit._runner_witness

    def foreign_lease(lock):
        current = runner(lock)
        if changed == "lock":
            current["lock"] = str(lock.with_name("other-runner.lock"))
        else:
            current["device"] += 1
        return current

    monkeypatch.setattr(systemd_exit, "_runner_witness", foreign_lease)
    assert not reconcile_handoff(kernel, "original", manager).usage_released
    assert witness_path(request).exists()
    assert kernel.active_host_action() == "original"


@pytest.mark.parametrize("fault", ["identity", "lease", "run", "hook", "receipt", "unready"])
def test_incomplete_or_foreign_retained_proof_never_releases_original_host(stopped_host, fault):
    kernel, request, manager, response = _finish_hook(stopped_host)
    path = witness_path(request)
    value = json.loads(path.read_text())
    if fault == "identity":
        fields = json.loads(response.read_text())
        fields["fields"].update(LoadState="loaded", Description="foreign Host")
        response.write_text(json.dumps(fields))
    elif fault == "lease":
        value["manager_lease_released"] = False
    elif fault == "run":
        value["binding"][0] = "other-run"
    elif fault == "hook":
        value["hook"] = {"pid": os.getpid(), "start_token": _process_start_token(os.getpid())}
    elif fault == "receipt":
        value["receipt_digest"] = "another-receipt"
    else:
        value["observation"]["usage_ready"] = False
    path.write_text(json.dumps(value))
    assert not reconcile_handoff(kernel, "original", manager).usage_released
    assert path.exists()
    assert not kernel.host_usage("original").usage_released
    with kernel.storage.transaction(write=False) as tx:
        assert tx.one("SELECT executor FROM runs")[0] == request.owner


def test_unknown_start_cannot_write_a_recoverable_exit_witness(tmp_path):
    from agent_run.adapters.systemd import HostUnavailable
    from agent_run.runtime.systemd_exit import record_exit_witness
    kernel = host_kernel(tmp_path)
    manager, fixture, _, _ = controlled_manager(tmp_path)
    request = kernel.admit_host("unknown", kind="run", command=fixture.command,
        environment_carrier=fixture.environment_carrier, preflight=manager.preflight)
    assert kernel.dispatch_host(request)
    kernel.observe_host(request, HostObservation("unknown", request.unit_name,
        evidence="{}", usage_pending=True))
    with pytest.raises(HostUnavailable, match="unproven"):
        record_exit_witness(kernel, "unknown", manager, {}, invocation_id="1" * 32,
                            notify_socket="/must-not-contact")
    assert not witness_path(request).exists()
    assert not reconcile_handoff(kernel, "unknown", manager).usage_released


def test_live_background_thread_prevents_a_final_stop_hook_census(tmp_path):
    from agent_run.adapters.systemd import HostUnavailable
    from agent_run.runtime.systemd_exit import _only_hook
    manager, _, _, _ = controlled_manager(tmp_path)
    reached, release = threading.Event(), threading.Event()

    def background():
        reached.set()
        assert release.wait(3)

    thread = threading.Thread(target=background)
    thread.start()
    try:
        assert reached.wait(3)
        with pytest.raises(HostUnavailable, match="background execution"):
            _only_hook(manager, "/synthetic-hook-group")
    finally:
        release.set()
        thread.join(timeout=3)
        assert not thread.is_alive()
