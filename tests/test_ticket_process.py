"""隔离测试硬退出及外层清理故障不能遗留资源或覆盖原错。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import time

import pytest

from agent_run.executor_host import _process_start_token
from agent_run.process_cleanup import child_subreaper
from support import ticket_process
from support.ticket_process import isolated_ticket_test
from support.worker_process_timing import wait_until


@isolated_ticket_test
def _abrupt_exit_probe(identity: Path, termination: str, descendant: str) -> None:
    if descendant == "subprocess":
        child = subprocess.Popen(
            [sys.executable, "-c", "import signal; signal.pause()"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True,
        )
        pid = child.pid
    else:
        ready, announce = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(ready)
            os.setsid()
            os.write(announce, b'1')
            os.close(announce)
            # Deliberately retain the inherited result fd, too.
            signal.pause()
            os._exit(0)
        os.close(announce)
        try:
            assert select.select([ready], [], [], 5)[0], "forked descendant did not start"
            assert os.read(ready, 1) == b'1'
        finally:
            os.close(ready)
    identity.write_text(json.dumps({
        str(pid): _process_start_token(pid),
        str(os.getpid()): _process_start_token(os.getpid()),
    }))
    if termination == "sigkill":
        os.kill(os.getpid(), signal.SIGKILL)
    if termination == "hang":
        signal.pause()
    os._exit(7)


def _reap_probe_processes(identity: Path) -> None:
    """回归自身即使失败，也只按探针已登记的 PID/token 回收。"""
    if not identity.exists():
        return
    owned = {int(pid): token for pid, token in json.loads(identity.read_text()).items()}
    for pid, token in owned.items():
        if token is not None and _process_start_token(pid) == token:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    deadline = time.monotonic() + 5
    while owned and time.monotonic() < deadline:
        for pid, token in list(owned.items()):
            try:
                os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                pass
            if _process_start_token(pid) != token:
                del owned[pid]
        if owned:
            time.sleep(0.01)
    assert not owned, f"Probe processes survived finalizer: {owned}"


@pytest.mark.parametrize("termination", ["exit", "sigkill"])
@pytest.mark.parametrize("descendant", ["subprocess", "fork"])
def test_abrupt_test_exit_reaps_detached_descendants_without_touching_other_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, termination: str, descendant: str,
) -> None:
    producer = '''import subprocess, sys
if sys.stdin.readline() == 'spawn\\n':
    child = subprocess.Popen([sys.executable, '-c', 'import signal; signal.pause()'])
    try:
        print(child.pid, flush=True)
        sys.stdin.readline()
    finally:
        child.terminate()
        child.wait(timeout=5)
'''
    original_popen = subprocess.Popen
    unrelated = original_popen([sys.executable, "-c", producer], text=True,
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    unrelated_children: list[int] = []
    identity = tmp_path / "abrupt-owned.json"

    def start_isolated(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        assert unrelated.stdin is not None and unrelated.stdout is not None
        unrelated.stdin.write("spawn\n")
        unrelated.stdin.flush()
        assert select.select([unrelated.stdout], [], [], 5)[0], "unrelated child did not start"
        unrelated_children.append(int(unrelated.stdout.readline()))
        return process

    with child_subreaper():
        try:
            monkeypatch.setattr(ticket_process.subprocess, "Popen", start_isolated)
            with pytest.raises(pytest.fail.Exception, match="Ticket test returned no result"):
                _abrupt_exit_probe(identity, termination, descendant)
            assert identity.exists(), "probe never reached its exit boundary"
            assert all(not Path(f"/proc/{pid}").exists() for pid in json.loads(identity.read_text()))
            assert unrelated.poll() is None
            assert unrelated_children and all(Path(f"/proc/{pid}").exists() for pid in unrelated_children)
        finally:
            try:
                _reap_probe_processes(identity)
            finally:
                unrelated.communicate("exit\n", timeout=5)
                assert unrelated.returncode == 0


@isolated_ticket_test
def _outcome_probe(identity: Path, failure: bool, large: bool = False) -> None:
    identity.write_text(str(os.getpid()))
    print("isolated stdout", flush=True)
    print("isolated stderr", file=sys.stderr, flush=True)
    if failure:
        details = "\n" + "x" * 300_000 + "\nend of isolated failure" if large else ""
        error = AssertionError("original isolated failure" + details)
        error.add_note("original child note")
        raise error


@pytest.mark.parametrize("failure", [True, False], ids=["test-and-cleanup-fail", "cleanup-only"])
def test_outer_cleanup_failure_preserves_test_traceback_and_cleanup_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: bool,
) -> None:
    cleanup = ticket_process.cleanup_owned_process
    cleaned: list[int] = []

    def cleanup_then_fail(process, *args, **kwargs):
        cleanup(process, *args, **kwargs)
        cleaned.append(process.pid)
        raise OSError("controlled cleanup failure")

    identity = tmp_path / "outcome.pid"
    monkeypatch.setattr(ticket_process, "cleanup_owned_process", cleanup_then_fail)
    with pytest.raises(pytest.fail.Exception if failure else OSError) as caught:
        _outcome_probe(identity, failure)
    if failure:
        assert "AssertionError: original isolated failure" in str(caught.value)
        assert "original child note" in str(caught.value)
        assert "controlled cleanup failure" in "\n".join(caught.value.__notes__)
    else:
        assert str(caught.value) == "controlled cleanup failure"
    assert cleaned and all(not Path(f"/proc/{pid}").exists() for pid in cleaned)
    assert not Path(f"/proc/{identity.read_text()}").exists()


def test_large_failure_and_output_survive_supervisor_protocol(tmp_path: Path, capsys) -> None:
    identity = tmp_path / "large-result.pid"
    with pytest.raises(pytest.fail.Exception) as caught:
        _outcome_probe(identity, True, large=True)
    assert "AssertionError: original isolated failure" in str(caught.value)
    assert "x" * 300_000 + "\nend of isolated failure" in str(caught.value)
    assert "original child note" in str(caught.value)
    output = capsys.readouterr()
    assert output.out == "isolated stdout\n" and output.err == "isolated stderr\n"
    assert not Path(f"/proc/{identity.read_text()}").exists()


def test_receive_timeout_reaps_running_test_and_its_detached_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = tmp_path / "blocked-owned.json"
    original = TimeoutError("controlled receive timeout")

    def timeout_after_worker_ready(*_args, **_kwargs):
        assert wait_until(lambda: identity.exists() and identity.stat().st_size > 0)
        raise original

    with child_subreaper():
        try:
            monkeypatch.setattr(ticket_process.socket.socket, "recv", timeout_after_worker_ready)
            with pytest.raises(TimeoutError) as caught:
                _abrupt_exit_probe(identity, "hang", "subprocess")
            assert caught.value is original
            assert all(not Path(f"/proc/{pid}").exists() for pid in json.loads(identity.read_text()))
        finally:
            _reap_probe_processes(identity)


@pytest.mark.parametrize("after_response", [False, True], ids=["before-response", "after-response"])
def test_receive_error_survives_outer_cleanup_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, after_response: bool,
) -> None:
    receive, cleanup = ticket_process.socket.socket.recv, ticket_process.cleanup_owned_process
    original = OSError("controlled receive failure")
    cleaned: list[int] = []

    def receive_then_fail(channel, *args, **kwargs):
        data = receive(channel, *args, **kwargs)
        if after_response and data:
            return data
        raise original

    def cleanup_then_fail(process, *args, **kwargs):
        cleanup(process, *args, **kwargs)
        cleaned.append(process.pid)
        raise RuntimeError("secondary cleanup failure")

    identity = tmp_path / "receive.pid"
    monkeypatch.setattr(ticket_process.socket.socket, "recv", receive_then_fail)
    monkeypatch.setattr(ticket_process, "cleanup_owned_process", cleanup_then_fail)
    with pytest.raises(pytest.fail.Exception if after_response else OSError) as caught:
        _outcome_probe(identity, after_response)
    if after_response:
        assert "AssertionError: original isolated failure" in str(caught.value)
        assert "original child note" in str(caught.value)
        assert "controlled receive failure" in "\n".join(caught.value.__notes__)
    else:
        assert caught.value is original
    assert "secondary cleanup failure" in "\n".join(caught.value.__notes__)
    assert cleaned and all(not Path(f"/proc/{pid}").exists() for pid in cleaned)
    assert not Path(f"/proc/{identity.read_text()}").exists()


def test_supervisor_exit_error_keeps_received_test_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Inject after the real Worker exits and its response has been received.
    script = ticket_process._SUPERVISOR.replace(
        "outcome['returncode'] = process.returncode",
        "raise OSError('controlled worker exit failure')", 1,
    )
    assert script != ticket_process._SUPERVISOR
    monkeypatch.setattr(ticket_process, "_SUPERVISOR", script)
    identity = tmp_path / "exit.pid"
    with pytest.raises(pytest.fail.Exception) as caught:
        _outcome_probe(identity, True)
    assert "AssertionError: original isolated failure" in str(caught.value)
    assert "original child note" in str(caught.value)
    assert "OSError: controlled worker exit failure" in str(caught.value)
    assert not Path(f"/proc/{identity.read_text()}").exists()
