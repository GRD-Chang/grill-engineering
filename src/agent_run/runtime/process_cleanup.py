"""Bounded cleanup for subprocess trees owned by this process."""

from __future__ import annotations

import ctypes
import os
import signal
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


@contextmanager
def child_subreaper() -> Iterator[None]:
    """Keep orphaned descendants attached while this supervisor is active."""

    if os.name != "posix":
        yield
        return
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        prctl = libc.prctl
        prctl.restype = ctypes.c_int
        previous = ctypes.c_int()
        if prctl(37, ctypes.byref(previous), 0, 0, 0) != 0:
            yield
            return
        if prctl(36, 1, 0, 0, 0) != 0:  # Linux PR_SET_CHILD_SUBREAPER.
            yield
            return
    except (AttributeError, OSError):
        yield
        return
    try:
        yield
    finally:
        prctl(36, previous.value, 0, 0, 0)


def terminate_process_group(
    process: subprocess.Popen[Any],
    *,
    adopted_baseline: set[int] | None = None,
    same_process_group: bool = False,
    timeout: float = 5,
) -> None:
    """Kill a process group and descendants that deliberately changed session."""

    # Once reaped, this child cannot retain descendants; any survivors were
    # reparented. Its process group must still be killed below.
    descendants = (
        set(descendant_pids(process.pid)) if process.poll() is None else set()
    )
    if adopted_baseline is not None:
        descendants.update(
            pid
            for pid in descendant_pids(os.getpid())
            if pid not in adopted_baseline
        )
    if same_process_group:
        try:
            process.kill()
        except OSError:
            pass
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            try:
                process.kill()
            except OSError:
                pass
    for pid in descendants:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    try:
        process.wait(timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        pass
    if adopted_baseline is None:
        _reap_children(descendants)
        return
    for _ in range(100):
        adopted = set(descendant_pids(os.getpid()))
        adopted.difference_update(adopted_baseline)
        if not adopted:
            _reap_children(descendants)
            return
        descendants.update(adopted)
        for pid in adopted:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        _reap_children(descendants)
        time.sleep(0.01)


def capture_process_scope() -> set[int]:
    """Return descendants that existed before a managed child is started."""

    return set(descendant_pids(os.getpid()))


def descendant_pids(root_pid: int) -> list[int]:
    children: dict[int, list[int]] = {}
    try:
        entries = Path("/proc").iterdir()
    except OSError:
        return []
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            fields = (entry / "stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()
            pid = int(entry.name)
            parent_pid = int(fields[1])
        except (OSError, UnicodeError, ValueError, IndexError):
            continue
        children.setdefault(parent_pid, []).append(pid)
    descendants: list[int] = []
    pending = [root_pid]
    while pending:
        parent_pid = pending.pop()
        for child_pid in children.get(parent_pid, []):
            if child_pid in descendants:
                continue
            descendants.append(child_pid)
            pending.append(child_pid)
    return descendants


def _reap_children(pids: set[int]) -> None:
    for pid in pids:
        try:
            os.waitpid(pid, os.WNOHANG)
        except (ChildProcessError, OSError):
            continue
