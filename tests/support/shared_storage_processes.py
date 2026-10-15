"""共享存储场景的启动登记、就绪边界与失败清理。"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import signal
import subprocess
from typing import Any, Iterator

from agent_run.executor_host import _process_start_token
from agent_run.process_cleanup import child_subreaper, descendant_pids
from support.worker_process_timing import wait_until


@dataclass(frozen=True)
class StartedStorageWorker:
    number: int
    pid: int
    process_start_token: str


def register_storage_worker(repo: Path, number: int, pid: int) -> None:
    """在生产 bootstrap 放行前登记，ready 和控制存储均不承担清理权威。"""
    token = _process_start_token(pid)
    assert token is not None
    directory = repo / "storage-workers"
    directory.mkdir(exist_ok=True)
    target = directory / f"{pid}.json"
    pending = target.with_suffix(".pending")
    pending.write_text(json.dumps(asdict(StartedStorageWorker(number, pid, token))))
    pending.replace(target)


def started_storage_workers(repo: Path) -> list[StartedStorageWorker]:
    return [StartedStorageWorker(**json.loads(path.read_text()))
            for path in (repo / "storage-workers").glob("*.json")]


def wait_for_storage_workers(
    repo: Path, executor: subprocess.Popen[Any], stderr_path: Path, *, timeout: float = 10,
) -> dict[int, int]:
    def alive() -> None:
        assert executor.poll() is None, stderr_path.read_text()

    def prepared() -> bool:
        alive()
        return (repo / "storage-prepared").exists()

    # 准备与 ready 分别计时；历史交付更早已由调用方同步完成。
    wait_until(prepared)

    def ready() -> dict[int, int] | None:
        alive()
        workers = {worker.number: worker for worker in started_storage_workers(repo)}
        if not {3, 4} <= workers.keys():
            return None
        for number in (3, 4):
            worker = workers[number]
            marker = repo / f"{number}.ready"
            if not marker.exists():
                return None
            assert int(marker.read_text()) == worker.pid
            assert _process_start_token(worker.pid) == worker.process_start_token
        return {number: workers[number].pid for number in (3, 4)}

    return wait_until(ready, timeout=timeout)


def _cleanup_executor(
    repo: Path, executor: subprocess.Popen[Any], executor_token: str,
) -> None:
    identities = {worker.pid: worker.process_start_token
                  for worker in started_storage_workers(repo)}
    # 同时捕获尚未完成登记的 bootstrap / Git 子进程。
    if executor.poll() is None:
        for pid in descendant_pids(executor.pid):
            if (token := _process_start_token(pid)) is not None:
                identities[pid] = token
        if _process_start_token(executor.pid) == executor_token:
            try:
                os.killpg(executor.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    executor.wait(timeout=5)
    # Executor 停止后登记不再增长，乱序或未 ready 的 Worker 都在其中。
    identities.update({worker.pid: worker.process_start_token
                       for worker in started_storage_workers(repo)})
    script = os.fsencode(repo / "held-worker.py")
    for pid in descendant_pids(os.getpid()):
        token = _process_start_token(pid)
        try:
            arguments = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        # 登记前被杀的 Executor 会关闭 bootstrap gate；只认本例准确脚本。
        if token is not None and script in arguments and _process_start_token(pid) == token:
            identities[pid] = token
    for pid, token in identities.items():
        if _process_start_token(pid) != token:
            continue
        try:
            if os.getpgid(pid) == pid:
                os.killpg(pid, signal.SIGKILL)
            else:
                os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def reaped() -> bool:
        pending = False
        for pid, token in identities.items():
            if _process_start_token(pid) != token:
                continue
            try:
                os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                pass
            pending |= _process_start_token(pid) == token
        return not pending

    wait_until(reaped, timeout=5)


@contextmanager
def storage_executor(
    repo: Path, command: list[str], environment: dict[str, str], stderr_path: Path,
) -> Iterator[subprocess.Popen[Any]]:
    with child_subreaper():
        with stderr_path.open("w", encoding="utf-8") as stderr:
            executor = subprocess.Popen(
                command, env=environment, stdout=subprocess.DEVNULL,
                stderr=stderr, start_new_session=True,
            )
        token = _process_start_token(executor.pid)
        assert token is not None
        try:
            yield executor
        finally:
            # 业务退出必须在离开上下文前断言；这里仅为失败路径兜底。
            _cleanup_executor(repo, executor, token)
