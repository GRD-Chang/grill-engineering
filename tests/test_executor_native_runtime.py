"""Formal env -i startup selects native SQLite through private material."""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from agent_run.executor_environment import capture_executor_environment, write_environment_carrier
from agent_run.runner_lease import default_runner_lock_path
from agent_run.systemd_executor_host import SystemdUserExecutorHost
from support.control_state import snapshot_control
from support.executor_exit import create_exit_run
from support.native_runtime import current_runtime, observed_runtimes, snapshot_python
from support.state import snapshot_run


@pytest.mark.parametrize("damage", [None, "binding", "expired"])
def test_formal_executor_reads_the_same_sqlite_as_its_launching_client(
    tmp_path: Path, damage: str | None,
) -> None:
    states, control, spec = create_exit_run(tmp_path, enabled=False)
    report = tmp_path / "runtime.jsonl"
    python = snapshot_python(tmp_path / "snapshot", report)
    query = ("status", str(spec.run_id), "--repo", spec.task.repository,
             "--state-dir", str(states.root), "--json")
    spec = replace(spec, command=query)
    environment = {**os.environ, "BOOTSTRAP_PRIVATE_VALUE": "not-unit-metadata"}
    foreground = subprocess.run(
        [str(python), "-I", "-m", "agent_run", *query], env=environment,
        capture_output=True, text=True, timeout=10,
    )
    assert foreground.returncode == 0, foreground.stdout + foreground.stderr
    assert observed_runtimes(report) == [current_runtime()]
    report.unlink()
    carrier = write_environment_carrier(
        tmp_path / "runtime", spec, capture_executor_environment(environment, command=query),
    )
    if damage is not None:
        document = json.loads(carrier.read_text())
        if damage == "binding":
            document["binding"]["generation"] += 1
        else:
            document["expires_at"] = 0
        carrier.write_text(json.dumps(document))
    host = SystemdUserExecutorHost(runtime_directory=tmp_path / "runtime", environment={}, executor_python=python)
    command = host._executor_command(spec, carrier, default_runner_lock_path(environment))
    assert "not-unit-metadata" not in " ".join(command)
    assert not any(value.startswith("LD_LIBRARY_PATH=") for value in command)
    before = snapshot_run((states.root, str(spec.run_id))), snapshot_control(control, spec.task)
    # This exact production command clears inherited variables before Python.
    result = subprocess.run(command, env={}, capture_output=True, text=True, timeout=10)
    if damage is None:
        assert result.returncode == 0, result.stdout + result.stderr
        assert json.loads(result.stdout)["status"] == json.loads(foreground.stdout)["status"]
        assert observed_runtimes(report) == [current_runtime()]
        assert not carrier.exists()
    else:
        assert result.returncode != 0
        assert "Executor runtime environment unavailable" in result.stderr
        assert "not-unit-metadata" not in result.stdout + result.stderr
        assert not report.exists(), "Invalid material reached application SQLite"
        assert carrier.exists() is (damage == "binding")
    assert (snapshot_run((states.root, str(spec.run_id))), snapshot_control(control, spec.task)) == before
