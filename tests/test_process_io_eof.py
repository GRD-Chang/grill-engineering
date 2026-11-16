"""Capture deadlines and cancellation still apply after both output pipes close."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
from types import SimpleNamespace

import pytest

from agent_run.runtime import command, process_io
from agent_run.runtime.process_io import CompleteOutput


@pytest.mark.parametrize("boundary", ["deadline", "cancel"])
def test_capture_after_pipe_eof_retains_deadline_and_cancellation(
    monkeypatch: pytest.MonkeyPatch, boundary: str,
) -> None:
    child = subprocess.Popen(
        [sys.executable, "-c",
         "import os, signal\nsignal.alarm(5)\n"
         "os.write(1, b'output')\nos.write(2, b'diagnostic')\n"
         "os.close(1)\nos.close(2)\nsignal.pause()\n"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
    )
    streams = (child.stdout, child.stderr)
    descriptors = [stream.fileno() for stream in streams]
    stdout, stderr = CompleteOutput(64), CompleteOutput(64)
    real_wait, real_signal = child.wait, os.killpg
    now, cancelled, bounded_waits, signals = [100.25], [False], [], []
    timeout = 0.125
    deadline = now[0] + timeout

    def wait(timeout=None):
        if timeout is not None and child.returncode is None:
            bounded_waits.append(timeout)
            assert timeout == 0.05
            assert stdout.eof and stderr.eof and child.poll() is None
            if boundary == "deadline":
                now[0] = deadline
            else:
                now[0] += timeout
                cancelled[0] = True
            raise subprocess.TimeoutExpired(child.args, timeout)
        # Capture can revisit the EOF branch after its check already reaped
        # the child. Only the still-live wait advances the controlled clock.
        return real_wait(timeout=1)

    def signal_group(pid, sig):
        assert pid == child.pid and sig == signal.SIGKILL
        assert os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is None
        signals.append(pid)
        real_signal(pid, sig)

    class PipeSelector(process_io.selectors.DefaultSelector):
        def select(self, timeout=None):
            # The replaced empty-selector sleep must fail promptly too.
            assert self.get_map(), "Use a bounded child wait after pipe EOF"
            return super().select(timeout)

    monkeypatch.setattr(child, "wait", wait)
    monkeypatch.setattr(command, "time", SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr(os, "killpg", signal_group)
    monkeypatch.setattr(process_io.selectors, "DefaultSelector", PipeSelector)
    try:
        result = command.capture_command(
            child, timeout=timeout, max_output=64, stdout=stdout, stderr=stderr,
            cancelled=lambda: cancelled[0],
        )
        assert result.stdout == b"output" and result.stderr == b"diagnostic"
        assert stdout.eof and stderr.eof
        assert result.timed_out is (boundary == "deadline")
        assert bounded_waits == [0.05] and signals == [child.pid]
        assert result.returncode == child.returncode == -signal.SIGKILL
        assert all(stream.closed for stream in streams)
        for descriptor in descriptors:
            with pytest.raises(OSError):
                os.fstat(descriptor)
        with pytest.raises(ChildProcessError):
            os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
    finally:
        if child.poll() is None:
            real_signal(child.pid, signal.SIGKILL)
        real_wait(timeout=1)
        for stream in streams:
            stream.close()
