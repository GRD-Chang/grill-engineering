from __future__ import annotations

import shutil
import subprocess
import sys
import venv
from pathlib import Path

from agent_run.executor_host import ExecutorSpec
from agent_run.systemd_executor_host import SystemdUserExecutorHost
from agent_run.task_control import TaskKey


def test_executor_command_preserves_snapshot_python(tmp_path: Path) -> None:
    snapshot = tmp_path / "snapshot"
    venv.EnvBuilder(with_pip=False, symlinks=True).create(snapshot)
    version = f"python{sys.version_info.major}.{sys.version_info.minor}"
    site_packages = snapshot / "lib" / version / "site-packages"
    shutil.copytree(
        Path(__file__).resolve().parents[1] / "src" / "agent_run",
        site_packages / "agent_run",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    active = tmp_path / "active"
    active.symlink_to(snapshot, target_is_directory=True)
    host = SystemdUserExecutorHost(
        runtime_directory=tmp_path / "runtime",
        environment={},
        executor_python=active / "bin" / "python",
    )
    spec = ExecutorSpec(
        task=TaskKey(tmp_path, "owner/repo", 213),
        action_id="python-probe",
        generation=1,
        run_id=None,
        command=("run", "213"),
        cwd=tmp_path,
        state_root=tmp_path / "state",
    )
    command = host._executor_command(spec, tmp_path / "carrier", tmp_path / "lease")
    result = subprocess.run(
        [*command, "--help"],
        cwd=tmp_path,
        env={},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert "--action-id" in result.stdout
    assert host.executor_python == snapshot / "bin" / "python"

    other_snapshot = tmp_path / "other-snapshot"
    venv.EnvBuilder(with_pip=False, symlinks=True).create(other_snapshot)
    other_host = SystemdUserExecutorHost(
        runtime_directory=tmp_path / "other-runtime",
        environment={},
        executor_python=other_snapshot / "bin" / "python",
    )
    assert host.executor_python.resolve() == other_host.executor_python.resolve()
    assert host._runner_binding() != other_host._runner_binding()
