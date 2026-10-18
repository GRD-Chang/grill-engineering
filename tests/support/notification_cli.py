"""真实通知 CLI 的观察错误通道、就绪屏障与失败回收。"""
from __future__ import annotations

import socket
import subprocess
from pathlib import Path
from typing import Any

from agent_run.process_cleanup import child_subreaper
from agent_run.task_control import TaskControlStore, TaskKey
from support.process_scope import cleanup_owned_process


NOTIFICATION_CLI_SCRIPT = """import os, socket, sys, threading
import agent_run.notifications as notifications
from agent_run.process_cleanup import child_subreaper
from agent_run.cli import main
process_scope = child_subreaper()
process_scope.__enter__()
original_close = notifications.Notifications.close
original_send = notifications.send
original_observe = notifications.Notifications._observe
real_delivery_titles = set()
observer_errors = []
# Capture lifecycle assertions without waiting for transport under the
# StateStore/Task Control transaction. Inspect the exact notification query
# after it has loaded the facts; the commit observer is only a current view.
def observe(self, *args, **kwargs):
    try:
        state = args[0]
        for event in notifications.events(state):
            if event['kind'] in {'run_start', 'ticket_completed', 'boundary'}:
                real_delivery_titles.add(event['title'])
            if not kwargs.get('recovering') and event['kind'] in {'ticket_started', 'parent_started'} and event['id'] not in self.document.get('seen', []):
                active = state.get('active_agent_invocation') or {}
                assert active.get('reported_thread_id') and active.get('status') == 'running', active
        original_observe(self, *args, **kwargs)
    except BaseException as error:
        # Notifications intentionally degrades business callback errors. Keep
        # a separate test channel until close, including in the forked Executor.
        observer_errors.append(error)
        raise
notifications.Notifications._observe = observe
# Detailed-stage combinations are asserted against persisted lifecycle facts.
# Keep the start, completed-ticket effort and approval/completion cards on the
# real executable transport; concise mode still delivers every card for its
# sequence/deduplication assertions, and cancellation keeps its real sender.
if os.environ.get('NOTIFICATION_TEST_REPRESENTATIVE_TRANSPORT'):
    def representative_send(config, card, *args, **kwargs):
        if card['header']['title']['content'] in real_delivery_titles:
            return original_send(config, card, *args, **kwargs)
        return {'outcome': 'success', 'reason': None}
    notifications.send = representative_send
release_sender = threading.Event()
port = os.environ.get('NOTIFICATION_TEST_PORT')
if port:
    notifications.CLOSE_TIMEOUT_SECONDS = 0.05
    def send(*args, **kwargs):
        if not release_sender.wait(30):
            error = AssertionError('notification sender release timed out')
            observer_errors.append(error)
            raise error
        return original_send(*args, **kwargs)
    notifications.send = send
def close(self):
    try:
        if port and self.thread is not None:
            release_sender.set()
            with socket.create_connection(('127.0.0.1', int(port)), timeout=15) as ready:
                ready.sendall(b'C')
                assert ready.recv(1) == b'1', 'close readiness not acknowledged'
    finally:
        release_sender.set()
        try:
            original_close(self)
            assert self.thread is None or not self.thread.is_alive(), 'notification sender did not exit'
        finally:
            if observer_errors:
                raise observer_errors[0]
notifications.Notifications.close = close
try:
    result = main(sys.argv[1:])
finally:
    if observer_errors:
        raise observer_errors[0]
raise SystemExit(result)
"""


def run_notification_cli(
    arguments: list[str], repo: Path, environment: dict[str, str],
    *, ready: socket.socket | None = None, task: TaskKey | None = None, state_root: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """正常退出先读回；任一屏障或退出失败均回收本次 CLI/Executor。"""
    with child_subreaper():
        assert (task is None) == (state_root is None)
        before = None
        if state_root is not None and (state_root / "state.sqlite3").is_file():
            assert task is not None
            before = TaskControlStore(state_root).load(task)
        return _run_notification_cli(arguments, repo, environment, ready=ready, state_root=state_root, task=task, before=before)


def _run_notification_cli(
    arguments: list[str], repo: Path, environment: dict[str, str],
    *, ready: socket.socket | None, state_root: Path | None,
    task: TaskKey | None, before: dict[str, Any] | None,
) -> subprocess.CompletedProcess[str]:
    process = subprocess.Popen(
        arguments, cwd=repo, env=environment, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
    )
    connections: dict[bytes, socket.socket] = {}
    accepted: list[socket.socket] = []
    try:
        if ready is not None:
            for _ in range(2):
                connection, _ = ready.accept()
                accepted.append(connection)
                connection.settimeout(15)
                key = connection.recv(1)
                if key not in {b'C', b'S'} or key in connections:
                    connection.close()
                    raise AssertionError(f'unexpected notification readiness: {key!r}')
                connections[key] = connection
            assert set(connections) == {b'C', b'S'}
            connections[b'C'].sendall(b'1')
        stdout, stderr = process.communicate(timeout=8 if ready is not None else 45)
        assert process.returncode == 0, stdout + stderr
        return subprocess.CompletedProcess(arguments, process.returncode, stdout, stderr)
    except BaseException as error:
        for connection in accepted:
            connection.close()
        declared = {}
        try:
            if state_root is not None and task is not None and (state_root / "state.sqlite3").is_file():
                current = TaskControlStore(state_root).load(task)
                executor = (current or {}).get("executor") or {}
                previous = (before or {}).get("executor") or {}
                binding = (executor.get("action_id"), executor.get("generation"))
                if (binding != (previous.get("action_id"), previous.get("generation"))
                        and type(executor.get("pid")) is int
                        and isinstance(executor.get("process_start_token"), str)):
                    declared[executor["pid"]] = executor["process_start_token"]
        except BaseException as lookup_error:
            error.add_note(f'notification Executor ownership lookup failed: {lookup_error!r}')
        try:
            cleanup_owned_process(process, declared)
        except BaseException as cleanup_error:
            error.add_note(f'notification CLI cleanup failed: {cleanup_error!r}')
        raise
    finally:
        for connection in accepted:
            connection.close()
        assert process.stdout is not None and process.stderr is not None
        process.stdout.close()
        process.stderr.close()
