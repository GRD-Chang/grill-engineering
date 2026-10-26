"""Owned guardian reaping and exact non-consuming exit observation."""
import array
import errno
import json
import os
from pathlib import Path
import select
import socket
import subprocess
import threading
import time
from types import SimpleNamespace

import pytest

from agent_run.cli.lifecycle import _runtime_directory, attach, submit
from agent_run.runtime import handoff as native
from agent_run.runtime.package_lease import runner_management_lease, runner_usage_lease
from support.systemd_manager import controlled_manager
from support.core_operations import host_kernel


@pytest.mark.parametrize("now,remaining", [(100.25, 2.5), (105.25, 0)])
def test_scope_exit_wait_is_bounded_by_original_deadline_without_signalling(monkeypatch, now, remaining):
    waits = []

    class UnprovenChild:
        def wait(self, *, timeout):
            waits.append(timeout)
            raise subprocess.TimeoutExpired("controlled child", timeout)

    handoff = native.Handoff(UnprovenChild(), {"deadline": 102.75})
    monkeypatch.setattr(native, "prepare_handoff", lambda *args, **kwargs: handoff)
    monkeypatch.setattr(native, "time", SimpleNamespace(monotonic=lambda: now))
    with pytest.raises(native.HandoffError, match="sealed deadline"):
        with native.handoff_scope(None, {}, runner_lock=None, terminal_lease=None):
            pass
    assert waits == [remaining + 3] and handoff.receipt == {"deadline": 102.75}


@pytest.mark.parametrize("entry", ["submit", "attach"])
@pytest.mark.parametrize("launch_error", [False, True])
def test_cli_creator_reaps_its_guardian_on_return_and_exception(tmp_path, monkeypatch, entry, launch_error):
    kernel = host_kernel(tmp_path)
    manager, fixture, response, calls = controlled_manager(tmp_path)
    state = json.loads(response.read_text())
    state["launch_returncode"] = 1
    response.write_text(json.dumps(state))
    environment = {key: os.environ[key] for key in ("PATH", "HOME", "XDG_RUNTIME_DIR", "XDG_DATA_HOME")}
    lock = tmp_path / "runner.lock"
    owned = []
    prepare = native.prepare_handoff

    def short_handoff(*args, **kwargs):
        handoff = prepare(*args, **kwargs, ttl_seconds=0.8)
        owned.append(handoff)
        return handoff

    monkeypatch.setattr(native, "prepare_handoff", short_handoff)
    if entry == "attach":
        directory = _runtime_directory("original", environment)
        kernel.admit_host("original", kind="run", command=fixture.command,
                          environment_carrier=directory / "environment.json", preflight=manager.preflight)
    if launch_error:
        launch = manager.launch

        def failed_reply(*args, **kwargs):
            launch(*args, **kwargs)
            raise RuntimeError("controlled caller failure after dispatch")

        monkeypatch.setattr(manager, "launch", failed_reply)

    def invoke():
        if entry == "submit":
            return submit(kernel, "run", action_id="original", command=fixture.command,
                          manager=manager, environment=environment, runner_lock=lock)
        return attach(kernel, requested_action="resume", manager=manager,
                      environment=environment, runner_lock=lock)

    try:
        if launch_error:
            with pytest.raises(RuntimeError, match="controlled caller failure"):
                invoke()
        else:
            result = invoke()
            assert result["host"] == "unknown" and result["action_id"] == "original"
        assert len(owned) == 1
        handoff = owned[0]
        # Assert before fallback cleanup; neither poll nor wait in this test
        # may make the public CLI's missing reap appear successful.
        assert handoff.process.returncode == 1
        assert not Path(f"/proc/{handoff.process.pid}").exists()
        assert time.monotonic() >= handoff.receipt["deadline"]
        usage = kernel.host_usage("original")
        assert usage.usage_pending and not usage.usage_released
        assert json.loads(usage.evidence) == handoff.receipt
        assert not Path(handoff.receipt["socket"]).exists()
        attached = attach(kernel, requested_action="resume", manager=manager,
                          environment=environment, runner_lock=lock)
        assert attached["action_id"] == "original" and attached["host"] == "unknown"
        assert len(owned) == 1  # An uncertain dispatch is never prepared/launched again.
        assert len([line for line in calls.read_text().splitlines()
                    if json.loads(line)[0] == "systemd-run"]) == 1
        with runner_management_lease(lock):
            pass
    finally:
        for handoff in owned:
            if handoff.process.poll() is None:
                handoff.process.wait(timeout=3)  # Only this test's own child, after verdict.


def test_exact_unreaped_guardian_exit_releases_only_after_all_host_evidence(tmp_path, monkeypatch):
    kernel = host_kernel(tmp_path)
    manager, fixture, response, calls = controlled_manager(tmp_path)
    private = tmp_path / "handoff"
    private.mkdir(mode=0o700)
    request = kernel.admit_host("original", kind="run", command=fixture.command,
                               environment_carrier=private / "environment.json", preflight=manager.preflight)
    state = json.loads(response.read_text())
    state["launch_returncode"] = 1
    state["fields"].update(Id=request.unit_name, Description=request.description,
                           ExecMainExitTimestamp=f"@{time.time()}")
    response.write_text(json.dumps(state))
    lock = tmp_path / "runner.lock"
    handoff = None
    try:
        with runner_usage_lease(lock) as terminal:
            handoff = native.prepare_handoff(request, {}, runner_lock=lock,
                                              terminal_lease=terminal, ttl_seconds=0.8)
            native.launch_handoff(kernel, "original", manager, handoff)
        assert not native.reconcile_handoff(kernel, "original", manager).usage_released
        descriptor = os.pidfd_open(handoff.process.pid)
        try:
            assert select.select([descriptor], [], [], 3)[0], "guardian did not naturally expire"
        finally:
            os.close(descriptor)
        assert handoff.process.returncode is None
        assert Path(f"/proc/{handoff.process.pid}/stat").read_text().split(") ", 1)[1].startswith("Z ")
        with runner_management_lease(lock):
            pass  # OS lease is gone; a missing wait must not block exact recovery.

        def unavailable_pidfd(pid, flags=0):
            raise OSError(errno.EPERM, "controlled observation unavailable")

        with monkeypatch.context() as observation:
            observation.setattr(native.os, "pidfd_open", unavailable_pidfd)
            assert not native.reconcile_handoff(kernel, "original", manager).usage_released
        with monkeypatch.context() as observation:
            observation.setattr(native, "_start_token", lambda pid: None)
            assert not native.reconcile_handoff(kernel, "original", manager).usage_released
        opened, tokens = [], []
        real_open, real_token = os.pidfd_open, native._start_token

        def capture_descriptor(pid, flags=0):
            descriptor = real_open(pid, flags)
            opened.append(descriptor)
            return descriptor

        def lose_binding_after_open(pid):
            tokens.append(pid)
            return real_token(pid) if len(tokens) == 1 else None

        with monkeypatch.context() as observation:
            observation.setattr(native.os, "pidfd_open", capture_descriptor)
            observation.setattr(native, "_start_token", lose_binding_after_open)
            assert not native.reconcile_handoff(kernel, "original", manager).usage_released
        assert len(opened) == 1 and tokens == [handoff.process.pid] * 2
        with pytest.raises(OSError) as closed:
            os.fstat(opened[0])
        assert closed.value.errno == errno.EBADF
        state["fields"]["NFileDescriptorStore"] = "1"
        response.write_text(json.dumps(state))
        assert not native.reconcile_handoff(kernel, "original", manager).usage_released
        state["fields"]["NFileDescriptorStore"] = "0"
        response.write_text(json.dumps(state))
        events = manager.cgroup_root / "unit-group" / "cgroup.events"
        events.write_text("populated 1\n")
        assert not native.reconcile_handoff(kernel, "original", manager).usage_released
        events.write_text("populated 0\n")
        released = native.reconcile_handoff(kernel, "original", manager)
        assert released.disposition == "exited" and released.usage_released
        assert json.loads(released.evidence) == handoff.receipt
        assert handoff.process.returncode is None and Path(f"/proc/{handoff.process.pid}").exists()
        before = calls.read_bytes()
        assert native.reconcile_handoff(kernel, "original", manager) == released
        assert calls.read_bytes() == before
        successor = kernel.admit_host("successor", kind="run", command=fixture.command,
                                     environment_carrier=private / "next.json", preflight=manager.preflight)
        assert successor.owner != request.owner and successor.epoch > request.epoch
    finally:
        if handoff is not None:
            handoff.process.wait(timeout=3)


def test_owned_scope_reaps_committed_guardian_without_waiting_for_host_lifetime(tmp_path):
    kernel = host_kernel(tmp_path)
    private = tmp_path / "handoff"
    private.mkdir(mode=0o700)
    request = kernel.admit_host("original", kind="run", command=("/bin/true",),
                               environment_carrier=private / "environment.json", preflight=lambda: None)
    lock = tmp_path / "runner.lock"
    stored, errors = [], []
    other = subprocess.Popen(["/bin/true"])
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as server:
        endpoint = str(private / "notify")
        server.bind(endpoint)
        server.settimeout(3)

        def acknowledge():
            try:
                for expected in (b"FDSTORE=1", b"BARRIER=1"):
                    data, ancillary, _, _ = server.recvmsg(1024, socket.CMSG_SPACE(array.array("i").itemsize))
                    assert data.startswith(expected)
                    for level, kind, value in ancillary:
                        assert (level, kind) == (socket.SOL_SOCKET, socket.SCM_RIGHTS)
                        descriptors = array.array("i")
                        descriptors.frombytes(value[:len(value) - len(value) % descriptors.itemsize])
                        for descriptor in descriptors:
                            if expected == b"FDSTORE=1":
                                stored.append(descriptor)
                            else:
                                os.close(descriptor)
            except BaseException as error:
                errors.append(error)

        receiver = threading.Thread(target=acknowledge)
        receiver.start()
        try:
            descriptor = os.pidfd_open(other.pid)
            try:
                assert select.select([descriptor], [], [], 3)[0]
            finally:
                os.close(descriptor)
            with runner_usage_lease(lock) as terminal:
                with native.handoff_scope(request, {}, runner_lock=lock, terminal_lease=terminal) as handoff:
                    with native.host_environment(request, runner_lock=lock,
                                                 credential=request.environment_carrier, notify_socket=endpoint):
                        pass
                assert handoff.process.returncode == 0
                assert not Path(f"/proc/{handoff.process.pid}").exists()
                assert time.monotonic() < handoff.receipt["deadline"]
                assert other.returncode is None and Path(f"/proc/{other.pid}").exists()
            receiver.join(timeout=4)
            assert not receiver.is_alive() and not errors and len(stored) == 1
            from agent_run.runtime.package_lease import RunnerLeaseBusy
            with pytest.raises(RunnerLeaseBusy):
                with runner_management_lease(lock):
                    raise AssertionError("guardian reap released the manager's separate retained FD")
        finally:
            other.wait(timeout=3)
            receiver.join(timeout=4)
            for descriptor in stored:
                os.close(descriptor)
