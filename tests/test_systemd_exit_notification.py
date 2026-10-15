"""Real forced death through the formal host adapter, with an isolated manager."""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
import venv
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from agent_run.notifications import read_notifications
from agent_run import cli
from agent_run.runner_lease import RunnerLeaseBusy, default_runner_lock_path, runner_management_lease
from agent_run.systemd_executor_host import (
    FakeSystemdTransport, SystemdStartResult, SystemdUnitObservation, SystemdUserExecutorHost,
)
from support.executor_exit import create_exit_run


@pytest.mark.parametrize("enabled", [True, False], ids=["automatic-alert", "disabled"])
def test_forced_executor_death_notifies_without_further_lifecycle_commands(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], enabled: bool,
) -> None:
    states, control, spec = create_exit_run(tmp_path, enabled=enabled, reserved=False)
    spec = replace(spec, command=("run", "1"))
    snapshot = tmp_path / "snapshot"
    venv.EnvBuilder(with_pip=False, symlinks=True, system_site_packages=True).create(snapshot)
    source = Path(__file__).resolve().parents[1] / "src"
    site = snapshot / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"
    (site / "source.pth").write_text(str(source))
    environment = {key: value for key, value in os.environ.items()
                   if key in {"PATH", "HOME", "TMPDIR"} or key.startswith("XDG_")}
    environment["PYTHONPATH"] = str(source)
    environment.update(
        GH_TOKEN="fixture-runner-secret", AGENT_RUN_GITHUB_APP_PRIVATE_KEY="fixture-private-key",
        # The terminal snapshot must not replace systemd's native location.
        AGENT_RUN_INTERNAL_CREDENTIALS_DIRECTORY="untrusted-terminal-value",
        AGENT_RUN_INTERNAL_SYSTEMD_RUNTIME_DIRECTORY="untrusted-terminal-runtime",
    )
    # Actual existing transport/identity validation runs against a local CLI.
    binary = tmp_path / "bin"
    binary.mkdir()
    lark = binary / "lark-cli"
    requests = tmp_path / "requests.jsonl"
    lark.write_text(f"#!{sys.executable}\n" + """import json, os, sys
from pathlib import Path
args = sys.argv[1:]
if 'whoami' in args:
    print(json.dumps({'appId':'cli_test', 'profile':'work', 'identity':'bot', 'available':True}))
else:
    with Path(os.environ['EXIT_TEST_REQUESTS']).open('a') as stream:
        stream.write(json.dumps(args) + '\\n')
    print(json.dumps({'data':{'message_id':'fixture-message'}}))
""")
    lark.chmod(0o700)
    environment.update(PATH=str(binary) + os.pathsep + environment["PATH"], EXIT_TEST_REQUESTS=str(requests))
    # Manager state can live below an arbitrarily long TMPDIR. Its notification
    # address must not inherit the filesystem path's Unix socket length limit.
    launch_root = tmp_path / ("manager-" + "x" * 108)
    launch_root.mkdir()
    manager: subprocess.Popen[str] | None = None

    class IsolatedManager(FakeSystemdTransport):
        def start(self, **kwargs: Any) -> SystemdStartResult:
            nonlocal manager
            super().start(**kwargs)
            launch = {"root": str(launch_root), "workspace": str(spec.task.workspace),
                      "repository": spec.task.repository, "parent": spec.task.parent_number,
                      "state_root": str(states.root), "action_id": spec.action_id,
                      "generation": spec.generation, "properties": kwargs["properties"],
                      "command": kwargs["command"]}
            path = launch_root / "launch.json"
            path.write_text(json.dumps(launch))
            manager = subprocess.Popen(
                [sys.executable, str(Path(__file__).parent / "support" / "systemd_exit_manager.py"), str(path)],
                env=environment, start_new_session=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            deadline = time.monotonic() + 5
            while not (launch_root / "ready").exists():
                assert manager.poll() is None, manager.communicate(timeout=1)
                assert time.monotonic() < deadline
                time.sleep(0.01)
            self.unit = SystemdUnitObservation("running", kwargs["description"],
                                              int((launch_root / "ready").read_text()), None)
            return SystemdStartResult("accepted")

    transport = IsolatedManager()
    host = SystemdUserExecutorHost(transport=transport, runtime_directory=tmp_path / "runtime",
                                  environment=environment, executor_python=snapshot / "bin" / "python")
    try:
        assert host.ensure(spec, control).status == "running"
        assert manager is not None
        assert (launch_root / "worker-isolated").exists() is enabled
        pid = int((launch_root / "ready").read_text())
        run_before = states.load_current_run(str(spec.run_id))
        control_before = control.load(spec.task)
        # The launching/observing client has returned. No run/resume/status (or
        # host.inspect) is called after this actual SIGKILL.
        started = time.monotonic()
        os.kill(pid, signal.SIGKILL)
        if enabled:
            while not (launch_root / "exit-code").exists():
                assert manager.poll() is None
                assert time.monotonic() - started < 5
                time.sleep(0.01)
            # The dead Executor's fd is still held by the manager, even before
            # Python/imports/ExecStopPost start. Updating/removing the snapshot
            # must fail at the same public management lease boundary.
            with pytest.raises(RunnerLeaseBusy):
                with runner_management_lease(default_runner_lock_path(environment)):
                    pytest.fail("Runner management crossed the exit-hook gap")
            (launch_root / "allow-hook").touch()
        stdout, stderr = manager.communicate(timeout=10)
        assert manager.returncode == 0, stdout + stderr
        assert time.monotonic() - started < 10
        notify_address = (launch_root / "notify-address").read_text()
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as reclaimed:
            reclaimed.bind("\0" + notify_address.removeprefix("@"))
        assert (launch_root / "exit-code").read_text() == str(-signal.SIGKILL)
        assert not Path(f"/proc/{pid}").exists(), "Manager must reap its Executor"
        document = read_notifications(states.root, str(spec.run_id))
        if enabled:
            assert (launch_root / "hook-code").read_text() == "0", stderr
            record, = [item for item in document["records"] if item["kind"] == "executor_exit"]
            assert record["outcome"] == "success" and record["attempts"] == 1
            assert record["exit_evidence"]["exit_status"] == "KILL"
            assert record["generation"] == spec.generation
            calls = [json.loads(line) for line in requests.read_text().splitlines()]
            assert any("执行会话意外退出" in args[args.index("--content") + 1] for args in calls)
            # Only after automatic delivery has been proved, exercise the
            # public read-only entrypoints and their existing audit projection.
            requests_before = requests.read_bytes()
            capsys.readouterr()
            for query in ("status", "history"):
                code = cli.main([query, str(spec.run_id), "--repo", spec.task.repository,
                                 "--state-dir", str(states.root), "--json"])
                output = capsys.readouterr()
                assert code == 0, output.out + output.err
                assert json.loads(output.out)["notifications"]["executor_exit"] == document["executor_exit"]
            assert read_notifications(states.root, str(spec.run_id)) == document
            assert requests.read_bytes() == requests_before
        else:
            assert not document and not requests.exists()
            assert not (launch_root / "hook-code").exists()
        assert states.load_current_run(str(spec.run_id)) == run_before
        assert control.load(spec.task) == control_before
        assert not (launch_root / "credentials").exists()
        assert not list((tmp_path / "runtime").glob("environment-*.json"))
        with runner_management_lease(default_runner_lock_path(environment)):
            pass
    finally:
        if manager is not None:
            (launch_root / "allow-hook").touch()
            if manager.poll() is None:
                ready = launch_root / "ready"
                if ready.exists():
                    try:
                        os.kill(int(ready.read_text()), signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                try:
                    manager.communicate(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(manager.pid, signal.SIGKILL)
            manager.communicate(timeout=3)
        transport.fire_cleanups()
