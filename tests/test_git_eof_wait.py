"""Pipe EOF does not release Git's ownership of a still-running child."""
from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import sys
from types import SimpleNamespace

import pytest

from agent_run.errors import GitError
from agent_run.runtime import git_command


@pytest.mark.parametrize("failure", ["deadline", "poll_error", "interrupt"])
def test_git_eof_wait_failures_reap_once_and_close_owned_descriptors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    helper = tmp_path / "git"
    helper.write_text(
        f"#!{sys.executable}\nimport os, signal\n"
        "signal.alarm(5)\n"  # A broken wait must still be bounded.
        "os.write(1, b'partial output')\nos.write(2, b'last diagnostic')\n"
        "os.close(1)\nos.close(2)\nsignal.pause()\n",
    )
    helper.chmod(0o700)
    environment = dict(os.environ, PATH=str(tmp_path) + os.pathsep + os.environ["PATH"])
    real_spawn, real_open = subprocess.Popen, os.pidfd_open
    real_wait, real_signal = os.waitid, os.killpg
    children, descriptors, pidfds, signals, reaped, polls = [], [], [], [], [], []
    now = [100.25]
    timeout = 0.125
    original = OSError("pidfd poll failed") if failure == "poll_error" else KeyboardInterrupt()

    def forbidden(*args, **kwargs):
        pytest.fail("Git must wait on its pidfd and retain the sole consuming waitid")

    def spawn(*args, **kwargs):
        child = real_spawn(*args, **kwargs)
        children.append(child)
        descriptors.extend(stream.fileno() for stream in (child.stdin, child.stdout, child.stderr)
                           if stream is not None)
        child.poll = forbidden
        child.wait = forbidden
        return child

    def pidfd_open(pid, flags=0):
        descriptor = real_open(pid, flags)
        if children and pid == children[0].pid:
            descriptors.append(descriptor)
            pidfds.append(descriptor)
        return descriptor

    def waitid(kind, identity, options):
        result = real_wait(kind, identity, options)
        if not options & os.WNOWAIT and children and result.si_pid == children[0].pid:
            assert kind == os.P_PIDFD and identity == descriptors[-1]
            reaped.append(result.si_pid)
        return result

    def signal_group(pid, sig):
        assert pid == children[0].pid and sig == signal.SIGKILL
        assert not reaped
        # Prove ownership immediately before signalling; never reap here.
        real_wait(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        signals.append(pid)
        real_signal(pid, sig)

    class WaitAtEof:
        def register(self, descriptor, event):
            assert descriptor == descriptors[-1]
            assert event == git_command.select.POLLIN

        def poll(self, milliseconds):
            polls.append(milliseconds)
            assert 0 < milliseconds <= timeout * 1000
            assert real_wait(os.P_PID, children[0].pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is None
            if failure == "deadline":
                now[0] += timeout
                return []
            raise original

    monkeypatch.setattr(git_command.subprocess, "Popen", spawn)
    monkeypatch.setattr(os, "pidfd_open", pidfd_open)
    monkeypatch.setattr(os, "waitid", waitid)
    monkeypatch.setattr(os, "killpg", signal_group)
    monkeypatch.setattr(git_command, "time", SimpleNamespace(monotonic=lambda: now[0], sleep=forbidden))
    # The old sleep-based implementation must fail promptly as well.
    monkeypatch.setattr(git_command, "select", SimpleNamespace(poll=WaitAtEof, POLLIN=1), raising=False)
    expected = GitError if failure == "deadline" else type(original)
    try:
        with pytest.raises(expected) as caught:
            git_command.run_git(["git", "--version"], cwd=tmp_path, input="",
                                environment=environment, timeout=timeout)
        if failure == "deadline":
            assert "timed out" in str(caught.value)
            assert caught.value.evidence == {
                "command": ["git", "--version"], "returncode": None,
                "stdout": "partial output", "stderr": "last diagnostic",
                "timeout_seconds": timeout,
            }
        else:
            assert caught.value is original
        assert len(children) == len(polls) == 1
        child = children[0]
        assert signals == reaped == [child.pid]
        assert child.returncode == -signal.SIGKILL
        assert all(stream.closed for stream in (child.stdin, child.stdout, child.stderr))
        for descriptor in descriptors:
            with pytest.raises(OSError):
                os.fstat(descriptor)
        with pytest.raises(ChildProcessError):
            real_wait(os.P_PID, child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
    finally:
        # Reclaim only a fixture child that remains waitable if an assertion fails.
        for child in children:
            try:
                real_wait(os.P_PID, child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            except ChildProcessError:
                pass
            else:
                try:
                    real_signal(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                real_wait(os.P_PID, child.pid, os.WEXITED)
            for stream in (child.stdin, child.stdout, child.stderr):
                if stream is not None:
                    stream.close()
        for descriptor in pidfds:
            try:
                os.close(descriptor)
            except OSError:
                pass
