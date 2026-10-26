"""Actual local lease and Unix-FD handoff, without claiming a live user unit."""

import array
from dataclasses import asdict
import json
import os
from pathlib import Path
import select
import shutil
import socket
import subprocess
import sys
import threading

import pytest

from agent_run.runtime.package_lease import RunnerLeaseBusy, runner_management_lease, runner_usage_lease
from agent_run.model import Commit, Delivery, DeliveryKind, RunSpec
from agent_run.core.kernel import Kernel
from agent_run.errors import KernelError
from agent_run.model import Execution, HostObservation
from support.core_operations import CoreOperations, native_agent_fixture
from agent_run.runtime.handoff import HandoffError, host_environment, launch_handoff, prepare_handoff
from support.systemd_manager import controlled_manager
from test_kernel_host_adapter import host_kernel


def test_terminal_guardian_host_and_fdstore_keep_one_continuous_usage_lease(tmp_path: Path) -> None:
    _exercise_handoff(tmp_path, managed=False)


def test_kernel_ready_and_released_follow_actual_host_and_manager_lease_handoff(tmp_path: Path) -> None:
    _exercise_handoff(tmp_path, managed=True)


def _exercise_handoff(tmp_path: Path, *, managed: bool) -> None:
    base = Commit("base", "tree")
    delivery = Delivery("parent", DeliveryKind.PARENT_ONLY, 1, "requirements", base, "source", "target")
    kernel, ports = native_agent_fixture(tmp_path / "state" / "kernel.sqlite3", RunSpec(
        "host-run", "example/repository", 1, "requirements", "graph", (delivery,),
        language="en", repository_root=str(tmp_path / "repository")))
    manager, fixture, response, calls = controlled_manager(tmp_path)
    private = tmp_path / "handoff"
    private.mkdir(mode=0o700)
    carrier = private / "environment.json"
    launch = kernel.admit_host("handoff", kind="run", command=fixture.command,
                               environment_carrier=carrier, preflight=manager.preflight)
    execution = Execution(launch.run_id, launch.owner, launch.epoch)
    launch_listener = None
    launch_channel = None
    launch_thread = None
    launch_results = []
    launch_errors = []
    if managed:
        launch_listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        launch_listener.bind(str(private / "launch-reply"))
        launch_listener.listen(1)
        launch_listener.settimeout(3)
        executable = Path(manager.systemd_run)
        program = executable.read_text()
        program = program.replace("    raise SystemExit(state['launch_returncode'])", f"""    import socket
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as reply:
        reply.settimeout(4)
        reply.connect({str(private / 'launch-reply')!r})
        assert reply.recv(1) == b'1'
    raise SystemExit(state['launch_returncode'])""")
        executable.write_text(program)
        state = json.loads(response.read_text())
        state["launch_returncode"] = 1
        response.write_text(json.dumps(state))
    lock = tmp_path / "runner.lock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    notify = str(private / "notify")
    server.bind(notify)
    server.settimeout(5)
    stored: list[int] = []
    errors: list[BaseException] = []
    post_finished = threading.Event()

    def receive_store() -> None:
        try:
            for expected in (b"FDSTORE=1", b"BARRIER=1"):
                data, ancillary, _, _ = server.recvmsg(1024, socket.CMSG_SPACE(array.array("i").itemsize))
                assert data.startswith(expected)
                descriptors = array.array("i")
                for level, kind, value in ancillary:
                    assert (level, kind) == (socket.SOL_SOCKET, socket.SCM_RIGHTS)
                    descriptors.frombytes(value[:len(value) - len(value) % descriptors.itemsize])
                assert len(descriptors) == 1
                if expected == b"FDSTORE=1":
                    stored.append(descriptors[0])
                else:
                    os.close(descriptors[0])  # Actual EOF acknowledges the systemd barrier.
            if managed:
                assert post_finished.wait(timeout=5), "ExitStopPost did not finish"
                os.close(stored.pop())
                state = json.loads(response.read_text())
                state["fields"]["NFileDescriptorStore"] = "0"
                response.write_text(json.dumps(state))
        except BaseException as error:
            errors.append(error)

    receiver = threading.Thread(target=receive_store)
    receiver.start()
    ready_read, ready_write = os.pipe()
    release_read, release_write = os.pipe()
    child = None
    post = None
    handoff = None
    try:
        with runner_usage_lease(lock) as terminal:
            handoff = prepare_handoff(launch, {"SYNTHETIC_SECRET": "fixture-private-value"},
                                       runner_lock=lock, terminal_lease=terminal)
            credential = private / "systemd-credential-copy"
            shutil.copyfile(carrier, credential)
            credential.chmod(0o600)
            if managed:
                def launch_once() -> None:
                    try:
                        launch_results.append(launch_handoff(kernel, "handoff", manager, handoff))
                    except BaseException as error:
                        launch_errors.append(error)
                launch_thread = threading.Thread(target=launch_once)
                launch_thread.start()
                assert launch_listener is not None
                launch_channel, _ = launch_listener.accept()
                with pytest.raises(KernelError, match="host_usage_unproven"):
                    CoreOperations(kernel, execution, lambda binding: ports).once()
        # The terminal's descriptor has closed; the acknowledged guardian alone
        # protects the active Runner before the Host even exists.
        with pytest.raises(RunnerLeaseBusy):
            with runner_management_lease(lock):
                raise AssertionError("management crossed the startup handoff")
        request = {**asdict(launch), "environment_carrier": str(carrier)}
        script = """
import json, os, sys
from pathlib import Path
from agent_run.model import HostLaunch
from agent_run.core.kernel import Kernel
from agent_run.runtime.handoff import host_environment
from agent_run.adapters.systemd import SystemdHost
value = json.loads(sys.argv[1]); value['environment_carrier'] = Path(value['environment_carrier'])
value['command'] = tuple(value['command'])
if sys.argv[7] == 'managed':
    from agent_run.runtime.handoff import managed_host_environment
    state_path = Path(sys.argv[8]); state = json.loads(state_path.read_text())
    group = next(line[3:] for line in Path('/proc/self/cgroup').read_text().splitlines() if line.startswith('0::'))
    state['fields'].update(Id=value['unit_name'], Description=HostLaunch(**value).description,
                          ActiveState='active', SubState='running', MainPID=str(os.getpid()), ControlGroup=group, NFileDescriptorStore='1')
    state_path.write_text(json.dumps(state))
    manager = SystemdHost(systemctl=sys.argv[9], systemd_run=sys.argv[10], environment={'PATH': os.defpath},
                          cgroup_root=Path(sys.argv[11]))
    selected = managed_host_environment(Kernel(Path(sys.argv[12])), 'handoff', manager,
                                       runner_lock=Path(sys.argv[2]), credential=Path(sys.argv[3]), notify_socket=sys.argv[4])
else:
    selected = host_environment(HostLaunch(**value), runner_lock=Path(sys.argv[2]),
                                credential=Path(sys.argv[3]), notify_socket=sys.argv[4])
with selected as inherited:
    assert inherited.environment == {'SYNTHETIC_SECRET': 'fixture-private-value'}
    os.write(int(sys.argv[5]), b'1')
    os.read(int(sys.argv[6]), 1)
"""
        child = subprocess.Popen([sys.executable, "-c", script, json.dumps(request), str(lock),
                                  str(credential), notify, str(ready_write), str(release_read),
                                  "managed" if managed else "raw", str(response), manager.systemctl,
                                  manager.systemd_run, str(manager.cgroup_root), str(kernel.storage.path)],
                                 pass_fds=(ready_write, release_read), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        os.close(ready_write); ready_write = -1
        os.close(release_read); release_read = -1
        assert select.select([ready_read], [], [], 5)[0], "Host never completed the real FDSTORE barrier"
        signal = os.read(ready_read, 1)
        assert signal == b"1", child.communicate(timeout=3) if not signal else signal
        if managed:
            reopened = Kernel(kernel.storage.path)
            usage = reopened.host_usage("handoff")
            assert usage is not None and usage.usage_ready and not usage.usage_released
            assert usage.main_pid == child.pid and json.loads(usage.evidence) == handoff.receipt
            assert "SYNTHETIC_SECRET" not in usage.evidence
            assert launch_channel is not None and launch_thread is not None
            launch_channel.sendall(b"1")  # The launch reply becomes unknown only after Host ready.
            launch_thread.join(timeout=3)
            assert not launch_thread.is_alive() and not launch_errors
            assert len(launch_results) == 1 and launch_results[0].disposition == "unknown"
            assert CoreOperations(reopened, execution, lambda binding: ports).once().kind in {"progress", "effect", "agent"}
        assert handoff.process.wait(timeout=3) == 0
        assert not carrier.exists()
        with pytest.raises(RunnerLeaseBusy):
            with runner_management_lease(lock):
                raise AssertionError("management crossed the Host lifetime")
        os.write(release_write, b"1")
        stdout, stderr = child.communicate(timeout=3)
        assert child.returncode == 0, stderr
        assert not stdout and b"fixture-private-value" not in stderr
        if not managed:
            receiver.join(timeout=3)
            assert not receiver.is_alive() and not errors
        assert len(stored) == 1
        # Host and guardian are reaped; the actual transferred open-file
        # description still protects a possible ExecStopPost interval.
        with pytest.raises(RunnerLeaseBusy):
            with runner_management_lease(lock):
                raise AssertionError("management crossed the manager's retained FD")
        if managed:
            from agent_run.runtime.handoff import reconcile_handoff
            reopened = Kernel(kernel.storage.path)
            state = json.loads(response.read_text())
            group = state["fields"]["ControlGroup"]
            group_path = manager.cgroup_root / group.lstrip("/")
            group_path.mkdir(parents=True, exist_ok=True)
            (group_path / "cgroup.events").write_text("populated 0\n")
            import time
            post = subprocess.Popen([sys.executable, "-c", "import sys;sys.stdout.write('ready\\n');sys.stdout.flush();sys.stdin.buffer.read(1)"],
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            assert post.stdout is not None and select.select([post.stdout], [], [], 3)[0]
            assert post.stdout.readline() == b"ready\n"
            state["fields"].update(ActiveState="deactivating", SubState="stop-post", MainPID="0", ControlPID=str(post.pid),
                                    ExecMainStatus="0", ExecMainExitTimestamp=f"@{time.time()}")
            response.write_text(json.dumps(state))
            assert not reconcile_handoff(reopened, "handoff", manager).usage_released
            with pytest.raises(RunnerLeaseBusy):
                with runner_management_lease(lock):
                    raise AssertionError("manager released the lease during actual ExitStopPost")
            post.communicate(input=b"1", timeout=3)
            assert post.returncode == 0
            state["fields"].update(ActiveState="inactive", SubState="dead", ControlPID="0")
            state["fields"]["InvocationID"] = "2" * 32
            response.write_text(json.dumps(state))
            assert not reconcile_handoff(reopened, "handoff", manager).usage_released
            state["fields"]["InvocationID"] = "1" * 32
            response.write_text(json.dumps(state))
            assert not reconcile_handoff(reopened, "handoff", manager).usage_released  # Store still contains the real FD.
            with pytest.raises(RunnerLeaseBusy):
                with runner_management_lease(lock):
                    raise AssertionError("nonempty manager store became a released receipt")
            # The manager closes automatically after Host + post exit, before
            # any later reconciliation command can be responsible for release.
            post_finished.set()
            receiver.join(timeout=3)
            assert not receiver.is_alive() and not errors and not stored
            with runner_management_lease(lock):
                pass
            state = json.loads(response.read_text())
            state["fields"]["LoadState"] = "not-found"
            response.write_text(json.dumps(state))
            assert not reconcile_handoff(reopened, "handoff", manager).usage_released
            state["fields"]["LoadState"] = "loaded"
            response.write_text(json.dumps(state))
            released = reconcile_handoff(reopened, "handoff", manager)
            assert released.disposition == "exited" and released.usage_released
            receipt = Kernel(kernel.storage.path).host_usage("handoff")
            assert receipt == released and "SYNTHETIC_SECRET" not in receipt.evidence
            before = calls.read_bytes()
            assert reconcile_handoff(reopened, "handoff", manager) == released
            assert calls.read_bytes() == before
            assert not any("clean" in json.loads(line) for line in before.decode().splitlines())
            receiver.join(timeout=3)
            assert not receiver.is_alive() and not errors and not stored
        else:
            os.close(stored.pop())
        with runner_management_lease(lock):
            pass
    finally:
        if launch_channel is not None:
            launch_channel.close()
        if launch_listener is not None:
            launch_listener.close()
        if launch_thread is not None:
            launch_thread.join(timeout=6)
        if child is not None and child.poll() is None:
            child.kill(); child.wait(timeout=3)
        if post is not None and post.poll() is None:
            post.kill(); post.wait(timeout=3)
        if handoff is not None and handoff.process.poll() is None:
            handoff.process.kill(); handoff.process.wait(timeout=3)
        server.close()
        receiver.join(timeout=6)
        for descriptor in (*stored, ready_read, ready_write, release_read, release_write):
            if descriptor >= 0:
                os.close(descriptor)


def test_unacknowledged_host_keeps_original_guardian_receipt_until_the_sealed_deadline(tmp_path: Path) -> None:
    kernel = host_kernel(tmp_path)
    manager, fixture, response, _ = controlled_manager(tmp_path)
    private = tmp_path / "handoff"
    private.mkdir(mode=0o700)
    carrier = private / "environment.json"
    launch = kernel.admit_host("handoff", kind="run", command=fixture.command,
                               environment_carrier=carrier, preflight=manager.preflight)
    state = json.loads(response.read_text())
    state["launch_returncode"] = 1
    response.write_text(json.dumps(state))
    lock = tmp_path / "runner.lock"
    handoff = None
    try:
        with runner_usage_lease(lock) as terminal:
            handoff = prepare_handoff(launch, {"SYNTHETIC_SECRET": "fixture-private-value"},
                                       runner_lock=lock, terminal_lease=terminal, ttl_seconds=3)
            credential = private / "late-credential-copy"
            shutil.copyfile(carrier, credential)
            credential.chmod(0o600)
            observed = launch_handoff(kernel, "handoff", manager, handoff)
            assert observed is not None and observed.disposition == "unknown"
        request = {**asdict(launch), "environment_carrier": str(carrier)}
        script = """
import json, sys
from pathlib import Path
from agent_run.model import HostLaunch
from agent_run.runtime.handoff import host_environment
value = json.loads(sys.argv[1]); value['environment_carrier'] = Path(value['environment_carrier'])
value['command'] = tuple(value['command'])
with host_environment(HostLaunch(**value), runner_lock=Path(sys.argv[2]),
                      credential=Path(sys.argv[3]), notify_socket=sys.argv[4]):
    Path(sys.argv[5]).write_text('unauthorized business work')
"""
        canary = tmp_path / "must-not-run"
        child = subprocess.run([sys.executable, "-c", script, json.dumps(request), str(lock),
                                str(credential), str(private / "missing-notify"), str(canary)],
                               capture_output=True, timeout=2)
        assert child.returncode != 0 and not canary.exists()
        assert b"fixture-private-value" not in child.stdout + child.stderr
        reopened = Kernel(kernel.storage.path)
        assert reopened.host_request("handoff") == launch
        usage = reopened.host_usage("handoff")
        assert usage is not None and usage.usage_pending and not usage.usage_ready
        assert json.loads(usage.evidence) == handoff.receipt
        assert "SYNTHETIC_SECRET" not in usage.evidence
        reopened.observe_host(launch, HostObservation("unknown", launch.unit_name, evidence="later manager unavailable"))
        assert reopened.host_usage("handoff") == usage
        with pytest.raises(RunnerLeaseBusy):
            with runner_management_lease(lock):
                raise AssertionError("unknown ACK released the guardian lease before its deadline")
        assert handoff.process.wait(timeout=4) != 0
        assert not carrier.exists() and not Path(handoff.receipt["socket"]).exists()
        with runner_management_lease(lock):
            pass
        with pytest.raises(HandoffError, match="expired"):
            with host_environment(launch, runner_lock=lock, credential=credential,
                                  notify_socket=str(private / "missing-notify")):
                raise AssertionError("late credential copy resurrected an expired handoff")
    finally:
        if handoff is not None and handoff.process.poll() is None:
            handoff.process.kill()
            handoff.process.wait(timeout=3)


@pytest.mark.parametrize("occupied", ["carrier", "socket"])
def test_failed_prepare_preserves_resources_created_by_another_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, occupied: str,
) -> None:
    kernel = host_kernel(tmp_path)
    manager, fixture, _, _ = controlled_manager(tmp_path)
    private = tmp_path / "handoff"
    private.mkdir(mode=0o700)
    carrier = private / "environment.json"
    launch = kernel.admit_host("handoff", kind="run", command=fixture.command,
                               environment_carrier=carrier, preflight=manager.preflight)
    endpoint = carrier.with_name("guardian-" + launch.nonce + ".sock")
    owned = carrier if occupied == "carrier" else endpoint
    identity = []
    original_open = os.open

    def competing_create(path, flags, mode=0o777, *, dir_fd=None):
        # Inject the competing filesystem actor at the actual O_EXCL boundary;
        # both the conflict and the preserved sentinel are real OS objects.
        if os.fspath(path) == str(carrier) and flags & os.O_EXCL:
            descriptor = original_open(carrier, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.write(descriptor, b"another owner's private carrier")
            os.close(descriptor)
            info = carrier.stat()
            identity.append((info.st_dev, info.st_ino))
        return original_open(path, flags, mode, dir_fd=dir_fd)

    with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as foreign:
        directory = original_open(private, os.O_RDONLY | os.O_DIRECTORY)
        try:
            if occupied == "socket":
                foreign.bind(f"/proc/self/fd/{directory}/{endpoint.name}")
                info = endpoint.stat()
                identity.append((info.st_dev, info.st_ino))
            else:
                monkeypatch.setattr(os, "open", competing_create)
            lock = tmp_path / "runner.lock"
            with runner_usage_lease(lock) as terminal:
                with pytest.raises((HandoffError, OSError, ValueError)):
                    prepare_handoff(launch, {}, runner_lock=lock, terminal_lease=terminal, ttl_seconds=0.5)
            assert owned.exists(), "failed preparation deleted a resource it never owned"
            info = owned.stat()
            assert (info.st_dev, info.st_ino) == identity[0]
            if occupied == "carrier":
                assert carrier.read_bytes() == b"another owner's private carrier"
            with runner_management_lease(lock):
                pass
        finally:
            os.close(directory)
            owned.unlink(missing_ok=True)
