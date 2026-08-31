from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent_run.runner_lease import (
    RunnerLeaseBusy,
    runner_management_lease,
    runner_usage_lease,
)


def test_multiple_tasks_hold_shared_usage_lease_and_management_fails_fast(
    tmp_path: Path,
) -> None:
    lock = tmp_path / "runner.lock"
    with runner_usage_lease(lock), runner_usage_lease(lock):
        with pytest.raises(RunnerLeaseBusy, match="活动 Executor"):
            with runner_management_lease(lock):
                raise AssertionError("management lease must not be acquired")


def test_process_exit_releases_usage_lease_to_the_operating_system(
    tmp_path: Path,
) -> None:
    lock = tmp_path / "runner.lock"
    ready_read, ready_write = os.pipe()
    release_read, release_write = os.pipe()
    script = """
import os
from pathlib import Path
from agent_run.runner_lease import runner_usage_lease
with runner_usage_lease(Path(os.environ['LEASE_PATH'])):
    os.write(int(os.environ['READY_FD']), b'1')
    os.read(int(os.environ['RELEASE_FD']), 1)
"""
    environment = os.environ.copy()
    environment.update(
        {
            "LEASE_PATH": str(lock),
            "READY_FD": str(ready_write),
            "RELEASE_FD": str(release_read),
            "PYTHONPATH": str(Path(__file__).parents[1] / "src"),
        }
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script],
        env=environment,
        pass_fds=(ready_write, release_read),
    )
    os.close(ready_write)
    os.close(release_read)
    try:
        assert os.read(ready_read, 1) == b"1"
        with pytest.raises(RunnerLeaseBusy):
            with runner_management_lease(lock):
                raise AssertionError("management lease must not be acquired")
        os.write(release_write, b"1")
        assert process.wait(timeout=3) == 0
        with runner_management_lease(lock):
            pass
    finally:
        os.close(ready_read)
        os.close(release_write)
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)


def test_parent_to_executor_usage_handoff_has_no_management_gap(
    tmp_path: Path,
) -> None:
    lock = tmp_path / "runner.lock"
    with runner_usage_lease(lock):
        with runner_usage_lease(lock):
            with pytest.raises(RunnerLeaseBusy):
                with runner_management_lease(lock):
                    raise AssertionError("management lease must not interleave")
