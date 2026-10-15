"""共享保存与诊断保存均失效后，首次公开 resume 必须真实续接原工作。"""
from __future__ import annotations

import json
from functools import partial
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from typing import Any

import pytest

from agent_run.executor_host import _process_start_token
from agent_run.object_state import object_store
from agent_run.state import StateStore
from agent_run.task_control import TaskControlStore, TaskKey
from support.worker_process_timing import wait_until
from support.workspace import managed_repo, managed_state


@pytest.mark.parametrize("storage_loss", [False, True], ids=["shared_failure", "storage_loss"])
def test_shared_storage_loss_and_first_public_resume_preserve_parallel_work(
    git_repo: Path, tmp_path: Path, storage_loss: bool,
) -> None:
    # 原 test_shared_storage_stop 的代表性真实 Worker 测试在此保留全部退出
    # 边界，并增补 Executor 真正消失后的公开恢复；不重复另一套昂贵准备。
    environment = os.environ.copy()
    project = Path(__file__).parents[1]
    environment["PYTHONPATH"] = os.pathsep.join((str(project / "src"), str(project / "tests")))
    stderr_path = tmp_path / "storage-executor.stderr"
    unrelated = subprocess.Popen([sys.executable, "-c", "import signal; signal.pause()"], start_new_session=True)
    with stderr_path.open("w", encoding="utf-8") as stderr:
        executor = subprocess.Popen(
            [sys.executable, "-m", "support.shared_storage", "interrupt", str(git_repo),
             "storage" if storage_loss else "shared"],
            env=environment, stdout=subprocess.DEVNULL, stderr=stderr, start_new_session=True,
        )
    workers: dict[int, int] = {}
    observed_tokens: dict[int, str] = {}
    try:
        def worker_ready(number: int) -> str | None:
            assert executor.poll() is None, stderr_path.read_text()
            marker = git_repo / f"{number}.ready"
            return marker.read_text() if marker.exists() else None

        for number in (3, 4):
            pid = int(wait_until(partial(worker_ready, number)))
            workers[number] = pid
            token = _process_start_token(pid)
            assert token is not None
            observed_tokens[pid] = token
        states = StateStore(managed_state(git_repo))
        run_id, = states.list_run_ids()
        control = TaskControlStore(states.root)
        task = TaskKey(managed_repo(git_repo), "example/project", 1)

        def started_invocations() -> Any:
            state = states.load_current_run(run_id)
            assert state is not None
            active = state["ticket_agent_invocations"]
            return state if all(active.get(str(number), {}).get("reported_thread_id") == f"held-{number}"
                                and active[str(number)].get("execution_segments") for number in (3, 4)) else None

        wait_until(started_invocations)

        def select_completed(state: dict[str, Any]) -> None:
            state["active_ticket_job"] = state["ticket_jobs"]["2"]
            state["active_agent_invocation"] = state["ticket_agent_invocations"]["2"]

        # 观察选择置于 A，不改变仍在工作的 B/C 的真实归属。
        def save_selection() -> None:
            selected = object_store(states).load_current_run_for_ticket(run_id, 2)
            assert selected is not None
            select_completed(selected)
            object_store(states).save_run(run_id, selected)

        states._commit_run(save_selection)
        before = StateStore(states.root).load_current_run(run_id)
        assert before is not None
        assert before["ticket_jobs"]["2"]["phase"] == "completed"
        assert before["active_ticket_job"]["ticket_number"] == 2
        assert before["ticket_jobs"]["6"]["blocked_reason"] == "agent_requires_human"
        active = before["ticket_agent_invocations"]
        assert active["3"]["semantic_attempt"]["role"] == "reviewer"
        assert active["4"]["role"] == "development"
        assert all(active[str(number)]["deadline_seconds"] > 6 for number in (3, 4))
        saved_control = control.load(task)
        assert saved_control is not None
        owner = saved_control["executor"]
        assert owner["pid"] == executor.pid
        assert owner["process_start_token"] == _process_start_token(executor.pid)
        tokens = {item["pid"]: item["process_start_token"] for item in owner["workers"]}
        assert set(tokens) == set(workers.values())
        assert all(_process_start_token(pid) == token for pid, token in tokens.items())
        started = time.monotonic()
        (git_repo / "storage-fault").touch()
        executor.wait(timeout=5)
        assert time.monotonic() - started < 6
        assert executor.returncode == 0, stderr_path.read_text()
        outcome = json.loads((git_repo / "storage-exit.json").read_text())
        assert outcome["process_returns"] == [-signal.SIGKILL, -signal.SIGKILL]
        assert outcome["live_threads"] == []
        assert outcome["starts"] == [3, 4]
        if storage_loss:
            assert outcome["failed_commits"] >= 2 and outcome["failed_control"] >= 1
            assert "database or disk is full" in outcome["error"]
            diagnostics = stderr_path.read_text()
            assert "durable failure/exit unknown" in diagnostics
            assert "durable admission unknown" in diagnostics
        else:
            assert outcome["failed_commits"] == outcome["failed_control"] == 0
            assert outcome["error"] == "shared execution unavailable"
        for pid in workers.values():
            with pytest.raises(ProcessLookupError):
                os.killpg(pid, 0)
        assert not Path(f"/proc/{executor.pid}").exists()
        assert unrelated.poll() is None
        failed = StateStore(states.root).load_current_run(run_id)
        if storage_loss:
            assert failed == before
            assert control.load(task) == saved_control
        else:
            assert failed["status"] == "execution_failed"
            assert failed["diagnostics"][0]["operator_gate"]["work_subject"] == f"run:{run_id}"
            assert failed["ticket_jobs"]["2"] == before["ticket_jobs"]["2"]
            assert failed["ticket_jobs"]["6"] == before["ticket_jobs"]["6"]
        checkout_root = states.root / "worktrees" / run_id
        reviewer_checkout = checkout_root / "ticket-3"
        developer_checkout = checkout_root / "ticket-4"
        assert (reviewer_checkout / "ticket-3.txt").read_text() == "done\n"
        assert (developer_checkout / "retained-draft.txt").read_text() == "draft 4"

        # 恢复存储后第一次公开命令，未先调用 status/history 或 Controller.resume。
        (git_repo / "storage-fault").unlink()
        agents = git_repo / "resume-agents.json"
        agents.write_text(json.dumps({
            "reviews": [{"expected_thread_id": "held-3", "thread_id": "held-3",
                         "error": "controlled resumed reviewer boundary"}],
            "developments": [{"expected_thread_id": "held-4", "thread_id": "held-4",
                              "write_files": {"resumed.txt": "successor started\n"},
                              "error_after_writes": "controlled resumed developer boundary"}],
        }), encoding="utf-8")
        admitted_snapshot = tmp_path / "resume-admitted.json"
        runtime = tmp_path / "resume-runtime"
        environment.update(RESUME_SNAPSHOT=str(admitted_snapshot), RESUME_RUNTIME=str(runtime))
        resumed = subprocess.run([
            sys.executable, "-m", "support.shared_storage", "resume", "resume", run_id,
            "--github-fixture", str(git_repo / "github.json"),
            "--agent-fixture", str(agents), "--json",
        ], cwd=git_repo, env=environment, capture_output=True, text=True, timeout=40)
        assert resumed.returncode == 2, resumed.stdout + resumed.stderr
        assert admitted_snapshot.exists(), resumed.stdout + resumed.stderr
        admitted = json.loads(admitted_snapshot.read_text())
        assert admitted["action"]["kind"] == "resume"
        assert admitted["action"]["action_id"] != saved_control["action"]["action_id"]
        after = states.load_current_run(run_id)
        assert after is not None
        new_control = control.load(task)
        assert new_control is not None
        successor = new_control["executor"]
        assert successor["generation"] > owner["generation"]
        assert successor["pid"] != executor.pid
        assert successor["status"] == "exited"
        assert not Path(f"/proc/{successor['pid']}").exists()
        assert new_control["action"]["application_observed"] is True
        assert after["action_application_receipt"]["kind"] == "resume"
        assert not list(runtime.glob("environment-*"))
        history = after["agent_invocation_history"]
        previous_starts = {item["started_at"] for item in before["agent_invocation_history"]}
        resumed_history = [item for item in history if item["started_at"] not in previous_starts]
        assert len(resumed_history) == 2, resumed.stdout + resumed.stderr
        for number, role in ((3, "reviewer"), (4, "developer")):
            invocation, = [item for item in resumed_history
                           if item["work_subject"] == active[str(number)]["work_subject"]]
            assert invocation["error"] == f"controlled resumed {role} boundary"
            assert invocation["semantic_attempt"]["attempt_id"] == active[str(number)]["semantic_attempt"]["attempt_id"]
            assert invocation["work_subject"] == active[str(number)]["work_subject"]
            assert invocation["generation"] == active[str(number)]["generation"]
            assert invocation["requested_thread_id"] == f"held-{number}"
            assert invocation["predecessor_invocation_started_at"] == active[str(number)]["started_at"]
        assert after["ticket_jobs"]["2"] == before["ticket_jobs"]["2"]
        assert after["ticket_jobs"]["6"] == before["ticket_jobs"]["6"]
        for number in (3, 4):
            job_before = before["ticket_jobs"][str(number)]
            job_after = after["ticket_jobs"][str(number)]
            for key in ("modification_attempts", "review_budget", "ticket_branch_generation", "policy_snapshot"):
                assert job_after[key] == job_before[key]
            assert job_after["pending_semantic_attempt"] == job_before["pending_semantic_attempt"]
        assert after["ticket_jobs"]["3"]["candidate_sha"] == before["ticket_jobs"]["3"]["candidate_sha"]
        assert after["ticket_jobs"]["3"]["development_thread_id"] == before["ticket_jobs"]["3"]["development_thread_id"]
        assert after["ticket_jobs"]["4"]["development_thread_id"] == "held-4"
        assert after["policy_snapshot"] == before["policy_snapshot"]
        assert after["timeline"][:len(before["timeline"])] == before["timeline"]
        assert (reviewer_checkout / "ticket-3.txt").read_text() == "done\n"
        assert (developer_checkout / "retained-draft.txt").read_text() == "draft 4"
        assert (developer_checkout / "resumed.txt").read_text() == "successor started\n"
        assert unrelated.poll() is None
    finally:
        # 先验证真实退出，再兜底；只回收本例登记的准确进程组。
        for child in (executor, unrelated):
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGKILL)
            child.wait(timeout=5)
        for pid in workers.values():
            if _process_start_token(pid) != observed_tokens.get(pid):
                continue
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
