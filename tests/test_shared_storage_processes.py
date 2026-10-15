"""以小型真实进程覆盖共享存储测试自身的失败回收边界。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading

import pytest

from agent_run.executor_host import _process_start_token
from support.shared_storage_processes import (
    started_storage_workers, storage_executor, wait_for_storage_workers,
)
from support.worker_process_timing import wait_until


def _executor_command(root: Path, mode: str) -> tuple[list[str], dict[str, str]]:
    script = root / "held-worker.py"
    script.write_text('''import os, pathlib, signal, sys, time
number, root, mode = sys.argv[1:]
signal.signal(signal.SIGTERM, signal.SIG_IGN)
if number == "4" or mode == "delayed_preparation":
    ready = pathlib.Path(root) / (number + ".ready")
    pending = ready.with_suffix(".pending")
    pending.write_text(str(os.getpid()))
    pending.replace(ready)
while True:
    time.sleep(1)
''')
    driver = root / "storage-driver.py"
    driver.write_text('''import pathlib, signal, subprocess, sys
from support.shared_storage_processes import register_storage_worker
from support.worker_process_timing import wait_until
root, mode = pathlib.Path(sys.argv[1]), sys.argv[2]
(root / "storage-preparing").touch()
if mode == "before_launch":
    raise RuntimeError("controlled preparation failure before launch")
if mode == "delayed_preparation":
    wait_until(lambda: (root / "continue-preparation").exists())
for number in (4, 3):
    child = subprocess.Popen([sys.executable, str(root / "held-worker.py"),
                              str(number), str(root), mode], start_new_session=True)
    register_storage_worker(root, number, child.pid)
    if number == 4:
        wait_until(lambda: (root / "4.ready").exists())
if mode == "after_launch":
    raise RuntimeError("controlled preparation failure after launch")
(root / "storage-prepared").touch()
signal.pause()
''')
    project = Path(__file__).parents[1]
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join((str(project / "src"), str(project / "tests")))
    return [sys.executable, str(driver), str(root), mode], environment


@pytest.mark.parametrize("mode", ["before_launch", "after_launch", "ready_timeout"])
def test_storage_preparation_and_ready_failures_reap_all_started_workers(
    tmp_path: Path, mode: str,
) -> None:
    command, environment = _executor_command(tmp_path, mode)
    stderr_path = tmp_path / "executor.stderr"
    unrelated = subprocess.Popen([sys.executable, "-c", "import signal; signal.pause()"], start_new_session=True)
    try:
        # 过期启动身份即使碰巧指向活 PID，也不能清理无关进程。
        directory = tmp_path / "storage-workers"
        directory.mkdir()
        (directory / "stale.json").write_text(json.dumps({
            "number": 99, "pid": unrelated.pid,
            "process_start_token": f"stale-{_process_start_token(unrelated.pid)}",
        }))
        with storage_executor(tmp_path, command, environment, stderr_path) as executor:
            if mode == "ready_timeout":
                wait_until(lambda: (tmp_path / "storage-prepared").exists())
                workers = [worker for worker in started_storage_workers(tmp_path)
                           if worker.number in (3, 4)]
                assert {worker.number for worker in workers} == {3, 4}
                assert (tmp_path / "4.ready").exists()
                assert not (tmp_path / "3.ready").exists()
                assert all(_process_start_token(worker.pid) == worker.process_start_token
                           for worker in workers)
            message = "Worker did not reach" if mode == "ready_timeout" else "controlled preparation failure"
            with pytest.raises(AssertionError, match=message):
                wait_for_storage_workers(tmp_path, executor, stderr_path, timeout=0.05)
        workers = [worker for worker in started_storage_workers(tmp_path)
                   if worker.number in (3, 4)]
        assert {worker.number for worker in workers} == (set() if mode == "before_launch" else {3, 4})
        assert all(not Path(f"/proc/{worker.pid}").exists() for worker in workers)
        assert not Path(f"/proc/{executor.pid}").exists()
        assert unrelated.poll() is None
        if mode == "before_launch":
            assert "controlled preparation failure before launch" in stderr_path.read_text()
        elif mode == "after_launch":
            assert "controlled preparation failure after launch" in stderr_path.read_text()
    finally:
        if unrelated.poll() is None:
            os.killpg(unrelated.pid, signal.SIGKILL)
        unrelated.wait(timeout=5)


def test_storage_preparation_does_not_consume_worker_ready_timeout(tmp_path: Path) -> None:
    command, environment = _executor_command(tmp_path, "delayed_preparation")
    stderr_path = tmp_path / "executor.stderr"
    started, done = threading.Event(), threading.Event()
    results: list[dict[int, int]] = []
    errors: list[BaseException] = []
    with storage_executor(tmp_path, command, environment, stderr_path) as executor:
        wait_until(lambda: (tmp_path / "storage-preparing").exists())

        def waiting() -> None:
            started.set()
            try:
                results.append(wait_for_storage_workers(tmp_path, executor, stderr_path, timeout=0.5))
            except BaseException as error:
                errors.append(error)
            finally:
                done.set()

        thread = threading.Thread(target=waiting)
        thread.start()
        try:
            assert started.wait(5)
            # 准备屏障超过 ready 窗口后才释放；等待者此时仍必须在准备阶段。
            assert not done.wait(0.6)
            assert not (tmp_path / "storage-prepared").exists()
            (tmp_path / "continue-preparation").touch()
            assert done.wait(5)
            assert errors == []
            assert len(results) == 1 and set(results[0]) == {3, 4}
        finally:
            (tmp_path / "continue-preparation").touch()
            # A failed assertion must not leave the readiness poller using
            # the fixture. Stopping its Executor makes alive() fail promptly.
            if thread.is_alive() and executor.poll() is None:
                executor.kill()
            thread.join()
            assert not thread.is_alive()
    assert all(not Path(f"/proc/{pid}").exists() for pid in results[0].values())
