"""Own orphan reaping only inside isolated SIGINT test controllers."""

from __future__ import annotations

import ctypes
import os
import signal
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any


def cleanup_probe_controller(controller: subprocess.Popen[Any], group_path: Path) -> None:
    """Let the controller reap first; terminate only its recorded Worker group."""

    def kill_worker_group() -> None:
        if group_path.exists() and (recorded := group_path.read_text().strip()):
            try:
                os.killpg(int(recorded), signal.SIGKILL)
            except ProcessLookupError:
                pass

    try:
        if controller.poll() is None:
            controller.send_signal(signal.SIGINT)
            try:
                controller.wait(timeout=5)
            except subprocess.TimeoutExpired:
                # Keep the subreaper alive while terminating its descendants.
                kill_worker_group()
                try:
                    controller.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    controller.kill()
                    controller.wait(timeout=5)
        kill_worker_group()
    finally:
        if controller.stdout is not None:
            controller.stdout.close()
        if controller.stderr is not None:
            controller.stderr.close()


@contextmanager
def expect_sigint_cleanup(child_path: Path, reaped_path: Path) -> Iterator[None]:
    assert ctypes.CDLL(None).prctl(36, 1, 0, 0, 0) == 0
    child: int | None = None
    reaped = False
    try:
        try:
            yield
        except KeyboardInterrupt:
            child = int(child_path.read_text().strip())
            deadline = time.monotonic() + 5
            while True:
                try:
                    if os.waitpid(child, os.WNOHANG)[0] == child:
                        break
                except ChildProcessError:
                    # The owning shell may already have reaped it.
                    try:
                        os.kill(child, 0)
                    except ProcessLookupError:
                        break
                    raise AssertionError("Worker child is alive but not owned")
                assert time.monotonic() < deadline, "background Worker survived SIGINT cleanup"
                time.sleep(0.02)
            reaped = True
            reaped_path.write_text("SIGINT cleanup passed")
            raise SystemExit(130)
        raise AssertionError("controller did not receive SIGINT")
    finally:
        # Real exit assertions precede the failure-path safety net.
        if not reaped:
            if child is None and child_path.exists():
                recorded = child_path.read_text().strip()
                if recorded:
                    child = int(recorded)
            if child is not None:
                try:
                    os.kill(child, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    os.waitpid(child, 0)
                except ChildProcessError:
                    pass
