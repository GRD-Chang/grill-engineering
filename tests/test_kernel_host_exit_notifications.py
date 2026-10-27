"""Finite post-exit credentials use the original Host and Runner lease."""

from support.repository_fixture import create_kernel
from dataclasses import asdict
import json
import os
from pathlib import Path
import shutil
import sys
import pytest

from agent_run.runtime.package_lease import runner_usage_lease
from agent_run.model import HostObservation
from agent_run.runtime.handoff import prepare_handoff
from test_kernel_host_adapter import host_kernel


def test_original_private_credential_keeps_runner_binding_after_startup_expiry(tmp_path: Path) -> None:
    kernel = host_kernel(tmp_path)
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    lock = tmp_path / "runner.lock"
    request = kernel.admit_host("original", kind="run", command=(sys.executable, "-c", "pass"),
                               environment_carrier=private / "environment.json", preflight=lambda: None)
    handoff = None
    try:
        with runner_usage_lease(lock) as terminal:
            handoff = prepare_handoff(request, {"PATH": os.defpath, "PRIVATE_TEST_VALUE": "original"},
                                       runner_lock=lock, terminal_lease=terminal, ttl_seconds=0.5)
            credential = private / "manager-private-copy"
            shutil.copyfile(request.environment_carrier, credential)
            credential.chmod(0o600)
            assert kernel.dispatch_host(request)
            encoded = json.dumps(handoff.receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            kernel.observe_host(request, HostObservation("unknown", request.unit_name, evidence=encoded,
                                                        usage_pending=True))
            # The real guardian expires and removes only its source carrier;
            # the manager-owned private copy survives until hook completion.
            assert handoff.process.wait(timeout=3) == 1
            assert not request.environment_carrier.exists()
            held = os.fstat(terminal.fileno())
            import agent_run.runtime.handoff as native
            assert handoff.receipt["runner"] == {
                "package_root": str(Path(native.__file__).resolve().parents[2]),
                "python": sys.executable, "lock": str(lock.resolve()),
                "device": held.st_dev, "inode": held.st_ino,
            }
            original = kernel.host_usage("original")
            assert original is not None and json.loads(original.evidence) == handoff.receipt
            from agent_run.runtime.handoff import read_exit_environment
            inherited = read_exit_environment(request, runner_lock=lock, credential=credential)
            assert inherited.environment == {"PATH": os.defpath, "PRIVATE_TEST_VALUE": "original"}
            assert inherited.receipt == handoff.receipt
            assert asdict(kernel.host_request("original")) == asdict(request)
    finally:
        if handoff is not None and handoff.process.poll() is None:
            handoff.process.terminate()
            handoff.process.wait(timeout=3)


def test_killed_original_host_notice_is_exact_current_and_business_read_only(tmp_path: Path) -> None:
    from copy import deepcopy
    import select
    import subprocess
    from agent_run.runtime.process_identity import _process_start_token
    from agent_run.runtime.execution import _cgroup
    from test_kernel_notifications import _kernel

    kernel = _kernel(tmp_path)
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    lock = tmp_path / "runner.lock"
    command = (sys.executable, "-I", "-c", "import signal; print('ready',flush=True); signal.pause()")
    request = kernel.admit_host("original", kind="run", command=command,
                               environment_carrier=private / "environment.json", preflight=lambda: None)
    child = subprocess.Popen(command, stdout=subprocess.PIPE, start_new_session=True)
    handoff = None
    try:
        assert child.stdout is not None and select.select([child.stdout], [], [], 3)[0]
        assert child.stdout.readline() == b"ready\n"
        token = _process_start_token(child.pid)
        assert token
        with runner_usage_lease(lock) as terminal:
            handoff = prepare_handoff(request, {"PATH": os.defpath}, runner_lock=lock,
                                       terminal_lease=terminal, ttl_seconds=0.5)
            assert kernel.dispatch_host(request)
            encoded = json.dumps(handoff.receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            kernel.observe_host(request, HostObservation("unknown", request.unit_name, evidence=encoded,
                                                        usage_pending=True))
            kernel.observe_host(request, HostObservation(
                "running", request.unit_name, invocation_id="1" * 32, cgroup=_cgroup(child.pid),
                boot_id=Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
                main_pid=child.pid, start_token=token, evidence=encoded, usage_ready=True))
            child.kill()
            assert child.wait(timeout=3) == -9
            before = kernel.storage.path.read_bytes()
            from agent_run.queries.host_exit import host_exit_notice, host_exit_current
            from agent_run.notifications.event_projection import host_exit_event
            notice = host_exit_notice(kernel.storage.path, 'original', handoff.receipt, service_result='signal', exit_code='killed', exit_status='KILL', invocation_id='1' * 32, storage=kernel.storage)
            assert notice is not None
            assert {key: notice[key] for key in ("run_id", "action_id", "epoch", "owner", "nonce")} == {
                "run_id": "run-native", "action_id": "original", "epoch": request.epoch,
                "owner": request.owner, "nonce": request.nonce,
            }
            assert notice["runner"] == handoff.receipt["runner"]
            assert notice["pid"] == child.pid and notice["process_start_token"] == token
            assert (notice["service_result"], notice["exit_code"], notice["exit_status"]) == ("signal", "killed", "KILL")
            assert host_exit_current(kernel.storage.path, notice, storage=kernel.storage)
            unavailable = kernel.storage.path.with_suffix(".temporarily-unavailable")
            kernel.storage.path.rename(unavailable)
            try:
                with pytest.raises(RuntimeError, match="unavailable"):
                    host_exit_current(kernel.storage.path, notice, storage=kernel.storage)
            finally:
                unavailable.rename(kernel.storage.path)
            assert host_exit_current(kernel.storage.path, notice, storage=kernel.storage)
            event = host_exit_event(kernel.storage.path, notice, storage=kernel.storage)
            assert event is not None and event["kind"] == "executor_exit" and event["important"] is True
            assert event["exit_evidence"] == notice
            duplicate = host_exit_notice(kernel.storage.path, 'original', handoff.receipt, service_result='signal', exit_code='killed', exit_status='KILL', invocation_id='1' * 32, storage=kernel.storage)
            assert duplicate is not None
            assert host_exit_event(kernel.storage.path, duplicate, storage=kernel.storage)["id"] == event["id"]
            assert host_exit_notice(kernel.storage.path, 'original', handoff.receipt, service_result='success', exit_code='exited', exit_status='0', invocation_id='1' * 32, storage=kernel.storage) is None
            for field, wrong in (("nonce", "another-nonce"), ("action_id", "another-action"),
                                 ("epoch", request.epoch + 1), ("owner", "another-owner"),
                                 ("run_id", "another-run"), ("invocation_id", "2" * 32),
                                 ("workspace", str(tmp_path / "another-workspace")),
                                 ("request_digest", "another-request")):
                changed = {**notice, field: wrong}
                assert not host_exit_current(kernel.storage.path, changed, storage=kernel.storage), field
            changed = deepcopy(notice)
            changed["runner"]["inode"] += 1
            assert not host_exit_current(kernel.storage.path, changed, storage=kernel.storage)
            assert kernel.storage.path.read_bytes() == before
            assert not (tmp_path / "notifications").exists()  # Projection is not a sender.
            with kernel.storage.transaction(write=False) as tx:
                host = tx.one("SELECT status,usage_released FROM hosts")
                assert tuple(host) == ("running", 0)  # Main exit does not prove Host release.
            kernel.stop_task("ticket-stop", subject="native-ticket")
            assert kernel.operation_state().disposition == "quiescent"
            with kernel.storage.transaction(write=False) as tx:
                assert tuple(tx.one("SELECT stopped,executor,epoch FROM runs")) == (0, request.owner, request.epoch)
            assert not host_exit_current(kernel.storage.path, notice, storage=kernel.storage)  # Original normal wait, same Host.
            kernel.stop_task("stop")
            assert not host_exit_current(kernel.storage.path, notice, storage=kernel.storage)
            assert host_exit_event(kernel.storage.path, notice, storage=kernel.storage) is None
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=3)
        if child.stdout is not None:
            child.stdout.close()
        if handoff is not None and handoff.process.poll() is None:
            handoff.process.terminate()
            handoff.process.wait(timeout=3)


def test_startup_exit_requires_exact_manager_identity_when_readiness_was_never_recorded(
    tmp_path: Path, monkeypatch,
) -> None:
    import subprocess
    from agent_run.queries.host_exit import host_exit_notice, host_exit_current
    from support.systemd_manager import controlled_manager
    from test_kernel_notifications import _kernel

    kernel = _kernel(tmp_path)
    manager, _, response, _ = controlled_manager(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    lock = tmp_path / "runner.lock"
    command = (sys.executable, "-I", "-c", "raise SystemExit(7)")
    request = kernel.admit_host("startup", kind="run", command=command,
                               environment_carrier=private / "environment.json", preflight=manager.preflight)
    state = json.loads(response.read_text())
    state["fields"].update(Id=request.unit_name, Description=request.description)
    response.write_text(json.dumps(state))
    child = subprocess.Popen(command)
    handoff = None
    try:
        assert child.wait(timeout=3) == 7
        with runner_usage_lease(lock) as terminal:
            handoff = prepare_handoff(request, {"PATH": os.environ["PATH"]}, runner_lock=lock,
                                       terminal_lease=terminal, ttl_seconds=0.5)
            assert kernel.dispatch_host(request)
            encoded = json.dumps(handoff.receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            kernel.observe_host(request, HostObservation("unknown", request.unit_name, evidence=encoded,
                                                        usage_pending=True))
            before = kernel.storage.path.read_bytes()
            notice = host_exit_notice(kernel.storage.path, 'startup', handoff.receipt, service_result='exit-code', exit_code='exited', exit_status='7', invocation_id='1' * 32, storage=kernel.storage)
            assert notice is not None
            assert notice["pid"] is None and notice["process_start_token"] is None
            assert host_exit_current(kernel.storage.path, notice, storage=kernel.storage)
            with kernel.storage.transaction(write=False) as tx:
                original = tx.one("SELECT invocation_id,status,usage_ready,usage_released FROM hosts")
                assert tuple(original) == (None, "unknown", 0, 0)
            complete = dict(state["fields"])
            del state["fields"]["InvocationID"]
            response.write_text(json.dumps(state))
            with pytest.raises(RuntimeError, match="unavailable"):
                host_exit_current(kernel.storage.path, notice, storage=kernel.storage)
            state["fields"] = complete
            response.write_text(json.dumps(state))
            assert host_exit_current(kernel.storage.path, notice, storage=kernel.storage)
            state["fields"]["Description"] = "another Host with the same service name"
            response.write_text(json.dumps(state))
            assert not host_exit_current(kernel.storage.path, notice, storage=kernel.storage)
            state["fields"].update(Description=request.description, InvocationID="2" * 32)
            response.write_text(json.dumps(state))
            assert not host_exit_current(kernel.storage.path, notice, storage=kernel.storage)
            state["fields"]["InvocationID"] = "1" * 32
            state["next_fields"] = {**state["fields"], "InvocationID": "2" * 32}
            response.write_text(json.dumps(state))
            assert not host_exit_current(kernel.storage.path, notice, storage=kernel.storage)  # Changed between the two real CLI reads.
            assert kernel.storage.path.read_bytes() == before
            assert not (tmp_path / "notifications").exists()
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=3)
        if handoff is not None and handoff.process.poll() is None:
            handoff.process.terminate()
            handoff.process.wait(timeout=3)


def test_enabled_host_arms_only_the_exact_bounded_exit_command_without_private_environment(tmp_path: Path, monkeypatch) -> None:
    from agent_run.contracts.prompt import RESOURCE_KEYS
    from agent_run.config.defaults import UserDefaultsStore
    from agent_run.model import Commit, Delivery, DeliveryKind, RunSpec
    from agent_run.core.kernel import Kernel
    from agent_run.config.creation import freeze_creation
    from support.systemd_manager import controlled_manager
    from test_kernel_notifications import _kernel

    kernel = _kernel(tmp_path)
    manager, fixture, _, calls = controlled_manager(tmp_path)
    private = tmp_path / "enabled-private"
    private.mkdir(mode=0o700)
    request = kernel.admit_host("enabled", kind="run", command=fixture.command,
                               environment_carrier=private / "environment.json", preflight=manager.preflight)
    lock = tmp_path / "runner.lock"
    environment = {"PATH": os.defpath, "PRIVATE_TEST_VALUE": "must-never-enter-unit-properties",
                   "LD_LIBRARY_PATH": "/test/sqlite path%literal/$library"}
    from agent_run.cli.host_exit import exit_properties
    properties = exit_properties(kernel.storage.path, 'enabled', request, lock, environment, storage=kernel.storage)
    command, = [value for value in properties if value.startswith("ExecStopPost=")]
    assert command.startswith("ExecStopPost=-:") and "from agent_run.cli.host_exit import main" in command
    assert environment["PRIVATE_TEST_VALUE"] not in " ".join(properties)
    assert "path%%literal/$library" in command
    assert "TimeoutStopSec=70s" in properties and "TimeoutStopFailureMode=kill" in properties
    assert "OOMPolicy=continue" in properties
    import agent_run.runtime.handoff as native_host
    from agent_run.cli.lifecycle import attach
    guardians = []
    original_prepare = native_host.prepare_handoff
    def short_handoff(*args, **kwargs):
        handoff = original_prepare(*args, **kwargs, ttl_seconds=0.5)
        guardians.append(handoff.process)
        return handoff
    monkeypatch.setattr(native_host, "prepare_handoff", short_handoff)
    try:
        result = attach(kernel, requested_action="resume", manager=manager, environment=environment, runner_lock=lock)
        assert result["result"] == "attached" and result["action_id"] == "enabled"
        assert result["requested_action_applied"] is False
    finally:
        for guardian in guardians:
            assert guardian.wait(timeout=3) == 1
    launch, = [json.loads(line) for line in calls.read_text().splitlines()
               if json.loads(line)[0] == "systemd-run"]
    assert "--property=" + command in launch
    assert "--property=TimeoutStopSec=70s" in launch and "--property=TimeoutStopSec=5s" not in launch
    assert "--property=FileDescriptorStorePreserve=restart" in launch
    assert launch.count("--property=LoadCredential=agent-run-environment:" + str(request.environment_carrier)) == 1
    assert environment["PRIVATE_TEST_VALUE"] not in json.dumps(launch)

    disabled = tmp_path / "disabled"
    disabled.mkdir()
    defaults = UserDefaultsStore(disabled / "defaults.json")
    defaults.configure(language="en", notifications={"enabled": False})
    delivery = Delivery("parent", DeliveryKind.PARENT_ONLY, 1, "requirements", Commit("base", "tree"), "source", "target")
    spec = freeze_creation(RunSpec("disabled", "example/project", 1, "requirements", "graph", (delivery,),
                                   repository_root=str(disabled)), defaults,
                           prompt_resources={key: "Frozen " + key for key in RESOURCE_KEYS})
    other = create_kernel(disabled / "kernel.sqlite3", spec)
    off = other.admit_host("disabled", kind="run", command=fixture.command,
                           environment_carrier=disabled / "environment.json", preflight=lambda: None)
    status_properties = exit_properties(other.storage.path, 'disabled', off, lock, environment, storage=other.storage)
    assert len([item for item in status_properties if item.startswith("ExecStopPost=")]) == 1
    assert "TimeoutStopSec=5s" in status_properties and "TimeoutStopSec=70s" not in status_properties
    assert not (disabled / "notifications").exists()


def test_real_post_exit_entry_restores_only_original_environment_and_calls_the_one_sender(tmp_path: Path) -> None:
    import select
    import subprocess
    from agent_run.runtime.process_identity import _process_start_token
    from agent_run.runtime.package_lease import runner_management_lease
    from agent_run.runtime.execution import _cgroup
    from test_kernel_notifications import _kernel
    import agent_run.runtime.handoff as native

    kernel = _kernel(tmp_path)
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    credentials = private / "credentials"
    credentials.mkdir(mode=0o700)
    lock = tmp_path / "runner.lock"
    captured = {**os.environ, "HOST_EXIT_TEST_VALUE": "terminal-original"}
    command = (sys.executable, "-I", "-c", "import signal; print('ready',flush=True); signal.pause()")
    request = kernel.admit_host("entry", kind="run", command=command,
                               environment_carrier=private / "environment.json", preflight=lambda: None)
    child = subprocess.Popen(command, stdout=subprocess.PIPE, start_new_session=True)
    handoff = hook = None
    try:
        assert child.stdout is not None and select.select([child.stdout], [], [], 3)[0]
        assert child.stdout.readline() == b"ready\n"
        token = _process_start_token(child.pid)
        assert token
        with runner_usage_lease(lock) as terminal:
            handoff = prepare_handoff(request, captured, runner_lock=lock, terminal_lease=terminal, ttl_seconds=0.5)
            shutil.copyfile(request.environment_carrier, credentials / "agent-run-environment")
            (credentials / "agent-run-environment").chmod(0o600)
            assert kernel.dispatch_host(request)
            encoded = json.dumps(handoff.receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            kernel.observe_host(request, HostObservation("unknown", request.unit_name, evidence=encoded, usage_pending=True))
            kernel.observe_host(request, HostObservation(
                "running", request.unit_name, invocation_id="1" * 32, cgroup=_cgroup(child.pid),
                boot_id=Path("/proc/sys/kernel/random/boot_id").read_text().strip(), main_pid=child.pid,
                start_token=token, evidence=encoded, usage_ready=True))
            child.kill()
            assert child.wait(timeout=3) == -9
            assert handoff.process.wait(timeout=3) == 1
        with runner_management_lease(lock):
            pass  # Neither the terminal nor expired guardian holds this test lock.
        output = tmp_path / "captured-notice.json"
        bootstrap = f"import sys,os,json,fcntl\nfrom pathlib import Path\nsys.path.insert(0,sys.argv[1])\nimport agent_run.notifications.sender as notifications\ndef capture(database,state_root,notice):\n    assert os.environ['HOST_EXIT_TEST_VALUE']=='terminal-original'\n    with open({str(lock)!r},'r') as held:\n        try:\n            fcntl.flock(held.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)\n        except BlockingIOError:\n            pass\n        else:\n            raise AssertionError('post-exit entry did not hold its original Runner lease')\n    Path({str(output)!r}).write_text(json.dumps({{'notice':notice,'database':str(database),'state_root':str(state_root)}}))\nnotifications.deliver_host_exit_notice=capture\nfrom agent_run.cli.host_exit import main\nraise SystemExit(main(sys.argv[2:]))\n"
        environment = {"PATH": os.defpath, "CREDENTIALS_DIRECTORY": str(credentials),
                       "SERVICE_RESULT": "signal", "EXIT_CODE": "killed", "EXIT_STATUS": "KILL",
                       "INVOCATION_ID": "1" * 32, "HOST_EXIT_TEST_VALUE": "manager-wrong"}
        if captured.get("LD_LIBRARY_PATH"):
            environment["LD_LIBRARY_PATH"] = captured["LD_LIBRARY_PATH"]
        before = kernel.storage.path.read_bytes()
        hook = subprocess.Popen([sys.executable, "-I", "-c", bootstrap, str(Path(native.__file__).resolve().parents[2]),
                                 "--database", str(kernel.storage.path), "--action", "entry", "--runner-lock", str(lock)],
                                env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        stdout, stderr = hook.communicate(timeout=8)
        assert hook.returncode == 0, stderr.decode()
        assert stdout == b"" and stderr == b""
        delivered = json.loads(output.read_text())
        assert delivered["database"] == str(kernel.storage.path) and delivered["state_root"] == str(tmp_path)
        assert delivered["notice"]["action_id"] == "entry" and delivered["notice"]["nonce"] == request.nonce
        assert delivered["notice"]["exit_status"] == "KILL"
        assert delivered["notice"]["runner"] == handoff.receipt["runner"]
        assert kernel.storage.path.read_bytes() == before and not (tmp_path / "notifications").exists()
        with runner_management_lease(lock):
            pass
    finally:
        if hook is not None and hook.poll() is None:
            import signal
            os.killpg(hook.pid, signal.SIGKILL)
            hook.wait(timeout=3)
        if child.poll() is None:
            child.kill()
            child.wait(timeout=3)
        if child.stdout is not None:
            child.stdout.close()
        if handoff is not None and handoff.process.poll() is None:
            handoff.process.terminate()
            handoff.process.wait(timeout=3)
