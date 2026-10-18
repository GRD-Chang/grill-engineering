"""在独立解释器完成并发测试，退出前不撤销父进程 fixture。"""
from __future__ import annotations

from collections.abc import Callable
from contextlib import ExitStack
from functools import wraps
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
from typing import Any

import pytest

from agent_run.process_cleanup import child_subreaper
from support.process_scope import cleanup_owned_process


_CHILD_TEST = r'''
import importlib, json, os, socket, sys, threading, traceback, warnings
from pathlib import Path
result = socket.socket(fileno=int(sys.argv[1]))
payload = json.loads(sys.stdin.read())
sys.path[:] = payload['sys_path']
import pytest
from agent_run.process_cleanup import child_subreaper, descendant_pids

def decode(value):
    if isinstance(value, dict) and value.get('__fixture__') == 'path':
        return Path(value['value'])
    if isinstance(value, dict) and value.get('__fixture__') == 'monkeypatch':
        return pytest.MonkeyPatch()
    return value

thread_errors = []
def thread_failed(arguments):
    thread_errors.append(''.join(traceback.format_exception(
        arguments.exc_type, arguments.exc_value, arguments.exc_traceback)))
threading.excepthook = thread_failed
warnings.filterwarnings("error", message="This process .*multi-threaded.*fork", category=DeprecationWarning)
error = None
try:
    with child_subreaper():
        test = getattr(importlib.import_module(payload['module']), payload['name'])
        while hasattr(test, '__wrapped__'):
            test = test.__wrapped__
        test(*[decode(value) for value in payload['args']],
             **{key: decode(value) for key, value in payload['kwargs'].items()})
        alive = [thread.name for thread in threading.enumerate()
                 if thread is not threading.current_thread() and thread.is_alive()]
        assert not alive, f'Ticket test returned with live threads: {alive}'
        assert not thread_errors, '\n'.join(thread_errors)
        for pid in descendant_pids(os.getpid()):
            try:
                state = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[0]
            except (OSError, IndexError):
                continue
            assert state == 'Z', f'Ticket test returned with live process {pid}'
except BaseException:
    error = traceback.format_exc()
    if thread_errors:
        error += "\nBackground thread errors:\n" + "\n".join(thread_errors)
result.sendall(json.dumps({'error': error}).encode())
# Forked business descendants can inherit this fd. Shutdown makes the result
# boundary independent of their exit, so the parent can reap them on failure.
result.shutdown(socket.SHUT_WR)
sys.stdout.flush()
sys.stderr.flush()
os._exit(1 if error else 0)
'''


_SUPERVISOR = r'''
import ctypes, json, select, signal, socket, subprocess, sys, traceback
result = socket.socket(fileno=int(sys.argv[1]))
payload = json.loads(sys.stdin.read())
sys.path[:] = payload['sys_path']
from agent_run.process_cleanup import child_subreaper

# Keep an owner alive independently of the test interpreter. Even a setsid
# descendant is adopted here if that interpreter exits without reporting.
with child_subreaper():
    response = bytearray()
    try:
        active = ctypes.c_int()
        assert ctypes.CDLL(None).prctl(37, ctypes.byref(active), 0, 0, 0) == 0 and active.value == 1, 'Ticket supervisor requires child subreaper support'
        observer, worker = socket.socketpair()
        with observer, worker:
            process = subprocess.Popen(
                [sys.executable, '-c', payload.pop('worker_script'), str(worker.fileno())],
                stdin=subprocess.PIPE, text=True, pass_fds=(worker.fileno(),))
            worker.close()
            process.stdin.write(json.dumps(payload))
            process.stdin.close()
            # A raw-fork descendant can retain this socket. Worker exit, not
            # EOF alone, determines when the supervisor reports a hard exit.
            while process.poll() is None:
                if select.select([observer], [], [], 0.1)[0]:
                    chunk = observer.recv(65536)
                    if not chunk:
                        process.wait()
                        break
                    response.extend(chunk)
            while select.select([observer], [], [], 0)[0]:
                chunk = observer.recv(65536)
                if not chunk:
                    break
                response.extend(chunk)
            outcome = json.loads(response) if response else {
                'error': f'Ticket test returned no result (exit {process.returncode})'}
            outcome['returncode'] = process.returncode
    except BaseException:
        failure = traceback.format_exc()
        try:
            reported = json.loads(response) if response else {}
        except ValueError:
            reported = {}
        original = reported.get('error') if isinstance(reported, dict) else None
        outcome = {'error': original + '\nSupervisor receive/exit failed:\n' + failure
                   if original else failure, 'returncode': 1}
        if response and not original:
            outcome['error'] += '\nReceived test response:\n' + response.decode(errors='replace')
    try:
        result.sendall(json.dumps(outcome).encode())
        result.shutdown(socket.SHUT_WR)
    finally:
        # The caller owns cleanup. Do not exit (and lose adopted ownership)
        # before its bounded process-tree cleanup has frozen this supervisor.
        while True:
            signal.pause()
'''


def _encode(value: Any) -> Any:
    if isinstance(value, Path):
        return {"__fixture__": "path", "value": str(value)}
    if isinstance(value, pytest.MonkeyPatch):
        return {"__fixture__": "monkeypatch"}
    json.dumps(value)  # Reject fixtures outside this bounded interface.
    return value


def isolated_ticket_test(test: Callable[..., None]) -> Callable[..., None]:
    """保留完整断言，以可终止子进程承载路径、MonkeyPatch 和 JSON 参数。"""
    @wraps(test)
    def run(*args: Any, **kwargs: Any) -> None:
        if "<locals>" in test.__qualname__:
            raise ValueError("Isolated Ticket tests must be module-level functions")
        payload = json.dumps({
            "module": test.__module__, "name": test.__name__, "sys_path": sys.path,
            "args": [_encode(value) for value in args],
            "kwargs": {key: _encode(value) for key, value in kwargs.items()},
            "worker_script": _CHILD_TEST,
        })
        resources = ExitStack()
        process = stdout = stderr = None
        errors: list[tuple[str, BaseException]] = []
        response = bytearray()
        try:
            resources.enter_context(child_subreaper())
            observer, worker = socket.socketpair()
            resources.enter_context(observer)
            resources.enter_context(worker)
            stdout = resources.enter_context(tempfile.TemporaryFile(mode="w+", encoding="utf-8"))
            stderr = resources.enter_context(tempfile.TemporaryFile(mode="w+", encoding="utf-8"))
            process = subprocess.Popen(
                [sys.executable, "-c", _SUPERVISOR, str(worker.fileno())],
                cwd=Path.cwd(), env=os.environ.copy(), stdin=subprocess.PIPE,
                stdout=stdout, stderr=stderr, text=True, start_new_session=True,
                pass_fds=(worker.fileno(),),
            )
            worker.close()
            observer.settimeout(180)
            assert process.stdin is not None
            process.stdin.write(payload)
            process.stdin.close()
            while chunk := observer.recv(65536):
                response.extend(chunk)
            outcome = json.loads(response) if response else {}
            error = outcome.get("error") if outcome else "Ticket supervisor returned no result"
            if outcome.get("returncode") != 0 or error:
                pytest.fail(error or f"Ticket test exited {outcome.get('returncode')}", pytrace=False)
        except BaseException as error:
            # recv can fail after a complete failure response has arrived.
            # Prefer that traceback while retaining the communication fault.
            if response and not isinstance(error, pytest.fail.Exception):
                try:
                    reported = json.loads(response)
                except ValueError:
                    reported = None
                original = reported.get("error") if isinstance(reported, dict) else None
                if original:
                    failure = pytest.fail.Exception(original, pytrace=False)
                    failure.add_note(f"Isolated Ticket receive failed: {error!r}")
                    error = failure
            errors.append(("test/receive", error))
        finally:
            if process is not None:
                try:
                    cleanup_owned_process(process)
                except BaseException as error:
                    errors.append(("process cleanup", error))
                try:
                    if process.stdin is not None:
                        process.stdin.close()
                except BaseException as error:
                    errors.append(("stdin close", error))
            try:
                for captured, output in ((stdout, sys.stdout), (stderr, sys.stderr)):
                    if captured is not None:
                        captured.seek(0)
                        output.write(captured.read())
            except BaseException as error:
                errors.append(("output read", error))
            try:
                resources.close()
            except BaseException as error:
                errors.append(("resource close", error))
        if errors:
            primary = errors[0][1]
            for phase, error in errors[1:]:
                primary.add_note(f"Isolated Ticket {phase} failed: {error!r}")
            raise primary
    return run
