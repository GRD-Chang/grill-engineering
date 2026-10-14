"""共享存储失效场景的独立 Executor；只替换外部 Agent/Host 边界。"""
from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
from threading import enumerate as threads
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

from agent_run import cli
from agent_run.codex import CodexCliBackend
from agent_run.controller import Controller
from agent_run.executor import DeliveryExecutor
from agent_run.executor_admission import executor_admission_scope
from agent_run.executor_environment import capture_executor_environment, write_environment_carrier
from agent_run.executor_host import FakeExecutorHost, _process_start_token
from agent_run.git import GitRepository
from agent_run.github_fixture import FixtureGitHubReader
from agent_run.run_lifecycle import prepare_action_application_receipt
from agent_run.state import StateStore
from agent_run.task_control import TaskControlStore, TaskKey
from support.cli_payloads import human_blocker_step
from support.seeding import seed_run, write_fixture
from support.ticket_parallel import TicketAgents, ticket_engine
from support.worker_process_timing import wait_until
from support.workspace import managed_repo, managed_state


def interrupt_shared_storage(repo: Path, *, storage_loss: bool) -> None:
    """运行真实并发 Reviewer/Developer，等待父进程注入持续 commit 失效。"""
    fixture = write_fixture(repo / "github.json", issues={
        str(number): {
            "number": number, "title": f"Ticket {number}", "body": f"Deliver {number}",
            "state": "OPEN", "labels": ["ready-for-agent"],
            "blocked_by": [{"number": 3, "state": "OPEN"}] if number == 5 else [],
        } for number in (2, 3, 4, 5, 6)
    })
    assert seed_run(repo, fixture, "1", "--ticket-development-workers", "2").returncode == 0
    states = StateStore(managed_state(repo))
    git = GitRepository(managed_repo(repo))
    run_id, = states.list_run_ids()
    agents = TicketAgents(repo, (2, 3, 4, 5, 6), lambda _: None)
    agents.backends[6].data["developments"] = [human_blocker_step("developer-6")]
    engine = ticket_engine(git, states, fixture, agents)
    engine.deliver_ticket(run_id, 6)
    engine.deliver_ticket(run_id, 2)
    controller = Controller(FixtureGitHubReader(fixture), git, states)
    state, _ = controller.resume(run_id)
    assert state["ticket_jobs"]["2"]["phase"] == "completed"
    assert state["ticket_jobs"]["6"]["blocked_reason"] == "agent_requires_human"

    control = TaskControlStore(states.root)
    task = TaskKey(git.root, "example/project", 1)
    claim = control.claim_action(task, kind="run", payload={})
    action_id = claim.action_id
    assert action_id is not None and claim.action is not None
    generation = control.begin_executor(task, action_id=action_id, run_id=run_id).generation
    control.bind_run(task, action_id, run_id, generation=generation)
    control.mark_handshake(task, action_id=action_id, generation=generation, pid=os.getpid(), process_start_token=_process_start_token(os.getpid()))
    bound = control.load(task)
    assert bound is not None
    prepare_action_application_receipt(state, bound["action"])
    states.save_run(run_id, state)
    control.record_application(task, action_id=action_id, generation=generation, run_id=run_id, payload_digest=claim.action["payload_digest"])
    control.complete_action(task, action_id=action_id, generation=generation, result_status="active")

    script = repo / "held-worker.py"
    script.write_text('''import json, os, pathlib, signal, sys, time
number, root = sys.argv[1:]
signal.signal(signal.SIGTERM, signal.SIG_IGN)
print(json.dumps({"type": "thread.started", "thread_id": "held-" + number}), flush=True)
if number == "4":
    pathlib.Path("retained-draft.txt").write_text("draft " + number)
(pathlib.Path(root) / (number + ".ready")).write_text(str(os.getpid()))
while True:
    time.sleep(1)
''', encoding="utf-8")
    starts: list[int] = []
    processes: list[subprocess.Popen[Any]] = []
    real_popen = subprocess.Popen

    def popen(*args: Any, **kwargs: Any) -> subprocess.Popen[Any]:
        child = real_popen(*args, **kwargs)
        if str(script) in args[0]:
            processes.append(child)
        return child

    def command(arguments: list[str], *, checkout: Path, **_options: Any) -> list[str]:
        number = int(checkout.name.split("ticket-", 1)[1].split("-", 1)[0])
        starts.append(number)
        return [sys.executable, str(script), str(number), str(repo)]

    def worker_started(pid: int) -> None:
        token = _process_start_token(pid)
        assert token is not None
        control.mark_worker_started(task, action_id=action_id, generation=generation,
                                    pid=pid, process_start_token=token)

    def worker_finished(pid: int) -> None:
        control.mark_worker_finished(task, action_id=action_id, generation=generation,
                                     pid=pid, process_start_token=tokens[pid])

    backend = CodexCliBackend(on_worker_started=worker_started, on_worker_finished=worker_finished)

    class Processes(TicketAgents):
        def develop(self, request: dict[str, Any]) -> Any:
            if self._number(request) == 4:
                return backend.develop(request)
            return super().develop(request)

        def review(self, request: dict[str, Any]) -> Any:
            return backend.review(request)

    engine = ticket_engine(git, states, fixture, Processes(repo, (3, 4, 5, 6), lambda _: None))
    tokens: dict[int, str] = {}
    failed_commits: list[str] = []
    failed_control: list[str] = []
    real_connect = sqlite3.connect
    real_write = control._write_unlocked
    fault_path = repo / "storage-fault"

    class FailingConnection(sqlite3.Connection):
        def commit(self) -> None:
            if storage_loss and fault_path.exists():
                failed_commits.append("shared commit")
                raise sqlite3.OperationalError("database or disk is full")
            super().commit()

    def connect(database: Any, *args: Any, **kwargs: Any) -> sqlite3.Connection:
        if database == states.root / "state.sqlite3":
            kwargs["factory"] = FailingConnection
        return cast(sqlite3.Connection, real_connect(database, *args, **kwargs))

    def write(target: TaskKey, record: Any) -> None:
        if storage_loss and fault_path.exists():
            failed_control.append("control write")
            raise OSError("control storage unavailable")
        real_write(target, record)

    def supervise(_lanes: frozenset[int]) -> None:
        wait_until(lambda: fault_path.exists())
        record = control.load(task)
        assert record is not None
        tokens.update({worker["pid"]: worker["process_start_token"] for worker in record["executor"]["workers"]})
        if not storage_loss:
            raise RuntimeError("shared execution unavailable")
        controller.record_execution_failure(run_id, "shared state save failed")

    def close(error: BaseException | None) -> None:
        control.close_ticket_resume_admission(
            task, action_id=action_id, generation=generation, run_id=run_id, can_exit=lambda: True, failure=str(error),
        )

    try:
        with patch("agent_run.codex.bubblewrap_command", command), patch.object(subprocess, "Popen", popen), \
                patch.object(sqlite3, "connect", connect), patch.object(control, "_write_unlocked", write), \
                executor_admission_scope(close):
            DeliveryExecutor(
                states=states,
                driver_factory=lambda *_: SimpleNamespace(advance=lambda _: engine.develop_frontier(
                    run_id, state, supervise=supervise,
                )),
                record_execution_failure=lambda _states, target, message, _binding: controller.record_execution_failure(target, message),
            ).execute(run_id, action_id=action_id, generation=generation)
    except BaseException as error:
        # 在兜底清理前记录真实退出结果；不能用清理制造取消成功。
        (repo / "storage-exit.json").write_text(json.dumps({
            "error": str(error), "starts": sorted(starts),
            "process_returns": [process.poll() for process in processes],
            "live_threads": [item.name for item in threads()
                             if item.name.startswith(("ticket_", "agent-run-worker-"))],
            "failed_commits": len(failed_commits), "failed_control": len(failed_control),
        }), encoding="utf-8")
    finally:
        for child in processes:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGKILL)
            child.wait(timeout=5)


class IndependentResumeHost(FakeExecutorHost):
    """首次恢复从持久请求进入生产 Executor.main，不继承 Run 内存闭包。"""
    def ensure(self, spec: Any, control: TaskControlStore, execute: Any = None, recover: bool = False) -> Any:
        reservation = control.begin_executor(spec.task, action_id=spec.action_id,
                                             run_id=spec.run_id, reclaim=recover)
        assert reservation.created
        Path(os.environ["RESUME_SNAPSHOT"]).write_text(json.dumps(control.load(spec.task)))
        spec = replace(spec, generation=reservation.generation)
        carrier = write_environment_carrier(Path(os.environ["RESUME_RUNTIME"]), spec,
            capture_executor_environment(os.environ, command=spec.command))
        child = (
            'import os, sys; from agent_run import cli, executor; '
            'from agent_run.executor_host import BoundExecutorHost; '
            'cli.FakeExecutorHost = lambda **kwargs: BoundExecutorHost('
            'action_id=os.environ["AGENT_RUN_EXECUTOR_ACTION_ID"], '
            'generation=int(os.environ["AGENT_RUN_EXECUTOR_GENERATION"])); '
            'raise SystemExit(executor.main(sys.argv[1:]))'
        )
        result = subprocess.run([sys.executable, "-c", child,
            "--workspace", str(spec.task.workspace), "--repository", spec.task.repository,
            "--parent", str(spec.task.parent_number), "--action-id", spec.action_id,
            "--generation", str(spec.generation), "--state-root", str(spec.state_root),
            "--carrier", str(carrier), "--runner-lock", str(carrier.parent / "runner.lock")],
            capture_output=True, text=True, timeout=30)
        assert not carrier.exists()
        assert result.returncode == 2, result.stdout + result.stderr
        return self.inspect(spec, control)


if __name__ == "__main__":
    if sys.argv[1] == "interrupt":
        interrupt_shared_storage(Path(sys.argv[2]), storage_loss=sys.argv[3] == "storage")
    else:
        setattr(cli, "FakeExecutorHost", IndependentResumeHost)
        raise SystemExit(cli.main(sys.argv[2:]))
