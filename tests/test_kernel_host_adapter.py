"""Real local CLI boundaries; controlled manager replies are not a live unit."""

import errno
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

import pytest

from agent_run.adapters.systemd import HostUnavailable, SystemdHost
from agent_run.model import Commit, Delivery, DeliveryKind
from agent_run.core.kernel import Kernel
from agent_run.errors import KernelError
from agent_run.model import RunSpec
from agent_run.model import HostLaunch


def test_missing_user_manager_refuses_host_readiness(tmp_path: Path) -> None:
    calls = tmp_path / "manager-calls.json"
    executable = tmp_path / "systemctl"
    executable.write_text(f"#!{sys.executable}\n" + f"""\
import json, sys
from pathlib import Path
Path({str(calls)!r}).write_text(json.dumps(sys.argv[1:]))
print('Failed to connect to user bus', file=sys.stderr)
raise SystemExit(1)
""")
    executable.chmod(0o700)
    host = SystemdHost(systemctl=str(executable), systemd_run="/unused-systemd-run",
                       environment={"PATH": os.defpath, "HOME": str(tmp_path)})
    with pytest.raises(HostUnavailable, match="user systemd"):
        host.preflight()
    assert json.loads(calls.read_text()) == ["--user", "show", "--property=Version", "--value"]


def controlled_manager(tmp_path: Path) -> tuple[SystemdHost, HostLaunch, Path, Path]:
    """Only systemd CLI responses/sysfs are synthetic; helpers really execute."""
    carrier = tmp_path / "environment-carrier"
    carrier.write_text("Private test environment material; never an online credential")
    carrier.chmod(0o600)
    request = HostLaunch("run-1", 3, "executor-3", "nonce-3", "agent-run-nonce-3.service",
                         (sys.executable, "-c", "print('$literal')"), carrier)
    response, calls = tmp_path / "manager-response.json", tmp_path / "helper-calls.jsonl"
    group = tmp_path / "cgroups" / "unit-group"
    group.mkdir(parents=True)
    (group / "cgroup.events").write_text("populated 0\nfrozen 0\n")
    fields = {
        "Id": request.unit_name, "Description": 'agent-run-kernel:["run-1",3,"executor-3","nonce-3"]', "LoadState": "loaded",
        "ActiveState": "active", "SubState": "exited", "InvocationID": "1" * 32,
        "ControlGroup": "/unit-group", "MainPID": "0", "ControlPID": "0", "ExecMainCode": "1",
        "ExecMainStatus": "7", "ExecMainExitTimestamp": "@1700000000.25",
        "ExecMainExitTimestampMonotonic": "1234567",
        "NFileDescriptorStore": "0",
    }
    response.write_text(json.dumps({"fields": fields, "launch_returncode": 0}))
    program = f"#!{sys.executable}\n" + f"""\
import json, sys
from pathlib import Path
response, calls = Path({str(response)!r}), Path({str(calls)!r})
with calls.open('a') as stream:
    stream.write(json.dumps([Path(sys.argv[0]).name, *sys.argv[1:]]) + '\\n')
state = json.loads(response.read_text())
if Path(sys.argv[0]).name == 'systemd-run':
    raise SystemExit(state['launch_returncode'])
if '--property=Version' in sys.argv:
    print('257')
else:
    for key, value in state['fields'].items():
        print(key + '=' + value)
    if 'next_fields' in state:
        state['fields'] = state.pop('next_fields')
        response.write_text(json.dumps(state))
"""
    for name in ("systemctl", "systemd-run"):
        executable = tmp_path / name
        executable.write_text(program)
        executable.chmod(0o700)
    host = SystemdHost(systemctl=str(tmp_path / "systemctl"), systemd_run=str(tmp_path / "systemd-run"),
                       environment={"PATH": os.defpath, "HOME": str(tmp_path)}, cgroup_root=tmp_path / "cgroups")
    return host, request, response, calls


def test_launch_uses_exact_grant_and_unknown_response_never_retries(tmp_path: Path) -> None:
    host, request, response, calls = controlled_manager(tmp_path)
    state = json.loads(response.read_text())
    state["launch_returncode"] = 1  # Manager accepted the unit but its reply was lost.
    response.write_text(json.dumps(state))
    host.preflight()
    result = host.launch(request, dispatch=lambda: True)
    assert result is not None and result.disposition == "unknown" and result.ended_at is None
    before = calls.read_bytes()
    reopened = SystemdHost(systemctl=host.systemctl, systemd_run=host.systemd_run,
                           environment=host.environment, cgroup_root=host.cgroup_root)
    assert reopened.launch(request, dispatch=lambda: False) is None
    assert calls.read_bytes() == before
    launches = [json.loads(line) for line in before.decode().splitlines()
                if json.loads(line)[0] == "systemd-run"]
    assert len(launches) == 1
    arguments = launches[0][1:]
    assert "--unit=agent-run-nonce-3.service" in arguments
    assert '--description=agent-run-kernel:["run-1",3,"executor-3","nonce-3"]' in arguments
    assert "--property=Restart=no" in arguments and "--property=Type=exec" in arguments
    assert "--property=KillMode=control-group" in arguments and "--property=RemainAfterExit=no" in arguments
    assert "--property=FileDescriptorStoreMax=1" in arguments
    assert "--property=FileDescriptorStorePreserve=restart" in arguments and "--property=NotifyAccess=main" in arguments
    assert "--expand-environment=no" in arguments and "--collect" not in arguments
    assert f"--property=LoadCredential=agent-run-environment:{request.environment_carrier}" in arguments
    assert arguments[arguments.index("--") + 1:] == list(request.command)
    assert request.environment_carrier.read_text() not in before.decode()
    # Reconciliation reads the original unit and exact witness; it never restarts it.
    observed = reopened.observe(request)
    assert observed.disposition == "exited" and observed.invocation_id == "1" * 32
    assert observed.cgroup == "/unit-group" and observed.exit_status == 7
    assert observed.ended_at == 1700000000.25
    assert len([line for line in calls.read_text().splitlines() if json.loads(line)[0] == "systemd-run"]) == 1


@pytest.mark.parametrize("fault", ["nonce", "missing-invocation", "populated", "missing-exit-time", "replaced", "absent"])
def test_uncertain_host_identity_or_descendant_exit_never_becomes_exit(tmp_path: Path, fault: str) -> None:
    host, request, response, _ = controlled_manager(tmp_path)
    state = json.loads(response.read_text())
    if fault == "nonce":
        state["fields"]["Description"] = "Another launch with the same PID or unit name"
    elif fault == "missing-invocation":
        state["fields"]["InvocationID"] = ""
    elif fault == "populated":
        (host.cgroup_root / "unit-group" / "cgroup.events").write_text("populated 1\n")
    elif fault == "missing-exit-time":
        state["fields"]["ExecMainExitTimestamp"] = ""
    elif fault == "replaced":
        state["next_fields"] = {**state["fields"], "InvocationID": "2" * 32}
    elif fault == "absent":
        state["fields"]["LoadState"] = "not-found"
    response.write_text(json.dumps(state))
    observed = host.observe(request)
    assert observed.disposition == "unknown" and observed.ended_at is None and observed.exit_status is None


def test_running_host_requires_real_main_process_start_and_cgroup(tmp_path: Path) -> None:
    host, request, response, _ = controlled_manager(tmp_path)
    process = subprocess.Popen([sys.executable, "-I", "-c", "import signal; signal.pause()"], start_new_session=True)
    try:
        state = json.loads(response.read_text())
        membership = next(line[3:] for line in Path(f"/proc/{process.pid}/cgroup").read_text().splitlines()
                          if line.startswith("0::"))
        state["fields"].update({"ActiveState": "active", "SubState": "running", "MainPID": str(process.pid),
                                "ControlGroup": membership})
        response.write_text(json.dumps(state))
        observed = host.observe(request)
        assert observed.disposition == "running" and observed.main_pid == process.pid
        assert observed.start_token and observed.boot_id and observed.ended_at is None
        assert not observed.usage_ready and not observed.usage_released
        state["fields"]["ControlGroup"] = "/different-unit"
        response.write_text(json.dumps(state))
        assert host.observe(request).disposition == "unknown"
    finally:
        process.terminate()
        process.wait(timeout=3)
    state["fields"]["ControlGroup"] = membership
    response.write_text(json.dumps(state))
    assert host.observe(request).disposition == "unknown"  # PID absence is not Host exit.


def _unavailable_manager(tmp_path: Path, *, missing_tools: bool = False) -> tuple[SystemdHost, str]:
    systemctl, systemd_run = shutil.which("systemctl"), shutil.which("systemd-run")
    if missing_tools or systemctl is None or systemd_run is None:
        # A real exec failure, never a substitute systemctl implementation.
        absent = tmp_path / "absent-tools"
        absent.mkdir()
        systemctl, systemd_run = str(absent / "systemctl"), str(absent / "systemd-run")
        capability = "missing-tools"
    else:
        capability = "missing-bus"
    return SystemdHost(systemctl=systemctl, systemd_run=systemd_run, environment={
        "PATH": os.defpath, "HOME": str(tmp_path), "XDG_RUNTIME_DIR": str(tmp_path / "absent-runtime"),
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=" + str(tmp_path / "absent-user-bus"),
    }), capability


def test_real_unavailable_manager_refuses_host_readiness(tmp_path: Path, record_property) -> None:
    host, capability = _unavailable_manager(tmp_path)
    record_property("unavailable_manager_capability", capability)
    with pytest.raises(HostUnavailable, match="user systemd"):
        host.preflight()
    _assert_unavailable_manager_precedes_admission(tmp_path, host, capability)


def host_kernel(tmp_path: Path) -> Kernel:
    base = Commit("base", "tree")
    delivery = Delivery("parent", DeliveryKind.PARENT_ONLY, 1, "requirements", base, "source", "target")
    return Kernel.create(tmp_path / "state" / "kernel.sqlite3", RunSpec(
        "host-run", "example/repository", 1, "requirements", "graph", (delivery,),
        language="en", repository_root=str(tmp_path / "repository"),
    ))


def _assert_unavailable_manager_precedes_admission(tmp_path: Path, host: SystemdHost, capability: str) -> None:
    from agent_run.contracts.task import TaskKey
    from agent_run.storage.tasks import read_task, task_path
    kernel = host_kernel(tmp_path)
    carrier = tmp_path / "carrier"
    task = TaskKey(tmp_path / "repository", "example/repository", 1)
    assert read_task(task_path(task)) is None and not carrier.exists()
    with pytest.raises(HostUnavailable, match="user systemd") as rejected:
        kernel.admit_host("start", kind="run", command=(sys.executable, "-c", "pass"),
                          environment_carrier=carrier, preflight=host.preflight)
    if capability == "missing-tools":
        cause = rejected.value.__cause__
        assert isinstance(cause, FileNotFoundError) and cause.errno == errno.ENOENT
        assert cause.filename == host.systemctl
    else:
        assert rejected.value.__cause__ is None  # A real CLI reply, not an exec/permission failure.
    with kernel.storage.transaction(write=False) as tx:
        assert tx.one("SELECT count(*) FROM actions")[0] == 0
        assert tx.one("SELECT count(*) FROM hosts")[0] == 0
        assert tx.one("SELECT executor FROM runs")[0] is None
    assert read_task(task_path(task)) is None and not carrier.exists()


def test_real_unavailable_manager_rejects_kernel_admission_before_action(tmp_path: Path, record_property) -> None:
    host, capability = _unavailable_manager(tmp_path)
    record_property("unavailable_manager_capability", capability)
    _assert_unavailable_manager_precedes_admission(tmp_path, host, capability)


def test_real_missing_manager_tools_reject_kernel_admission_before_action(tmp_path: Path, record_property) -> None:
    host, capability = _unavailable_manager(tmp_path, missing_tools=True)
    record_property("unavailable_manager_capability", capability)
    _assert_unavailable_manager_precedes_admission(tmp_path, host, capability)


def test_real_manager_boundary_reports_unknown_and_reconciles_original_kernel_grant(tmp_path: Path) -> None:
    kernel = host_kernel(tmp_path)
    host, original_fixture, response, calls = controlled_manager(tmp_path)
    launch = kernel.admit_host("start", kind="run", command=original_fixture.command,
                               environment_carrier=original_fixture.environment_carrier, preflight=host.preflight)
    state = json.loads(response.read_text())
    state["fields"].update(Id=launch.unit_name, Description=launch.description,
                           ExecMainExitTimestamp=f"@{time.time()}")
    state["launch_returncode"] = 1
    response.write_text(json.dumps(state))
    observed = host.launch(launch, dispatch=lambda: kernel.dispatch_host(launch))
    assert observed is not None and observed.disposition == "unknown"
    assert kernel.observe_host(launch, observed) == "unknown"
    reopened = Kernel(kernel.storage.path)
    before = calls.read_bytes()
    assert host.launch(launch, dispatch=lambda: reopened.dispatch_host(launch)) is None
    assert calls.read_bytes() == before
    with reopened.storage.transaction(write=False) as tx:
        assert tx.one("SELECT executor FROM runs")[0] == launch.owner
    ended = host.observe(launch)
    assert ended.disposition == "exited" and ended.invocation_id == "1" * 32
    assert ended.main_pid is None and ended.start_token is None
    assert not ended.usage_ready and not ended.usage_released
    assert reopened.observe_host(launch, ended) == "exited"
    with reopened.storage.transaction(write=False) as tx:
        assert tx.one("SELECT executor FROM runs")[0] is None
        assert tuple(tx.one("SELECT usage_ready,usage_released FROM hosts")) == (0, 0)
    with pytest.raises(KernelError, match="executor_exit_unproven"):
        reopened.admit_host("next-start", kind="run", command=launch.command,
                            environment_carrier=launch.environment_carrier, preflight=host.preflight)
    launches = [json.loads(line) for line in calls.read_text().splitlines() if json.loads(line)[0] == "systemd-run"]
    assert len(launches) == 1
    assert "--unit=" + launch.unit_name in launches[0]
    assert "--description=" + launch.description in launches[0]
