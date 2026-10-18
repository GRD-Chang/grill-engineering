"""真实通知 CLI 测试支撑的错误传播和失败清理回归。"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from support.notification_cli import NOTIFICATION_CLI_SCRIPT, run_notification_cli
from support.workspace import prepare_workspace


@pytest.mark.parametrize("error_type", ["AssertionError", "OSError"])
def test_notification_observer_failure_survives_business_catch(
    tmp_path: Path, error_type: str,
) -> None:
    # Execute the exact CLI instrumentation in a separate interpreter. The
    # business observer catches this deliberate failure before test close
    # re-raises it; no full delivery or transport is needed for that boundary.
    script = '''import sys
from pathlib import Path
import agent_run.cli
import agent_run.notifications as notifications

def broken_observe(self, *args, **kwargs):
    raise ERROR_TYPE('controlled observer failure')
notifications.Notifications._observe = broken_observe
notifications.events = lambda state: []
def main(arguments):
    state = {'run_id': 'r', 'language': 'en', 'repository': 'o/repo',
             'parent': {'number': 1}, 'notifications': {'enabled': False}}
    sender = notifications.Notifications(Path(sys.argv[1]), state)
    sender.enabled = True
    try:
        sender.observe(state)
        assert sender.failure_reason == 'controlled observer failure'
        print('business observer caught the deliberate error', flush=True)
        return 0
    finally:
        sender.close()
agent_run.cli.main = main
'''.replace("ERROR_TYPE", error_type) + NOTIFICATION_CLI_SCRIPT
    environment = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")}
    environment.pop("NOTIFICATION_TEST_PORT", None)
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)], env=environment,
        text=True, capture_output=True, timeout=8, check=False,
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert "business observer caught the deliberate error" in result.stdout
    assert f"{error_type}: controlled observer failure" in result.stderr


@pytest.mark.parametrize("only_ready", ["C", "S"], ids=["missing-sender", "missing-close"])
def test_notification_readiness_failure_reaps_cli_and_keeps_original_error(
    git_repo: Path, tmp_path: Path, only_ready: str,
) -> None:
    # A proven live child reports the same half twice, so the other readiness
    # is absent. Detect it without a startup race or a short elapsed window.
    prepare_workspace(git_repo)
    pid_file = tmp_path / "ready.pid"
    script = '''import os, signal, socket, sys
from pathlib import Path
Path(sys.argv[2]).write_text(str(os.getpid()))
with socket.create_connection(('127.0.0.1', int(sys.argv[1]))) as first:
    first.sendall(sys.argv[3].encode())
    with socket.create_connection(('127.0.0.1', int(sys.argv[1]))) as second:
        second.sendall(sys.argv[3].encode())
        signal.pause()
'''
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        server.settimeout(8)
        with pytest.raises(AssertionError, match="unexpected notification readiness") as caught:
            run_notification_cli(
                [sys.executable, "-c", script, str(server.getsockname()[1]),
                 str(pid_file), only_ready],
                git_repo, dict(os.environ), ready=server,
            )
    assert pid_file.exists(), "child did not reach the intended readiness boundary"
    pid = int(pid_file.read_text())
    assert not Path(f"/proc/{pid}").exists()
    assert not getattr(caught.value, "__notes__", []), "cleanup also failed"


def test_failed_cli_exit_still_cleans_executor_ownership(
    git_repo: Path, tmp_path: Path,
) -> None:
    from agent_run.task_control import TaskControlStore, TaskKey

    workspace = prepare_workspace(git_repo)
    task = TaskKey(workspace.repository_root, "example/project", 1)
    identity = tmp_path / "detached-executor.pid"
    # CLI has already exited and its Executor changed session. Only the exact
    # new Task binding proves ownership; its PID is absent from CLI descendants.
    script = '''import subprocess, sys
from pathlib import Path
from agent_run.executor_host import _process_start_token
from agent_run.task_control import TaskControlStore, TaskKey
control = TaskControlStore(Path(sys.argv[1]))
task = TaskKey(Path(sys.argv[2]), 'example/project', 1)
claim = control.claim_action(task, kind='run', payload={'parent': 1})
reservation = control.begin_executor(task, action_id=claim.action_id, run_id=None)
child = subprocess.Popen([sys.executable, '-c', 'import signal; signal.pause()'],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
control.mark_process_started(task, action_id=claim.action_id,
    generation=reservation.generation, pid=child.pid,
    process_start_token=_process_start_token(child.pid))
Path(sys.argv[3]).write_text(str(child.pid))
print('controlled CLI failure')
raise SystemExit(7)
'''
    source = str(Path(__file__).parents[1] / "src")
    environment = {**os.environ, "PYTHONPATH": source + os.pathsep + os.environ.get("PYTHONPATH", "")}
    with pytest.raises(AssertionError, match="controlled CLI failure") as caught:
        run_notification_cli(
            [sys.executable, "-c", script, str(workspace.state_root),
             str(workspace.repository_root), str(identity)],
            git_repo, environment, task=task, state_root=workspace.state_root,
        )
    pid = int(identity.read_text())
    record = TaskControlStore(workspace.state_root).load(task)
    assert record is not None and record["executor"]["pid"] == pid
    assert not Path(f"/proc/{pid}").exists()
    assert not getattr(caught.value, "__notes__", []), "cleanup also failed"


def test_failed_notification_cli_leaves_other_task_executor_running(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import select

    from agent_run.executor_host import _process_start_token
    from agent_run.task_control import TaskControlStore, TaskKey

    workspace = prepare_workspace(git_repo)
    control = TaskControlStore(workspace.state_root)
    other_task = TaskKey(workspace.repository_root, "example/project", 99)
    original_popen = subprocess.Popen
    # The other Executor creates its child AFTER our CLI starts. Merely
    # subtracting preexisting descendants would incorrectly own this new PID.
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
    unrelated = original_popen([sys.executable, "-c", producer], text=True,
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    descendants: list[int] = []

    def start_cli(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        assert unrelated.stdin is not None and unrelated.stdout is not None
        unrelated.stdin.write("spawn\n")
        unrelated.stdin.flush()
        assert select.select([unrelated.stdout], [], [], 5)[0], "other child did not start"
        descendants.append(int(unrelated.stdout.readline()))
        return process

    try:
        claim = control.claim_action(other_task, kind="run", payload={"parent": 99})
        reservation = control.begin_executor(other_task, action_id=claim.action_id, run_id=None)
        control.mark_process_started(other_task, action_id=claim.action_id,
                                     generation=reservation.generation, pid=unrelated.pid,
                                     process_start_token=_process_start_token(unrelated.pid))
        before = control.load(other_task)
        assert before is not None and before["executor"]["status"] == "starting"
        monkeypatch.setattr(subprocess, "Popen", start_cli)
        with pytest.raises(AssertionError, match="controlled CLI failure"):
            run_notification_cli(
                [sys.executable, "-c", "print('controlled CLI failure'); raise SystemExit(7)"],
                git_repo, dict(os.environ), state_root=workspace.state_root,
                task=TaskKey(workspace.repository_root, "example/project", 1),
            )
        assert unrelated.poll() is None
        assert descendants and all(Path(f"/proc/{pid}").exists() for pid in descendants)
        assert control.load(other_task) == before
    finally:
        assert unrelated.stdin is not None and unrelated.stdout is not None
        if unrelated.poll() is None:
            unrelated.stdin.write("exit\n")
            unrelated.stdin.flush()
        try:
            unrelated.wait(timeout=5)
        finally:
            unrelated.stdin.close()
            unrelated.stdout.close()
