"""只回收已证明属于本次调用的 PID/start token，不扫描其他任务。"""
from __future__ import annotations

import os
import signal
import subprocess
import time
from collections.abc import Mapping
from typing import Any

from agent_run.runtime.process_identity import _process_start_token
from agent_run.runtime.process_cleanup import descendant_pids, terminate_process_group


def cleanup_owned_process(
    process: subprocess.Popen[Any], declared: Mapping[int, str] | None = None,
) -> None:
    owned = dict(declared or {})
    if process.poll() is None:
        token = _process_start_token(process.pid)
        if token is not None:
            owned[process.pid] = token
    # Freeze known owners before collecting their descendants. This does not
    # stop or enumerate any baseline/unrelated process tree.
    for _ in range(3):
        for pid, token in list(owned.items()):
            if _process_start_token(pid) != token:
                continue
            try:
                os.kill(pid, signal.SIGSTOP)
            except ProcessLookupError:
                continue
            for child in descendant_pids(pid):
                if (child_token := _process_start_token(child)) is not None:
                    owned[child] = child_token
    terminate_process_group(process)
    for pid, token in owned.items():
        if _process_start_token(pid) == token:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    deadline = time.monotonic() + 5
    pending = set(owned)
    while pending and time.monotonic() < deadline:
        for pid in list(pending):
            try:
                os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                pass
            if _process_start_token(pid) != owned[pid]:
                pending.remove(pid)
        if pending:
            time.sleep(0.01)
    assert process.poll() is not None and not pending, f"Owned processes survived cleanup: {pending}"
