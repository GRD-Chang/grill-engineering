"""Credential copies remain private across real Worker mount namespaces."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from agent_run.executor_environment import capture_executor_environment, write_environment_carrier
from agent_run.executor_exit import CREDENTIAL
from agent_run.executor_host import ExecutorSpec
from agent_run.task_control import TaskKey


_WORKER_PROBE = """
import json, os, sys
from pathlib import Path
print('ready', flush=True)
assert sys.stdin.readline().strip() == 'inspect'
readable = []
for name in sys.argv[1:]:
    try:
        Path(name).read_bytes()
    except OSError:
        readable.append(False)
    else:
        readable.append(True)
Path('worker-output').write_text('allowed')
print(json.dumps({'readable': readable, 'token': os.environ.get('GH_TOKEN'),
                  'internal_environment': any(key.startswith('AGENT_RUN_INTERNAL_') for key in os.environ),
                  'private_key': 'AGENT_RUN_GITHUB_APP_PRIVATE_KEY' in os.environ}))
"""

# The outer namespace supplies a private /run, including the conventional
# /run/user/<uid> path. No real user-manager files are created or inspected.
_LAUNCH_WORKER = r"""
import json, os, select, shutil, subprocess, sys
from pathlib import Path
from agent_run.codex import _worker_hidden_paths
from agent_run.worker_sandbox import bubblewrap_command, native_worker_environment, worker_environment
data = json.load(sys.stdin)
checkout, temporary = Path(data['checkout']), Path(data['temporary'])
for name in data['late_roots']:
    assert not Path(name).exists(), name
mode = data['mode']
environment = (worker_environment(temporary / 'gh', 'fixture-worker-token') if mode == 'app'
               else native_worker_environment(dict(os.environ, GH_TOKEN='fixture-worker-token'),
                                              temporary / 'gh', host=mode == 'host'))
command = bubblewrap_command(
    [sys.executable, '-c', data['probe'], *data['paths']],
    checkout=checkout, temporary=temporary, writable_checkout=True, environment=environment,
    hidden_paths=_worker_hidden_paths(None, checkout=checkout, hide_host_credentials=mode == 'app'),
    inherit_host_auth=mode == 'host')
with subprocess.Popen(command, env=environment, text=True, stdin=subprocess.PIPE,
                      stdout=subprocess.PIPE, stderr=subprocess.PIPE) as worker:
    try:
        assert select.select([worker.stdout], [], [], 5)[0], 'Worker did not become ready'
        assert worker.stdout.readline() == 'ready\n'
        for name in data['late_files']:
            target = Path(name)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(data['carrier'], target)
            target.chmod(0o400)
        for name in data['paths']:
            assert 'fixture-runner-secret' in Path(name).read_text(), name
        stdout, stderr = worker.communicate('inspect\n', timeout=10)
    finally:
        if worker.poll() is None:
            worker.kill()
        worker.wait(timeout=5)
sys.stdout.write(stdout)
sys.stderr.write(stderr)
raise SystemExit(worker.returncode)
"""


@pytest.mark.parametrize("mode", ["app", "pat", "host"])
@pytest.mark.parametrize("late", [False, True], ids=["existing", "created-after-ready"])
def test_worker_cannot_read_executor_credential_copies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, late: bool,
) -> None:
    bwrap = shutil.which("bwrap")
    if bwrap is None:
        pytest.skip("bubblewrap is unavailable")
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    temporary = tmp_path / "worker"
    temporary.mkdir()
    runtime = Path(os.environ["XDG_RUNTIME_DIR"])
    fake_run = tmp_path / "fake-run"
    # Source capture contains both Runner token and App signing material.
    spec = ExecutorSpec(TaskKey(checkout, "example/project", 1), "action", "run", 1)
    carrier = write_environment_carrier(
        runtime / "agent-run" / "executor", spec,
        capture_executor_environment(
            {"GH_TOKEN": "fixture-runner-secret", "AGENT_RUN_GITHUB_APP_PRIVATE_KEY": "fixture-private-key"},
            command=("run", "1"),
        ),
    )
    native_runtime = Path("/run/user") / str(os.geteuid())
    credentials = [
        runtime / "credentials" / "executor.service" / CREDENTIAL,
        runtime / "credentials" / "other-generation.service" / CREDENTIAL,
        runtime / "systemd" / "temporary-credentials" / "other.service" / CREDENTIAL,
        native_runtime / "credentials" / "executor.service" / CREDENTIAL,
        native_runtime / "systemd" / "temporary-credentials" / "other.service" / CREDENTIAL,
        Path("/run/credentials/system.service") / CREDENTIAL,
        Path("/run/systemd/temporary-credentials/system.service") / CREDENTIAL,
        tmp_path / "inherited-credentials" / CREDENTIAL,
    ]
    # System services are outside the user Executor's hosting scope. Its user
    # credential roots start absent in the late-creation case.
    late_files = [path for path in credentials if path.parts[:3] not in
                  {('/', 'run', 'credentials'), ('/', 'run', 'systemd')}] if late else []
    for path in credentials:
        if path in late_files:
            continue
        destination = (fake_run / path.relative_to("/run")
                       if path.is_relative_to("/run") and not path.is_relative_to(tmp_path) else path)
        destination.parent.mkdir(parents=True, mode=0o700)
        shutil.copyfile(carrier, destination)
        destination.chmod(0o400)
    # A symlink is another spelling of the same readable service credential.
    alias = checkout / "credential-alias"
    alias.symlink_to(credentials[0])
    inherited = tmp_path / "inherited-credentials-alias"
    inherited.symlink_to(credentials[-1].parent, target_is_directory=True)
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(inherited))
    monkeypatch.setenv("AGENT_RUN_INTERNAL_SYSTEMD_RUNTIME_DIRECTORY", str(native_runtime))
    monkeypatch.setenv("GH_TOKEN", "fixture-runner-secret")
    monkeypatch.setenv("AGENT_RUN_GITHUB_APP_PRIVATE_KEY", "fixture-private-key")
    paths = [str(path) for path in [carrier, *credentials, alias]]
    source = Path(__file__).resolve().parents[1] / "src"
    namespace = [bwrap, "--die-with-parent", "--bind", "/", "/", "--dev", "/dev",
                 "--bind", str(fake_run), "/run"]
    # Reopen only this test's files/runtime, even when pytest or the interpreter
    # lives on /run/user/<uid> tmpfs. The host /run remains isolated.
    for path in (tmp_path, source, Path(sys.prefix)):
        if path.is_relative_to("/run"):
            namespace.extend(["--bind", str(path), str(path)])
    namespace.extend(["--tmpfs", str(fake_run), "--", sys.executable, "-c", _LAUNCH_WORKER])
    late_roots = [runtime / "credentials", runtime / "systemd" / "temporary-credentials",
                  native_runtime / "credentials", native_runtime / "systemd" / "temporary-credentials",
                  inherited] if late else []
    result = subprocess.run(
        namespace,
        input=json.dumps({"checkout": str(checkout), "temporary": str(temporary),
                          "mode": mode, "paths": paths, "probe": _WORKER_PROBE,
                          "carrier": str(carrier), "late_files": [str(path) for path in late_files],
                          "late_roots": [str(path) for path in late_roots]}),
        env=dict(os.environ, PYTHONPATH=str(source)),
        text=True, capture_output=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "readable": [False] * len(paths), "token": "fixture-worker-token",
        "private_key": False, "internal_environment": False,
    }
    assert (checkout / "worker-output").read_text() == "allowed"
    assert "fixture-runner-secret" in carrier.read_text()


def test_unavailable_credential_directory_blocks_worker_preparation() -> None:
    from agent_run.codex import _worker_hidden_paths
    from agent_run.worker_sandbox import WorkerSandboxError

    # A non-directory at the reserved mountpoint must not become a silent skip.
    reserved = Path(os.environ["XDG_RUNTIME_DIR"]) / "credentials"
    reserved.write_text("occupied")
    with pytest.raises(WorkerSandboxError, match="凭据隔离目录"):
        _worker_hidden_paths(None)
    assert reserved.read_text() == "occupied"
