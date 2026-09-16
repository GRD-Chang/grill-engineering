"""Process-owned shared usage and exclusive Runner management leases."""

from __future__ import annotations

import fcntl
import os
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from io import TextIOWrapper
from pathlib import Path
from typing import Iterator

try:
    from agent_run.paths import app_data_root
except ModuleNotFoundError:  # pragma: no cover - source-tree installer
    from paths import app_data_root  # type: ignore[import-not-found, no-redef]


class RunnerLeaseError(RuntimeError):
    """A Runner lease could not be established safely."""


class RunnerLeaseBusy(RunnerLeaseError):
    """A conflicting usage or management lease already exists."""


@dataclass(frozen=True)
class RunnerLease:
    file: TextIOWrapper

    def fileno(self) -> int:
        return self.file.fileno()


def default_runner_lock_path(environment: dict[str, str] | None = None) -> Path:
    return app_data_root(environment) / "install.lock"


@contextmanager
def runner_usage_lease(lock_path: Path) -> Iterator[RunnerLease]:
    """Hold one non-blocking shared lease for an Executor lifetime."""

    with _runner_lease(
        lock_path, fcntl.LOCK_SH, "Runner 正在执行管理操作"
    ) as lease:
        yield lease


@contextmanager
def runner_management_lease(lock_path: Path) -> Iterator[RunnerLease]:
    """Hold the exclusive non-blocking lease used by Runner mutations."""

    with _runner_lease(
        lock_path,
        fcntl.LOCK_EX,
        "存在活动 Executor 或另一个 Runner 管理操作",
    ) as lease:
        yield lease


@contextmanager
def _runner_lease(
    lock_path: Path, operation: int, busy_message: str
) -> Iterator[RunnerLease]:
    lock_path = Path(lock_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as error:
        raise RunnerLeaseError("无法创建 Runner 租约文件") from error
    with os.fdopen(descriptor, "a+") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), operation | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RunnerLeaseBusy(busy_message) from error
        except OSError as error:
            raise RunnerLeaseError("无法取得 Runner 租约") from error
        yield RunnerLease(lock_file)


def _guard_inherited_lease(carrier: Path, timeout_seconds: float) -> int:
    deadline = time.monotonic() + timeout_seconds
    while carrier.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    return 0


def main(arguments: list[str] | None = None) -> int:
    selected = sys.argv[1:] if arguments is None else arguments
    if len(selected) != 3 or selected[0] != "guard":
        return 2
    try:
        timeout_seconds = float(selected[2])
    except ValueError:
        return 2
    if timeout_seconds <= 0:
        return 2
    return _guard_inherited_lease(Path(selected[1]), timeout_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
