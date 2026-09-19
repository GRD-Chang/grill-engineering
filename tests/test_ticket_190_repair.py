from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from agent_run.run_driver import DirectRunOperations, RunDriver, RunOutcome
from agent_run.state import StateStore
from agent_run.task_control import (
    TaskControlBusyError,
    TaskControlStore,
    TaskKey,
)
from cli_run_supervision_support import _parent_only_agents
from conftest import write_fixture
from support.workspace import managed_workspace
from test_cli_delivery import ticket


def _isolated_environment(root: Path) -> dict[str, str]:
    return {
        "HOME": str(root / "home"),
        "XDG_CONFIG_HOME": str(root / "config"),
        "XDG_DATA_HOME": str(root / "data"),
        "XDG_STATE_HOME": str(root / "state"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
    }


def _wait_for_marker(process: subprocess.Popen[str], marker: Path) -> None:
    deadline = time.monotonic() + 10
    while not marker.exists():
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            raise AssertionError(f"fixture CLI exited early: {stdout}\n{stderr}")
        if time.monotonic() >= deadline:
            raise AssertionError("fixture CLI did not reach the Agent barrier")
        os.sched_yield()


def _write_competition_sitecustomize(path: Path) -> None:
    path.write_text(
        """from contextlib import contextmanager
import os
from pathlib import Path
import time

from agent_run.managed_workspace import ManagedWorkspace
from agent_run.task_control import (
    TaskControlBusyError,
    TaskControlStore,
    TaskKey,
)
import threading


role = os.environ.get("TICKET_190_ROLE")

if role == "first":
    original_transaction = TaskControlStore._executor_current_transaction

    @contextmanager
    def observed_transaction(self, task, *, action_id, generation, run_id=None):
        agent_release = os.environ.get("TICKET_190_AGENT_RELEASE")
        holder = os.environ.get("TICKET_190_HOLDER")
        attempted = os.environ.get("TICKET_190_ATTEMPTED")
        busy = os.environ.get("TICKET_190_BUSY")
        if (
            agent_release
            and holder
            and attempted
            and Path(agent_release).exists()
            and Path(holder).exists()
            and not Path(attempted).exists()
        ):
            Path(attempted).touch()
        try:
            with original_transaction(
                self,
                task,
                action_id=action_id,
                generation=generation,
                run_id=run_id,
            ):
                yield
        except TaskControlBusyError:
            if busy:
                Path(busy).touch()
            raise

    TaskControlStore._executor_current_transaction = observed_transaction

elif role == "second":
    original_locked = TaskControlStore._locked
    hold_state = {"used": False}

    def held_lock(self, task):
        manager = original_locked(self, task)

        @contextmanager
        def hold_once():
            with manager:
                marker = os.environ.get("TICKET_190_HOLDER")
                release = os.environ.get("TICKET_190_HOLDER_RELEASE")
                if marker and release and not hold_state["used"]:
                    hold_state["used"] = True
                    Path(marker).touch()
                    deadline = time.monotonic() + 10
                    while not Path(release).exists():
                        if time.monotonic() >= deadline:
                            raise RuntimeError("Task Control barrier timed out")
                        time.sleep(0.001)
                yield

        return hold_once()

    TaskControlStore._locked = held_lock

    def hold_same_parent_transaction():
        workspace = ManagedWorkspace.for_repository("example/project")
        store = TaskControlStore(workspace.state_root)
        task = TaskKey(workspace.repository_root, "example/project", 1)
        deadline = time.monotonic() + 10
        while True:
            record = store.load(task)
            executor = record.get("executor") if record else None
            if (
                isinstance(executor, dict)
                and executor.get("status") == "running"
            ):
                break
            if time.monotonic() >= deadline:
                raise RuntimeError("same-parent Executor did not become active")
            time.sleep(0.001)
        try:
            with store._locked(task):
                pass
        except BaseException as error:
            error_path = os.environ.get("TICKET_190_HOLDER_ERROR")
            if error_path:
                Path(error_path).write_text(str(error), encoding="utf-8")

    threading.Thread(target=hold_same_parent_transaction, daemon=False).start()
""",
        encoding="utf-8",
    )


def test_fixture_executor_keeps_reserved_generation_after_replacement(
    git_repo: Path, tmp_path: Path
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": ticket()})
    agents = _parent_only_agents(git_repo / "agents.json")
    started = tmp_path / "agent-started"
    release = tmp_path / "agent-release"
    data = json.loads(agents.read_text(encoding="utf-8"))
    data["invocation_gate"] = {
        "role": "development",
        "started_file": str(started),
        "release_file": str(release),
        "timeout_seconds": 30,
    }
    agents.write_text(json.dumps(data), encoding="utf-8")
    environment = _isolated_environment(tmp_path / "generation")
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    workspace = managed_workspace(git_repo, extra_env=environment)
    command = [
        sys.executable,
        "-m",
        "agent_run",
        "run",
        "1",
        "--agent-fixture",
        str(agents),
        "--github-fixture",
        str(fixture),
    ]
    process = subprocess.Popen(
        command,
        cwd=git_repo,
        env={**os.environ, **environment},
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        _wait_for_marker(process, started)
        state_path = next((workspace.state_root / "runs").glob("*.json"))
        control = TaskControlStore(workspace.state_root)
        task = TaskKey(workspace.repository_root, "example/project", 1)
        control_path = next((workspace.state_root / "task-control").glob("*.json"))
        record = json.loads(control_path.read_text(encoding="utf-8"))
        action_id = record["action"]["action_id"]
        generation = record["executor"]["generation"]
        state_before_release = state_path.read_bytes()
        fixture_before_release = fixture.read_bytes()
        agents_before_release = agents.read_bytes()
        git_status_before_release = {
            repo: subprocess.run(
                ["git", "status", "--short"],
                cwd=repo,
                text=True,
                capture_output=True,
                check=True,
            ).stdout
            for repo in (git_repo, workspace.repository_root)
        }

        control.mark_executor_absent(
            task, action_id=action_id, generation=generation
        )
        successor_claim = control.claim_action(
            task, kind="run", payload=record["action"]["payload"]
        )
        assert successor_claim.action_id is not None
        run_id = record["executor"]["run_id"]
        assert isinstance(run_id, str)
        control.bind_run(task, successor_claim.action_id, run_id)
        successor = control.begin_executor(
            task,
            action_id=successor_claim.action_id,
            run_id=run_id,
        )
        assert successor.generation != generation

        release.touch()
        stdout, stderr = process.communicate(timeout=30)
        assert process.returncode == 2, f"{stdout}\n{stderr}"
        assert state_path.read_bytes() == state_before_release
        assert fixture.read_bytes() == fixture_before_release
        assert agents.read_bytes() == agents_before_release
        assert {
            repo: subprocess.run(
                ["git", "status", "--short"],
                cwd=repo,
                text=True,
                capture_output=True,
                check=True,
            ).stdout
            for repo in git_status_before_release
        } == git_status_before_release
        current = control.load(task)
        assert current is not None
        assert current["executor"]["generation"] == successor.generation
        assert current["executor"]["status"] == "starting"
    finally:
        release.touch()
        if process.poll() is None:
            process.terminate()
            try:
                process.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=3)


def test_real_cli_retries_state_commit_during_same_parent_contention(
    git_repo: Path, tmp_path: Path
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": ticket()})
    agents = _parent_only_agents(git_repo / "agents.json")
    started = tmp_path / "agent-started"
    agent_release = tmp_path / "agent-release"
    holder = tmp_path / "task-control-held"
    holder_release = tmp_path / "task-control-release"
    holder_error = tmp_path / "task-control-error"
    attempted = tmp_path / "state-commit-attempted"
    busy = tmp_path / "state-commit-busy"
    data = json.loads(agents.read_text(encoding="utf-8"))
    data["reviews"].append(
        {**data["reviews"][0], "thread_id": "parent-reviewer-2"}
    )
    data["run_reviews"] = [
        {**data["reviews"][0], "thread_id": "run-reviewer-1"}
    ]
    data["run_publications"] = [dict(data["publications"][0])]
    data["invocation_gate"] = {
        "role": "development",
        "started_file": str(started),
        "release_file": str(agent_release),
        "timeout_seconds": 30,
    }
    agents.write_text(json.dumps(data), encoding="utf-8")

    sitecustomize = tmp_path / "sitecustomize"
    sitecustomize.mkdir()
    _write_competition_sitecustomize(sitecustomize / "sitecustomize.py")
    source_path = str(Path(__file__).resolve().parents[1] / "src")
    base_environment = _isolated_environment(tmp_path / "cli-race")
    base_environment["PYTHONPATH"] = (
        f"{sitecustomize}{os.pathsep}{source_path}"
    )
    workspace = managed_workspace(git_repo, extra_env=base_environment)
    command = [
        sys.executable,
        "-m",
        "agent_run",
        "run",
        "1",
        "--agent-fixture",
        str(agents),
        "--github-fixture",
        str(fixture),
    ]
    status_command = [
        sys.executable,
        "-m",
        "agent_run",
        "status",
        "--parent",
        "1",
        "--json",
        "--github-fixture",
        str(fixture),
    ]
    first_environment = {
        **os.environ,
        **base_environment,
        "TICKET_190_ROLE": "first",
        "TICKET_190_AGENT_RELEASE": str(agent_release),
        "TICKET_190_HOLDER": str(holder),
        "TICKET_190_ATTEMPTED": str(attempted),
        "TICKET_190_BUSY": str(busy),
    }
    second_environment = {
        **os.environ,
        **base_environment,
        "TICKET_190_ROLE": "second",
        "TICKET_190_HOLDER": str(holder),
        "TICKET_190_HOLDER_RELEASE": str(holder_release),
        "TICKET_190_HOLDER_ERROR": str(holder_error),
    }
    first = subprocess.Popen(
        command,
        cwd=git_repo,
        env=first_environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    second: subprocess.Popen[str] | None = None
    try:
        _wait_for_marker(first, started)
        second = subprocess.Popen(
            status_command,
            cwd=git_repo,
            env=second_environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        _wait_for_marker(second, holder)
        control_path = next(
            (workspace.state_root / "task-control").glob("*.json")
        )
        record_before = json.loads(control_path.read_text(encoding="utf-8"))
        executor_before = record_before["executor"]
        assert executor_before["status"] == "running"
        agent_release.touch()
        _wait_for_marker(first, busy)
        holder_release.touch()
        first_stdout, first_stderr = first.communicate(timeout=20)
        second_stdout, second_stderr = second.communicate(timeout=20)
        assert first.returncode == 0, f"{first_stdout}\n{first_stderr}"
        assert second.returncode == 0, f"{second_stdout}\n{second_stderr}"
        assert not holder_error.exists()

        run_files = list((workspace.state_root / "runs").glob("*.json"))
        control_files = list(
            (workspace.state_root / "task-control").glob("*.json")
        )
        assert len(run_files) == 1
        assert len(control_files) == 1
        state = json.loads(run_files[0].read_text(encoding="utf-8"))
        record = json.loads(control_files[0].read_text(encoding="utf-8"))
        assert state["status"] not in {"completed", "abandoned"}
        assert state["status"] != "execution_failed"
        assert record["action"]["status"] == "completed"
        assert record["executor"]["status"] == "exited"
        assert record["executor"]["failure"] is None
        assert record["executor"]["action_id"] == record["action"]["action_id"]
        for key in ("action_id", "generation", "run_id", "pid"):
            assert record["executor"][key] == executor_before[key]
    finally:
        agent_release.touch()
        holder_release.touch()
        for process in (first, second):
            if process is None or process.poll() is not None:
                continue
            process.terminate()
            try:
                process.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=3)


def test_state_commit_retries_task_control_busy_without_failing_run(
    tmp_path: Path,
) -> None:
    task = TaskKey(tmp_path / "checkout", "example/project", 156)
    control = TaskControlStore(tmp_path / "control")
    states = StateStore(tmp_path / "runs")
    states.save_run("run-1", {"run_id": "run-1", "status": "active"})
    claim = control.claim_action(task, kind="run", payload={"parent": 156})
    assert claim.action_id is not None
    control.bind_run(task, claim.action_id, "run-1")
    reservation = control.begin_executor(
        task, action_id=claim.action_id, run_id="run-1"
    )
    control.mark_process_started(
        task,
        action_id=claim.action_id,
        generation=reservation.generation,
        pid=123,
        process_start_token="test-start",
    )
    control.mark_handshake(
        task,
        action_id=claim.action_id,
        generation=reservation.generation,
        pid=123,
        process_start_token="test-start",
    )

    busy_observed = threading.Event()
    ninth_attempt = threading.Event()
    holder_entered = threading.Event()
    release_holder = threading.Event()
    state_peer_entered = threading.Event()
    retry_attempts: list[int] = []
    release_errors: list[BaseException] = []

    def transaction() -> object:
        @contextmanager
        def current_transaction() -> object:
            try:
                with control._executor_current_transaction(
                    task,
                    action_id=claim.action_id,
                    generation=reservation.generation,
                    run_id="run-1",
                ):
                    yield
            except TaskControlBusyError:
                retry_attempts.append(len(retry_attempts) + 1)
                busy_observed.set()
                if len(retry_attempts) >= 9:
                    ninth_attempt.set()
                raise

        return current_transaction()

    states._set_write_guard(lambda: None, transaction=transaction)

    def hold_task_control() -> None:
        with control._locked(task):
            holder_entered.set()
            if not busy_observed.wait(timeout=2):
                raise AssertionError("state commit did not contend for Task Control")
            if not release_holder.wait(timeout=2):
                raise AssertionError("Task Control holder was not released")

    writer_errors: list[BaseException] = []

    def write_state() -> None:
        try:
            states.save_run("run-1", {"run_id": "run-1", "status": "continued"})
        except BaseException as error:  # pragma: no cover - diagnostic propagation
            writer_errors.append(error)

    def release_after_retry() -> None:
        if not ninth_attempt.wait(timeout=1):
            release_errors.append(
                AssertionError("state commit did not reach the ninth retry")
            )
        release_holder.set()

    holder = threading.Thread(target=hold_task_control, daemon=True)
    writer = threading.Thread(target=write_state, daemon=True)
    release_controller = threading.Thread(
        target=release_after_retry, daemon=True
    )

    def observe_state_lock() -> None:
        with states.locked():
            state_peer_entered.set()

    state_peer = threading.Thread(target=observe_state_lock, daemon=True)
    holder.start()
    assert holder_entered.wait(timeout=2)
    release_controller.start()
    writer.start()
    assert busy_observed.wait(timeout=2)
    state_peer.start()
    assert state_peer_entered.wait(timeout=2)
    writer.join(timeout=2)
    holder.join(timeout=2)
    release_controller.join(timeout=2)
    state_peer.join(timeout=2)

    assert not writer.is_alive()
    assert not holder.is_alive()
    assert not release_controller.is_alive()
    assert not state_peer.is_alive()
    assert release_errors == []
    assert writer_errors == []
    assert len(retry_attempts) >= 9
    saved = states.load_run("run-1")
    assert saved is not None
    assert saved["status"] == "continued"
    current = control.load(task)
    assert current is not None
    assert current["action"]["action_id"] == claim.action_id
    assert current["executor"]["generation"] == reservation.generation
    assert current["executor"]["status"] == "running"


def test_run_driver_does_not_route_task_control_busy_to_execution_failure() -> None:
    failures: list[str] = []

    class Controller:
        def record_execution_failure(self, _run_id: str, message: str) -> bool:
            failures.append(message)
            return True

    class Operations:
        controller = Controller()

        @staticmethod
        def classify(_state: dict[str, object]) -> RunOutcome:
            return DirectRunOperations.classify({"status": "active"})

        @staticmethod
        def dispatch(*_args: object) -> RunOutcome:
            raise TaskControlBusyError("short transaction is busy")

    with pytest.raises(TaskControlBusyError):
        RunDriver(
            operations=Operations(),  # type: ignore[arg-type]
            states=object(),  # type: ignore[arg-type]
            supervisor=object(),  # type: ignore[arg-type]
        ).advance({"run_id": "run-1", "status": "active"})
    assert failures == []
