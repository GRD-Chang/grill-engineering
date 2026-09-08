from __future__ import annotations

import fcntl
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, nullcontext
from dataclasses import replace
from pathlib import Path

import pytest

from agent_run import cli
from agent_run.agent_invocation import session_interruption_is_persisted
from agent_run.executor_host import (
    BoundExecutorHost,
    ExecutorLostError,
    ExecutorSpec,
    ExecutorStartUnknownError,
    FakeExecutorHost,
    HostObservation,
)
from agent_run.run_lifecycle import (
    LifecycleRequest,
    RunLifecycle,
    prepare_action_application_receipt,
)
from agent_run.state import SimulatedProcessCrash, StateStore
from agent_run.state_contract import require_current_run_state
from agent_run.systemd_executor_host import (
    FakeSystemdTransport,
    SystemdUnitObservation,
    SystemdUserExecutorHost,
)
from agent_run.task_control import (
    ActionBusyError,
    ActionReconciliationError,
    TaskControlStore,
    TaskKey,
)
from conftest import seed_idle_control, seed_run, write_fixture
from cli_fixtures import run_agents
from cli_run_supervision_support import _parent_only_agents
from test_cli import load_only_run_state, run_cli, stdout_json
from test_cli_delivery import ticket


def _task(tmp_path: Path) -> TaskKey:
    return TaskKey(tmp_path / "checkout", "example/project", 156)


def _isolated_environment(root: Path) -> dict[str, str]:
    return {
        "HOME": str(root / "home"),
        "XDG_CONFIG_HOME": str(root / "config"),
        "XDG_DATA_HOME": str(root / "data"),
        "XDG_STATE_HOME": str(root / "state"),
        "PATH": os.pathsep.join((str(Path(sys.executable).parent), "/usr/bin", "/bin")),
    }


def _file_snapshot(root: Path) -> dict[str, bytes]:
    if not root.exists():
        return {}
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def _assert_public_action_receipt(
    output: Mapping[str, object], *, submission: str
) -> None:
    action = output.get("action")
    assert isinstance(action, Mapping)
    assert set(action) == {
        "repository",
        "parent",
        "operation",
        "submission",
        "status",
        "next_action",
    }
    assert action.get("repository") == "example/project"
    parent = action.get("parent")
    assert isinstance(parent, Mapping)
    assert set(parent) == {"number", "title"}
    assert parent.get("number") == 1
    assert action.get("operation") == "run"
    assert action.get("submission") == submission
    assert action.get("status") in {"applied", "in_progress", "failed"}
    assert isinstance(action.get("next_action"), str)
    run_id = output.get("run_id")
    if isinstance(run_id, str):
        assert run_id not in str(action.get("next_action"))
    assert not {
        "action_id",
        "run_id",
        "payload_digest",
        "generation",
        "executor_generation",
        "executor",
    }.intersection(action)


class _InMemoryRunState:
    def __init__(self) -> None:
        self.value: dict[str, object] = {"run_id": "run-1", "status": "active"}

    @contextmanager
    def locked(self) -> Iterator[None]:
        yield

    def load_current_run(self, run_id: str) -> dict[str, object] | None:
        if self.value.get("run_id") != run_id:
            return None
        return dict(self.value)

    def save_run(self, run_id: str, state: dict[str, object]) -> None:
        if self.value.get("run_id") != run_id:
            raise AssertionError("unexpected Run ID")
        self.value = dict(state)


class _FaultInjectingTaskControlStore(TaskControlStore):
    def __init__(self, root: Path, *, after_write: bool) -> None:
        super().__init__(root)
        self.after_write = after_write

    def _write_unlocked(
        self, task: TaskKey, record: Mapping[str, object]
    ) -> None:
        if not self.after_write:
            raise SimulatedProcessCrash("before Task Control commit")
        super()._write_unlocked(task, record)
        raise SimulatedProcessCrash("after Task Control commit")


class _ActionCloseFaultStore(TaskControlStore):
    def __init__(self, root: Path, *, crash_at: str | None) -> None:
        super().__init__(root)
        self.crash_at = crash_at
        self.injected = False

    def _crash(self, checkpoint: str) -> None:
        if not self.injected and self.crash_at == checkpoint:
            self.injected = True
            raise SimulatedProcessCrash(f"crash at {checkpoint}")

    def complete_action(
        self,
        task: TaskKey,
        *,
        action_id: str,
        generation: int | None = None,
        result_status: str | None = None,
    ) -> dict[str, object]:
        self._crash("pre_action_close")
        record = super().complete_action(
            task,
            action_id=action_id,
            generation=generation,
            result_status=result_status,
        )
        self._crash("post_action_close")
        return record


def test_task_action_admission_is_single_slot_and_duplicate_is_idempotent(
    tmp_path: Path,
) -> None:
    task = _task(tmp_path)
    control = TaskControlStore(tmp_path / "state")

    first = control.claim_action(task, kind="run", payload={"parent": 156})
    duplicate = control.claim_action(task, kind="run", payload={"parent": 156})

    assert first.action_id is not None
    assert duplicate.attached is True
    assert duplicate.action_id == first.action_id

    before = control.path_for(task).read_bytes()
    with pytest.raises(ActionBusyError):
        control.claim_action(task, kind="approve", payload={"parent": 156})
    assert control.path_for(task).read_bytes() == before


def test_run_prepares_executor_environment_before_persisting_a_new_action(
    tmp_path: Path,
) -> None:
    task = _task(tmp_path)
    control = TaskControlStore(tmp_path / "state")
    states = _InMemoryRunState()
    host = FakeExecutorHost()

    def reject_environment() -> None:
        raise ValueError("发起终端环境过大")

    lifecycle = RunLifecycle(
        states=states,  # type: ignore[arg-type]
        control=control,
        host=host,
        task=task,
        preflight=lambda: None,
        select_run=lambda _action: pytest.fail("environment must fail first"),
        initialize_profile=None,
        executor_spec=lambda run_id, action_id, generation: ExecutorSpec(
            task=task,
            action_id=action_id,
            run_id=run_id,
            generation=generation,
        ),
        execute=lambda _run_id: pytest.fail("environment must fail first"),
        prepare_executor_session=reject_environment,
    )

    with pytest.raises(ValueError, match="环境过大"):
        lifecycle.submit(
            LifecycleRequest(task=task, kind="run", payload={"parent": 156})
        )

    assert control.load(task) is None
    assert host.start_count == 0


def test_oversize_second_terminal_attaches_to_running_executor(
    tmp_path: Path,
) -> None:
    task = _task(tmp_path)
    control = TaskControlStore(tmp_path / "state")
    states = _InMemoryRunState()
    claim = control.claim_action(task, kind="run", payload={"parent": 156})
    assert claim.action is not None
    assert claim.action_id is not None
    action_id = claim.action_id
    control.bind_run(task, action_id, "run-1")
    prepare_action_application_receipt(states.value, claim.action)
    states.save_run("run-1", states.value)
    control.record_application(
        task,
        action_id=action_id,
        run_id="run-1",
        payload_digest=str(claim.action["payload_digest"]),
    )
    reservation = control.begin_executor(
        task, action_id=action_id, run_id="run-1"
    )
    control.mark_process_started(
        task,
        action_id=action_id,
        generation=reservation.generation,
        pid=os.getpid(),
        process_start_token=None,
    )
    control.mark_handshake(
        task,
        action_id=action_id,
        generation=reservation.generation,
        pid=os.getpid(),
        process_start_token=None,
    )
    control.complete_action(task, action_id=action_id, result_status="active")
    before = control.path_for(task).read_bytes()
    transport = FakeSystemdTransport()
    host = SystemdUserExecutorHost(
        transport=transport,
        runtime_directory=tmp_path / "runtime",
        environment={"PATH": "x" * 1024},
        executor_python=Path("/usr/bin/python3"),
        max_environment_bytes=128,
    )
    spec = ExecutorSpec(
        task=task,
        action_id=action_id,
        run_id="run-1",
        generation=reservation.generation,
        command=("run", "156"),
        cwd=task.workspace,
        state_root=tmp_path / "state",
    )
    transport.unit = SystemdUnitObservation(
        "running", host._description(spec), os.getpid(), None
    )
    lifecycle = RunLifecycle(
        states=states,  # type: ignore[arg-type]
        control=control,
        host=host,
        task=task,
        preflight=lambda: dict(states.value),
        select_run=lambda _action: pytest.fail("the action is already running"),
        initialize_profile=None,
        executor_spec=lambda run_id, current_action_id, generation: replace(
            spec,
            run_id=run_id,
            action_id=current_action_id,
            generation=generation,
        ),
        execute=lambda _run_id: pytest.fail("the existing Executor owns execution"),
        prepare_executor_session=lambda: host.prepare_environment(spec.command),
    )

    state, resumed, receipt = lifecycle.submit(
        LifecycleRequest(task=task, kind="run", payload={"parent": 156})
    )

    assert state == states.value
    assert resumed is True
    assert receipt.attached is True
    assert receipt.action_id == action_id
    assert receipt.executor_generation == reservation.generation
    assert control.path_for(task).read_bytes() == before
    assert transport.start_count == 0
    assert not list((tmp_path / "runtime").glob("environment-*.json"))


def test_task_control_lock_contention_fails_without_persistent_mutation(
    git_repo: Path, tmp_path: Path
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": ticket()})
    agents = run_agents(git_repo / "agents.json")
    task = TaskKey(git_repo, "example/project", 1)
    control = TaskControlStore(git_repo / ".agent-run")
    lock_path = control.directory / f".{task.fingerprint}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    ready = tmp_path / "lock-ready"
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import fcntl, pathlib, sys; "
                "lock = pathlib.Path(sys.argv[1]).open('a+'); "
                "fcntl.flock(lock.fileno(), fcntl.LOCK_EX); "
                "pathlib.Path(sys.argv[2]).touch(); "
                "sys.stdin.read()"
            ),
            str(lock_path),
            str(ready),
        ],
        stdin=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 10
        while not ready.exists():
            if holder.poll() is not None:
                raise AssertionError("Task Control lock holder exited early")
            if time.monotonic() >= deadline:
                raise AssertionError("Task Control lock holder did not become ready")
            time.sleep(0.01)
        before = _file_snapshot(git_repo / ".agent-run")
        blocked = run_cli(
            git_repo,
            fixture,
            "run",
            "1",
            "--agent-fixture",
            str(agents),
            extra_env=_isolated_environment(tmp_path / "busy"),
        )

        assert blocked.returncode == 2, f"{blocked.stdout}\n{blocked.stderr}"
        assert stdout_json(blocked)["diagnostics"][0]["code"] == "task_control"
        assert _file_snapshot(git_repo / ".agent-run") == before
    finally:
        if holder.stdin is not None:
            holder.stdin.close()
        holder.wait(timeout=10)

    accepted = control.claim_action(task, kind="run", payload={"parent": 1})
    assert accepted.attached is False
    assert accepted.action_id is not None


def test_matching_action_attaches_during_task_control_transaction_contention(
    tmp_path: Path,
) -> None:
    task = _task(tmp_path)
    control = TaskControlStore(tmp_path / "state")
    accepted = control.claim_action(task, kind="run", payload={"parent": 156})
    reservation = control.begin_executor(
        task, action_id=str(accepted.action_id), run_id=None
    )
    before = _file_snapshot(control.root)
    lock_path = control.directory / f".{task.fingerprint}.lock"

    with lock_path.open("a+") as holder:
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        attached = control.claim_action(
            task, kind="run", payload={"parent": 156}
        )
        with pytest.raises(ActionBusyError):
            control.claim_action(task, kind="resume", payload={"parent": 156})

    assert attached.attached is True
    assert attached.action_id == accepted.action_id
    assert reservation.created is True
    assert _file_snapshot(control.root) == before


def test_task_control_preparation_callbacks_run_outside_short_transactions(
    tmp_path: Path,
) -> None:
    task = _task(tmp_path)
    control = TaskControlStore(tmp_path / "state")
    lock_path = control.directory / f".{task.fingerprint}.lock"

    def assert_lock_is_available() -> None:
        with lock_path.open("a+") as lock_file:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:  # pragma: no cover - regression guard
                raise AssertionError("preparation ran under the Task Control lock") from error
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    claim = control.claim_action(
        task,
        kind="run",
        payload={"parent": 156},
        before_create=assert_lock_is_available,
    )
    assert claim.action_id is not None
    reservation = control.begin_executor(
        task,
        action_id=claim.action_id,
        run_id=None,
        before_create=assert_lock_is_available,
    )
    assert reservation.created is True


@pytest.mark.parametrize("after_write", [False, True])
def test_action_admission_crash_window_is_deterministic(
    tmp_path: Path, after_write: bool
) -> None:
    task = _task(tmp_path)
    root = tmp_path / "state"
    faulting = _FaultInjectingTaskControlStore(root, after_write=after_write)

    with pytest.raises(SimulatedProcessCrash):
        faulting.claim_action(task, kind="run", payload={"parent": 156})

    control = TaskControlStore(root)
    record = control.load(task)
    if not after_write:
        assert record is None
        return

    assert record is not None
    assert record["action"]["status"] == "accepted"
    attached = control.claim_action(task, kind="run", payload={"parent": 156})
    assert attached.attached is True
    assert attached.action_id == record["action"]["action_id"]


@pytest.mark.parametrize(
    "crash_at",
    [
        "host_accepted",
        "pre_handshake",
        "post_handshake",
        "pre_action_close",
        "post_action_close",
    ],
)
def test_executor_crash_window_matrix_repeats_without_duplicate_side_effects(
    tmp_path: Path, crash_at: str
) -> None:
    task = _task(tmp_path)
    control = _ActionCloseFaultStore(
        tmp_path / "state",
        crash_at=(crash_at if "action_close" in crash_at else None),
    )
    injected = False

    def host_fault(checkpoint: str) -> None:
        nonlocal injected
        if not injected and checkpoint == crash_at:
            injected = True
            raise SimulatedProcessCrash(f"crash at {checkpoint}")

    host = FakeExecutorHost(fault_hook=host_fault)
    states = _InMemoryRunState()
    agent_invocations = 0
    publisher_writes = 0
    now = 0.0

    def advance(seconds: float) -> None:
        nonlocal now
        now += seconds

    def execute(_run_id: str) -> dict[str, object]:
        nonlocal agent_invocations, publisher_writes
        agent_invocations += 1
        publisher_writes += 1
        return dict(states.value)

    lifecycle = RunLifecycle(
        states=states,  # type: ignore[arg-type]
        control=control,
        host=host,
        task=task,
        preflight=lambda: dict(states.value) if control.load(task) else None,
        select_run=lambda _action: (dict(states.value), False),
        initialize_profile=None,
        executor_spec=lambda run_id, action_id, generation: ExecutorSpec(
            task=task,
            action_id=action_id,
            run_id=run_id,
            generation=generation,
        ),
        execute=execute,
        sleep=advance,
        clock=lambda: now,
        startup_timeout=0.02,
        poll_interval=0.01,
    )
    request = LifecycleRequest(task=task, kind="run", payload={"parent": 156})

    with pytest.raises(SimulatedProcessCrash, match=crash_at):
        lifecycle.submit(request)

    interrupted = control.load(task)
    assert interrupted is not None
    action_id = interrupted["action"]["action_id"]
    generation = interrupted["action"]["executor_generation"]
    control_bytes = control.path_for(task).read_bytes()
    state_before_retry = dict(states.value)

    host.inspect = lambda spec, _control: HostObservation(  # type: ignore[method-assign]
        status="unknown",
        generation=spec.generation,
        pid=None,
        handshake=crash_at
        in {"post_handshake", "pre_action_close", "post_action_close"},
        reason="crashed fixture ownership is not yet proven exited",
    )

    with pytest.raises(
        (ActionReconciliationError, ExecutorStartUnknownError)
    ):
        lifecycle.submit(request)

    repeated = control.load(task)
    assert repeated is not None
    assert repeated["action"]["action_id"] == action_id
    assert repeated["action"]["executor_generation"] == generation == 1
    assert repeated["executor"]["generation"] == generation
    assert control.path_for(task).read_bytes() == control_bytes
    assert states.value == state_before_retry
    assert host.start_count == 1
    assert agent_invocations == 0
    assert publisher_writes == 0


def test_executor_interrupt_releases_the_action_slot(tmp_path: Path) -> None:
    task = _task(tmp_path)
    control = TaskControlStore(tmp_path / "state")
    claim = control.claim_action(task, kind="run", payload={"parent": 156})
    assert claim.action_id is not None
    control.bind_run(task, claim.action_id, "run-1")
    spec = ExecutorSpec(
        task=task,
        action_id=claim.action_id,
        run_id="run-1",
        generation=1,
    )

    def interrupt() -> dict[str, str]:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        FakeExecutorHost().ensure(spec, control, execute=interrupt)

    record = control.load(task)
    assert record is not None
    assert record["action"]["status"] == "failed"
    assert record["executor"]["status"] == "exited"
    retry = control.claim_action(task, kind="run", payload={"parent": 156})
    assert retry.attached is False


def test_detached_fixture_stderr_preserves_utf8_chunk_boundaries(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    task = _task(tmp_path)
    control = TaskControlStore(tmp_path / "state")
    claim = control.claim_action(task, kind="run", payload={"parent": 156})
    assert claim.action_id is not None
    control.bind_run(task, claim.action_id, "run-1")
    spec = ExecutorSpec(
        task=task,
        action_id=claim.action_id,
        run_id="run-1",
        generation=1,
    )
    message = ("a" * 65_535) + "中\n"

    def write_stderr() -> dict[str, str]:
        os.write(2, message.encode())
        return {"status": "complete"}

    FakeExecutorHost(separate_process=True).ensure(
        spec, control, execute=write_stderr
    )

    assert capsys.readouterr().err == message


def test_detached_fixture_stderr_streams_large_output_with_bounded_storage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class StreamingProbe:
        def __init__(self) -> None:
            self.total = 0
            self.largest_write = 0
            self.prefix = ""
            self.suffix = ""

        def write(self, value: str) -> int:
            self.total += len(value)
            self.largest_write = max(self.largest_write, len(value))
            self.prefix = (self.prefix + value)[:32]
            self.suffix = (self.suffix + value)[-32:]
            return len(value)

        def flush(self) -> None:
            pass

    def reject_temporary_storage(*args: object, **kwargs: object) -> None:
        raise AssertionError("stderr streaming must not allocate temporary storage")

    task = _task(tmp_path)
    control = TaskControlStore(tmp_path / "state")
    claim = control.claim_action(task, kind="run", payload={"parent": 156})
    assert claim.action_id is not None
    control.bind_run(task, claim.action_id, "run-1")
    spec = ExecutorSpec(
        task=task,
        action_id=claim.action_id,
        run_id="run-1",
        generation=1,
    )
    probe = StreamingProbe()
    block = b"x" * (64 * 1024)
    block_count = 128

    def write_stderr() -> dict[str, str]:
        os.write(2, b"begin\n")
        for _ in range(block_count):
            os.write(2, block)
        os.write(2, b"\nend\n")
        return {"status": "complete"}

    monkeypatch.setattr(tempfile, "TemporaryFile", reject_temporary_storage)
    monkeypatch.setattr(sys, "stderr", probe)

    FakeExecutorHost(separate_process=True).ensure(
        spec, control, execute=write_stderr
    )

    assert probe.total == len(b"begin\n\nend\n") + (len(block) * block_count)
    assert probe.prefix.startswith("begin\n")
    assert probe.suffix.endswith("\nend\n")
    assert probe.largest_write <= 64 * 1024


def test_task_control_failure_evidence_is_credential_safe(tmp_path: Path) -> None:
    task = _task(tmp_path)
    control = TaskControlStore(tmp_path / "state")
    claim = control.claim_action(task, kind="run", payload={"parent": 156})
    assert claim.action_id is not None

    record = control.fail_action(
        task,
        action_id=claim.action_id,
        failure="Authorization: Bearer super-secret-token",
    )

    failure = record["action"]["failure"]
    assert failure == "Authorization: [REDACTED]"
    assert "super-secret-token" not in control.path_for(task).read_text(
        encoding="utf-8"
    )


def test_unknown_host_result_keeps_generation_without_starting_again(
    tmp_path: Path,
) -> None:
    task = _task(tmp_path)
    control = TaskControlStore(tmp_path / "state")
    claim = control.claim_action(task, kind="run", payload={"parent": 156})
    assert claim.action_id is not None
    control.bind_run(task, claim.action_id, "run-1")

    spec = ExecutorSpec(
        task=task,
        action_id=claim.action_id,
        run_id="run-1",
        generation=1,
    )
    host = FakeExecutorHost(start_outcome="unknown")
    business_calls = 0

    def execute() -> dict[str, str]:
        nonlocal business_calls
        business_calls += 1
        return {"status": "completed"}

    first = host.ensure(spec, control, execute=execute)
    second = host.ensure(spec, control, execute=execute)

    assert first.status == second.status == "unknown"
    assert host.start_count == 1
    assert business_calls == 0
    record = control.load(task)
    assert record is not None
    assert record["executor"]["generation"] == 1
    assert record["executor"]["status"] == "starting"
    assert (
        json.loads(control.path_for(task).read_text(encoding="utf-8"))["action"][
            "action_id"
        ]
        == claim.action_id
    )


def test_run_lifecycle_does_not_reinvoke_business_callback_on_unknown_host(
    tmp_path: Path,
) -> None:
    task = _task(tmp_path)
    control = TaskControlStore(tmp_path / "state")
    states = _InMemoryRunState()
    host = FakeExecutorHost(start_outcome="unknown")
    business_calls = 0
    now = 0.0

    def advance(seconds: float) -> None:
        nonlocal now
        now += seconds

    def execute(_run_id: str) -> dict[str, str]:
        nonlocal business_calls
        business_calls += 1
        return {"status": "completed"}

    lifecycle = RunLifecycle(
        states=states,  # type: ignore[arg-type]
        control=control,
        host=host,
        task=task,
        preflight=lambda: None,
        select_run=lambda _action: (dict(states.value), False),
        initialize_profile=None,
        executor_spec=lambda run_id, action_id, generation: ExecutorSpec(
            task=task,
            action_id=action_id,
            run_id=run_id,
            generation=generation,
        ),
        execute=execute,
        sleep=advance,
        clock=lambda: now,
    )
    request = LifecycleRequest(
        task=task,
        kind="run",
        payload={"parent": task.parent_number},
    )

    for _ in range(2):
        with pytest.raises(ExecutorStartUnknownError) as captured:
            lifecycle.submit(request)
        assert "核验 Executor Host/Unit 与 Task Control" in str(captured.value)
        assert "原样重试命令" in str(captured.value)

    assert business_calls == 0
    assert host.start_count == 1
    record = control.load(task)
    assert record is not None
    assert record["action"]["status"] == "accepted"
    assert record["executor"]["status"] == "starting"


def test_run_lifecycle_treats_exited_native_unit_as_lost(tmp_path: Path) -> None:
    task = _task(tmp_path)
    control = TaskControlStore(tmp_path / "state")
    states = _InMemoryRunState()

    class ExitedHost:
        cleanup_calls = 0

        def ensure(
            self,
            spec: ExecutorSpec,
            store: TaskControlStore,
            execute: object = None,
            recover: bool = False,
        ) -> HostObservation:
            del execute, recover
            reservation = store.begin_executor(
                spec.task, action_id=spec.action_id, run_id=spec.run_id
            )
            return HostObservation(
                "exited", reservation.generation, None, False, "unit failed"
            )

        def inspect(
            self, spec: ExecutorSpec, store: TaskControlStore
        ) -> HostObservation:
            del store
            return HostObservation(
                "exited", spec.generation, None, False, "unit failed"
            )

        def cleanup_startup(self, spec: ExecutorSpec) -> None:
            del spec
            self.cleanup_calls += 1

    host = ExitedHost()
    lifecycle = RunLifecycle(
        states=states,  # type: ignore[arg-type]
        control=control,
        host=host,  # type: ignore[arg-type]
        task=task,
        preflight=lambda: None,
        select_run=lambda _action: (dict(states.value), False),
        initialize_profile=None,
        executor_spec=lambda run_id, action_id, generation: ExecutorSpec(
            task=task,
            action_id=action_id,
            run_id=run_id,
            generation=generation,
        ),
        execute=lambda _run_id: dict(states.value),
        sleep=lambda _seconds: None,
    )

    with pytest.raises(ExecutorLostError, match="完成前退出"):
        lifecycle.submit(
            LifecycleRequest(task=task, kind="run", payload={"parent": 156})
        )
    assert host.cleanup_calls == 1


def test_bound_executor_takes_over_pre_reserved_generation(tmp_path: Path) -> None:
    task = _task(tmp_path)
    control = TaskControlStore(tmp_path / "state")
    states = _InMemoryRunState()
    claim = control.claim_action(task, kind="run", payload={"parent": 156})
    assert claim.action_id is not None
    reservation = control.begin_executor(
        task, action_id=claim.action_id, run_id=None
    )
    host = BoundExecutorHost(
        action_id=claim.action_id, generation=reservation.generation
    )
    lifecycle = RunLifecycle(
        states=states,  # type: ignore[arg-type]
        control=control,
        host=host,
        task=task,
        preflight=lambda: None,
        select_run=lambda _action: (dict(states.value), False),
        initialize_profile=None,
        executor_spec=lambda run_id, action_id, generation: ExecutorSpec(
            task=task,
            action_id=action_id,
            run_id=run_id,
            generation=generation,
        ),
        execute=lambda _run_id: dict(states.value),
        sleep=lambda _seconds: None,
    )

    final, _resumed, receipt = lifecycle.execute_claimed(
        action_id=claim.action_id, generation=reservation.generation
    )

    assert final["run_id"] == "run-1"
    assert receipt.status == "completed"
    record = control.load(task)
    assert record is not None
    assert record["action"]["status"] == "completed"
    assert record["executor"]["status"] == "exited"


def test_approval_boundary_is_not_written_before_executor_handshake(
    tmp_path: Path,
) -> None:
    task = _task(tmp_path)
    control = TaskControlStore(tmp_path / "state")
    states = _InMemoryRunState()
    states.value["status"] = "run_approval_pending"
    seed_idle_control(control, task, str(states.value["run_id"]))
    host = FakeExecutorHost()
    observed: list[bool] = []

    def select_run(action: dict[str, object]) -> tuple[dict[str, object], bool]:
        record = control.load(task)
        assert record is not None
        executor = record.get("executor")
        assert isinstance(executor, dict)
        observed.append(isinstance(executor.get("handshake_at"), str))
        prepare_action_application_receipt(states.value, action)
        return dict(states.value), True

    lifecycle = RunLifecycle(
        states=states,  # type: ignore[arg-type]
        control=control,
        host=host,
        task=task,
        preflight=lambda: dict(states.value),
        select_run=select_run,
        initialize_profile=None,
        executor_spec=lambda run_id, action_id, generation: ExecutorSpec(
            task=task,
            action_id=action_id,
            run_id=run_id,
            generation=generation,
        ),
        execute=lambda _run_id: dict(states.value),
        sleep=lambda _seconds: None,
    )

    state, _resumed, receipt = lifecycle.submit(
        LifecycleRequest(task=task, kind="run", payload={"parent": 156})
    )

    assert observed == [True]
    assert state["status"] == "run_approval_pending"
    assert receipt.status == "completed"


def test_action_slot_is_released_before_run_driver_continues(tmp_path: Path) -> None:
    task = _task(tmp_path)
    control = TaskControlStore(tmp_path / "state")
    states = _InMemoryRunState()
    observed: list[tuple[str, str]] = []

    def select_run(action: dict[str, object]) -> tuple[dict[str, object], bool]:
        prepare_action_application_receipt(states.value, action)
        states.save_run("run-1", states.value)
        return dict(states.value), False

    def execute(_run_id: str) -> dict[str, object]:
        record = control.load(task)
        assert record is not None
        observed.append((record["action"]["status"], record["executor"]["status"]))
        states.value["status"] = "run_approval_pending"
        states.save_run("run-1", states.value)
        return dict(states.value)

    lifecycle = RunLifecycle(
        states=states,  # type: ignore[arg-type]
        control=control,
        host=FakeExecutorHost(),
        task=task,
        preflight=lambda: None,
        select_run=select_run,
        initialize_profile=None,
        executor_spec=lambda run_id, action_id, generation: ExecutorSpec(
            task=task,
            action_id=action_id,
            run_id=run_id,
            generation=generation,
        ),
        execute=execute,
        sleep=lambda _seconds: None,
    )

    state, _resumed, receipt = lifecycle.submit(
        LifecycleRequest(task=task, kind="run", payload={"parent": 156})
    )

    assert observed == [("completed", "running")]
    assert state["status"] == "run_approval_pending"
    assert receipt.status == "completed"


def test_recovered_executor_generation_is_recorded_in_run_receipt(
    tmp_path: Path,
) -> None:
    task = _task(tmp_path)
    control = TaskControlStore(tmp_path / "state")
    states = _InMemoryRunState()
    states.value["status"] = "waiting_external"
    claim = control.claim_action(task, kind="run", payload={"parent": 156})
    assert claim.action is not None
    action_id = claim.action_id
    assert action_id is not None
    control.bind_run(task, action_id, "run-1")
    prepare_action_application_receipt(states.value, claim.action)
    states.save_run("run-1", states.value)

    host = FakeExecutorHost()
    spec = ExecutorSpec(
        task=task,
        action_id=action_id,
        run_id="run-1",
        generation=1,
    )
    started = host.ensure(spec, control)
    assert started.status == "running"
    control.mark_executor_absent(task, action_id=action_id, generation=1)

    lifecycle = RunLifecycle(
        states=states,  # type: ignore[arg-type]
        control=control,
        host=host,
        task=task,
        preflight=lambda: dict(states.value),
        select_run=lambda _action: pytest.fail("the action is already bound"),
        initialize_profile=None,
        executor_spec=lambda run_id, action_id, generation: ExecutorSpec(
            task=task,
            action_id=action_id,
            run_id=run_id,
            generation=generation,
        ),
        execute=lambda _run_id: dict(states.value),
        sleep=lambda _seconds: None,
    )

    state, _resumed, receipt = lifecycle.submit(
        LifecycleRequest(task=task, kind="run", payload={"parent": 156})
    )

    assert state["run_id"] == "run-1"
    assert receipt.status == "completed"
    current = control.load(task)
    assert current is not None
    assert current["action"]["executor_generation"] == 2
    assert state["action_application_receipt"]["executor_generation"] == 2


def test_absent_executor_cannot_record_application_for_its_old_generation(
    tmp_path: Path,
) -> None:
    task = _task(tmp_path)
    control = TaskControlStore(tmp_path / "state")
    claim = control.claim_action(task, kind="run", payload={"parent": 156})
    assert claim.action is not None
    assert claim.action_id is not None
    control.bind_run(task, claim.action_id, "run-1")
    host = FakeExecutorHost()
    spec = ExecutorSpec(
        task=task,
        action_id=claim.action_id,
        run_id="run-1",
        generation=1,
    )

    started = host.ensure(spec, control)
    assert started.status == "running"
    control.mark_executor_absent(task, action_id=claim.action_id, generation=1)

    with pytest.raises(ActionReconciliationError, match="no longer active"):
        control.record_application(
            task,
            action_id=claim.action_id,
            run_id="run-1",
            payload_digest=str(claim.action["payload_digest"]),
            generation=1,
        )


def test_applied_receipt_without_executor_fails_closed(
    tmp_path: Path,
) -> None:
    task = _task(tmp_path)
    control = TaskControlStore(tmp_path / "state")
    states = _InMemoryRunState()
    states.value["status"] = "waiting_external"
    claim = control.claim_action(task, kind="run", payload={"parent": 156})
    assert claim.action is not None
    action_id = claim.action_id
    assert action_id is not None
    control.bind_run(task, action_id, "run-1")
    prepare_action_application_receipt(states.value, claim.action)
    states.save_run("run-1", states.value)
    control_before = control.path_for(task).read_bytes()
    host = FakeExecutorHost()
    business_calls = 0

    def execute(_run_id: str) -> dict[str, str]:
        nonlocal business_calls
        business_calls += 1
        return {"status": "completed"}

    lifecycle = RunLifecycle(
        states=states,  # type: ignore[arg-type]
        control=control,
        host=host,
        task=task,
        preflight=lambda: dict(states.value),
        select_run=lambda _action: pytest.fail("the action is already bound"),
        initialize_profile=None,
        executor_spec=lambda run_id, action_id, generation: ExecutorSpec(
            task=task,
            action_id=action_id,
            run_id=run_id,
            generation=generation,
        ),
        execute=execute,
    )

    with pytest.raises(ExecutorStartUnknownError):
        lifecycle.submit(
            LifecycleRequest(task=task, kind="run", payload={"parent": 156})
        )

    assert host.start_count == 0
    assert business_calls == 0
    assert control.path_for(task).read_bytes() == control_before


@pytest.mark.parametrize("control_case", ["missing", "corrupt"])
@pytest.mark.parametrize("host_status", ["running", "unknown", "conflict"])
def test_untrusted_control_receipt_retries_stay_fail_closed(
    tmp_path: Path, control_case: str, host_status: str
) -> None:
    task = _task(tmp_path)
    control = TaskControlStore(tmp_path / "state")
    states = _InMemoryRunState()
    active_invocation = {
        "status": "running",
        "role": "development",
        "started_at": "2026-09-01T00:00:00+00:00",
        "work_subject": "ticket:193",
    }
    states.value["active_agent_invocation"] = dict(active_invocation)
    states.value["agent_invocation_history"] = [dict(active_invocation)]
    claim = control.claim_action(task, kind="run", payload={"parent": 156})
    assert claim.action is not None
    action = claim.action
    control.bind_run(task, claim.action_id or "", "run-1")
    prepare_action_application_receipt(states.value, action)
    states.save_run("run-1", states.value)
    if control_case == "missing":
        control.path_for(task).unlink()
    else:
        control.path_for(task).write_text("not json", encoding="utf-8")

    class UncertainHost(FakeExecutorHost):
        def __init__(self) -> None:
            super().__init__()
            self.observe_count = 0

        def observe(
            self, spec: ExecutorSpec, store: TaskControlStore
        ) -> HostObservation:
            del store
            self.observe_count += 1
            return HostObservation(
                host_status,  # type: ignore[arg-type]
                spec.generation,
                None,
                False,
                "host ownership remains uncertain",
            )

    host = UncertainHost()
    business_calls = 0

    def execute(_run_id: str) -> dict[str, str]:
        nonlocal business_calls
        business_calls += 1
        return {"status": "completed"}

    lifecycle = RunLifecycle(
        states=states,  # type: ignore[arg-type]
        control=control,
        host=host,
        task=task,
        preflight=lambda: dict(states.value),
        select_run=lambda _action: pytest.fail(
            "receipt reconciliation must not select a new Run"
        ),
        initialize_profile=None,
        executor_spec=lambda run_id, action_id, generation: ExecutorSpec(
            task=task,
            action_id=action_id,
            run_id=run_id,
            generation=generation,
        ),
        execute=execute,
    )

    with pytest.raises(ExecutorStartUnknownError):
        lifecycle.submit(
            LifecycleRequest(
                task=task,
                kind="run",
                payload={"parent": 156},
            )
        )

    assert host.start_count == 0
    assert host.observe_count == 1
    assert business_calls == 0
    repaired = control.load(task)
    assert repaired is not None
    assert repaired["executor"]["reconciliation_required"] is True
    repaired_bytes = control.path_for(task).read_bytes()
    state_before_retry = dict(states.value)
    persisted_failure = False

    def persist_failure() -> None:
        nonlocal persisted_failure
        persisted_failure = True

    with pytest.raises(ActionReconciliationError, match="未证明原 Executor 已退出"):
        control.fail_session_from_application_receipt(
            task,
            action_id=str(action["action_id"]),
            generation=1,
            application_receipt=states.value["action_application_receipt"],
            persist_run_failure=persist_failure,
        )
    assert persisted_failure is False
    assert control.path_for(task).read_bytes() == repaired_bytes

    for _attempt in range(2):
        with pytest.raises(ExecutorStartUnknownError):
            lifecycle.submit(
                LifecycleRequest(task=task, kind="run", payload={"parent": 156})
            )
        assert states.value == state_before_retry
        assert control.path_for(task).read_bytes() == repaired_bytes
        assert host.start_count == 0
        assert host.observe_count == _attempt + 2
        assert business_calls == 0

    with pytest.raises(ExecutorStartUnknownError):
        lifecycle.submit(
            LifecycleRequest(
                task=task,
                kind="resume",
                payload={"parent": 156, "run_id": "run-1"},
                allow_terminal_successor=True,
            )
        )
    final = control.load(task)
    assert final is not None
    assert final["next_generation"] == 2
    assert final["action"]["executor_generation"] == 1


@pytest.mark.parametrize("control_case", ["missing", "corrupt"])
@pytest.mark.parametrize("host_status", ["exited", "unknown", "conflict"])
@pytest.mark.parametrize(
    ("predecessor_kind", "predecessor_status"),
    [("run", "supervision_timeout"), ("resume", "execution_failed")],
)
def test_resume_reconciles_a_receipt_before_admitting_its_successor(
    tmp_path: Path,
    control_case: str,
    host_status: str,
    predecessor_kind: str,
    predecessor_status: str,
) -> None:
    task = _task(tmp_path)
    control = TaskControlStore(tmp_path / "state")
    states = _InMemoryRunState()
    states.value["status"] = predecessor_status
    if predecessor_status == "supervision_timeout":
        states.value.update(
            {
                "terminal_kind": "supervision_timeout",
                "supervision_wait": {
                    "kind": "required_checks",
                    "resume_status": "waiting_checks",
                },
            }
        )
    resume_payload = {"parent": 156, "run_id": "run-1"}
    predecessor_payload = (
        resume_payload if predecessor_kind == "resume" else {"parent": 156}
    )
    claim = control.claim_action(
        task,
        kind=predecessor_kind,
        payload=predecessor_payload,
    )
    assert claim.action is not None
    original_action = claim.action
    control.bind_run(task, claim.action_id or "", "run-1")
    prepare_action_application_receipt(states.value, original_action)
    states.save_run("run-1", states.value)
    if control_case == "missing":
        control.path_for(task).unlink()
    else:
        control.path_for(task).write_text("not json", encoding="utf-8")

    class ReconciliationHost(FakeExecutorHost):
        def __init__(self) -> None:
            super().__init__()
            self.observe_count = 0

        def observe(
            self, spec: ExecutorSpec, store: TaskControlStore
        ) -> HostObservation:
            del store
            self.observe_count += 1
            return HostObservation(
                host_status,  # type: ignore[arg-type]
                spec.generation,
                None,
                False,
                "original Executor ownership is not proven"
                if host_status != "exited"
                else None,
                runner_binding="0" * 16 if host_status == "exited" else None,
            )

    host = ReconciliationHost()
    business_calls = 0

    def execute(_run_id: str) -> dict[str, object]:
        nonlocal business_calls
        business_calls += 1
        return dict(states.value)

    lifecycle = RunLifecycle(
        states=states,  # type: ignore[arg-type]
        control=control,
        host=host,
        task=task,
        preflight=lambda: dict(states.value),
        select_run=lambda _action: (dict(states.value), True),
        initialize_profile=None,
        executor_spec=lambda run_id, action_id, generation: ExecutorSpec(
            task=task,
            action_id=action_id,
            run_id=run_id,
            generation=generation,
        ),
        execute=execute,
    )
    request = LifecycleRequest(
        task=task,
        kind="resume",
        payload=resume_payload,
        allow_terminal_successor=True,
    )

    if host_status == "exited":
        _state, _resumed, receipt = lifecycle.submit(request)
        assert receipt.kind == "resume"
        assert receipt.executor_generation == 2
        assert host.observe_count == 1
        assert host.start_count == 1
        assert business_calls == 1
        repaired = control.load(task)
        assert repaired is not None
        predecessors = [
            entry["action"]
            for entry in repaired["action_history"]
            if entry["action"]["action_id"] == original_action["action_id"]
        ]
        assert len(predecessors) == 1
        assert predecessors[0]["receipt_only_reconciliation"] is True
        return

    for attempt in range(2):
        with pytest.raises(ExecutorStartUnknownError):
            lifecycle.submit(request)
        assert host.observe_count == attempt + 1
        assert host.start_count == 0
        assert business_calls == 0
        current = control.load(task)
        assert current is not None
        assert current["next_generation"] == 2
        assert current["action"]["action_id"] == original_action["action_id"]


@pytest.mark.parametrize(
    ("crash_after", "proof_damage"),
    [
        ("host_proof", None),
        ("predecessor_completion", None),
        ("predecessor_completion", "missing_binding"),
        ("predecessor_completion", "missing_executor"),
    ],
)
def test_resume_finishes_a_receipt_predecessor_after_reconciliation_crash(
    tmp_path: Path,
    crash_after: str,
    proof_damage: str | None,
) -> None:
    task = _task(tmp_path)

    class ProofCrashStore(TaskControlStore):
        injected = False

        def record_reconciled_executor_exit(
            self, *args: object, **kwargs: object
        ) -> dict[str, object]:
            record = super().record_reconciled_executor_exit(*args, **kwargs)  # type: ignore[arg-type]
            if not self.injected and crash_after == "host_proof":
                self.injected = True
                raise SimulatedProcessCrash("after Host exit proof commit")
            return record

        def complete_action_from_application_receipt(
            self, *args: object, **kwargs: object
        ) -> dict[str, object]:
            record = super().complete_action_from_application_receipt(*args, **kwargs)  # type: ignore[arg-type]
            if not self.injected and crash_after == "predecessor_completion":
                self.injected = True
                raise SimulatedProcessCrash("after predecessor completion commit")
            return record

    control = ProofCrashStore(tmp_path / "state")
    states = _InMemoryRunState()
    states.value.update(
        {
            "status": "supervision_timeout",
            "terminal_kind": "supervision_timeout",
            "supervision_wait": {
                "kind": "required_checks",
                "resume_status": "waiting_checks",
            },
        }
    )
    claim = control.claim_action(task, kind="run", payload={"parent": 156})
    assert claim.action is not None
    original_action = claim.action
    control.bind_run(task, claim.action_id or "", "run-1")
    prepare_action_application_receipt(states.value, original_action)
    states.save_run("run-1", states.value)
    control.path_for(task).unlink()

    host_status = "exited"

    class ReconciliationHost(FakeExecutorHost):
        def __init__(self) -> None:
            super().__init__()
            self.observe_count = 0

        def observe(
            self, spec: ExecutorSpec, store: TaskControlStore
        ) -> HostObservation:
            del store
            self.observe_count += 1
            return HostObservation(
                host_status,  # type: ignore[arg-type]
                spec.generation,
                None,
                False,
                (
                    "original Executor ownership is not proven"
                    if host_status != "exited"
                    else None
                ),
                runner_binding="0" * 16 if host_status == "exited" else None,
            )

    host = ReconciliationHost()
    business_calls = 0

    def execute(_run_id: str) -> dict[str, object]:
        nonlocal business_calls
        business_calls += 1
        return dict(states.value)

    lifecycle = RunLifecycle(
        states=states,  # type: ignore[arg-type]
        control=control,
        host=host,
        task=task,
        preflight=lambda: dict(states.value),
        select_run=lambda _action: (dict(states.value), True),
        initialize_profile=None,
        executor_spec=lambda run_id, action_id, generation: ExecutorSpec(
            task=task,
            action_id=action_id,
            run_id=run_id,
            generation=generation,
        ),
        execute=execute,
    )
    request = LifecycleRequest(
        task=task,
        kind="resume",
        payload={"parent": 156, "run_id": "run-1"},
        allow_terminal_successor=True,
    )

    with pytest.raises(SimulatedProcessCrash, match="after .* commit"):
        lifecycle.submit(request)

    after_proof = control.load(task)
    assert after_proof is not None
    assert after_proof["action"]["action_id"] == original_action["action_id"]
    assert after_proof["action"]["status"] == (
        "accepted" if crash_after == "host_proof" else "completed"
    )
    assert after_proof["action"]["receipt_only_reconciliation"] is True
    assert after_proof["executor"]["status"] == "exited"
    assert "reconciliation_required" not in after_proof["executor"]
    assert after_proof["next_generation"] == 2
    assert host.observe_count == 1
    assert host.start_count == 0
    assert business_calls == 0

    if proof_damage is not None:
        damaged = json.loads(control.path_for(task).read_text(encoding="utf-8"))
        if proof_damage == "missing_binding":
            del damaged["executor"]["runner_binding"]
        else:
            damaged["executor"] = None
        control.path_for(task).write_text(json.dumps(damaged), encoding="utf-8")
        host_status = "unknown"

        with pytest.raises(ExecutorStartUnknownError):
            lifecycle.submit(request)
        repaired_bytes = control.path_for(task).read_bytes()
        with pytest.raises(ExecutorStartUnknownError):
            lifecycle.submit(request)
        assert control.path_for(task).read_bytes() == repaired_bytes
        assert host.observe_count == 3
        assert host.start_count == 0
        assert business_calls == 0
        return

    _state, _resumed, receipt = lifecycle.submit(request)

    assert receipt.kind == "resume"
    assert receipt.executor_generation == 2
    assert host.observe_count == 1
    assert host.start_count == 1
    assert business_calls == 1
    repaired = control.load(task)
    assert repaired is not None
    assert repaired["action"]["action_id"] == receipt.action_id
    assert repaired["action"]["kind"] == "resume"
    predecessors = [
        entry["action"]
        for entry in repaired["action_history"]
        if entry["action"]["action_id"] == original_action["action_id"]
    ]
    assert len(predecessors) == 1
    assert predecessors[0]["status"] == "completed"


@pytest.mark.parametrize("control_case", ["missing", "corrupt"])
@pytest.mark.parametrize("action_kind", ["run", "approve"])
def test_receipt_only_control_closes_once_after_exact_host_exit_proof(
    git_repo: Path, tmp_path: Path, control_case: str, action_kind: str
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": ticket()})
    agents = run_agents(git_repo / "agents.json")
    environment = _isolated_environment(tmp_path / f"host-exit-{control_case}")
    interrupted = run_cli(
        git_repo,
        fixture,
        "run",
        "1",
        "--agent-fixture",
        str(agents),
        "--crash-after-save",
        "9",
        extra_env=environment,
    )
    assert interrupted.returncode == 2

    state_root = git_repo / ".agent-run"
    states = StateStore(state_root)
    state = load_only_run_state(git_repo)
    run_id = str(state["run_id"])
    task = TaskKey(git_repo, "example/project", 1)
    control = TaskControlStore(state_root)
    control_path = control.path_for(task)
    original_control = json.loads(control_path.read_text(encoding="utf-8"))
    original_action = original_control["action"]
    if action_kind != "run":
        original_action["kind"] = action_kind
        state["action_application_receipt"]["kind"] = action_kind
        states.save_run(run_id, state)
    payload = dict(original_action["payload"])
    if control_case == "missing":
        control_path.unlink()
    else:
        control_path.write_text("not json", encoding="utf-8")

    class ProofCrashStore(TaskControlStore):
        injected = False

        def record_reconciled_executor_exit(
            self, *args: object, **kwargs: object
        ) -> dict[str, object]:
            record = super().record_reconciled_executor_exit(*args, **kwargs)  # type: ignore[arg-type]
            if not self.injected:
                self.injected = True
                raise SimulatedProcessCrash("after Host exit proof commit")
            return record

    control = ProofCrashStore(state_root)

    class ExitedHost:
        def __init__(self) -> None:
            self.observe_count = 0
            self.ensure_count = 0

        def observe(
            self, spec: ExecutorSpec, store: TaskControlStore
        ) -> HostObservation:
            del store
            self.observe_count += 1
            return HostObservation(
                "exited",
                spec.generation,
                None,
                False,
                runner_binding="0" * 16,
            )

        def inspect(
            self, spec: ExecutorSpec, store: TaskControlStore
        ) -> HostObservation:
            return self.observe(spec, store)

        def ensure(self, *_args: object, **_kwargs: object) -> HostObservation:
            self.ensure_count += 1
            raise AssertionError("receipt reconciliation must not start an Executor")

        def cleanup_startup(self, spec: ExecutorSpec) -> None:
            del spec

    host = ExitedHost()
    business_calls = 0

    def execute(_run_id: str) -> dict[str, object]:
        nonlocal business_calls
        business_calls += 1
        return dict(state)

    lifecycle = RunLifecycle(
        states=states,
        control=control,
        host=host,  # type: ignore[arg-type]
        task=task,
        preflight=lambda: states.load_current_run(run_id),
        select_run=lambda _action: pytest.fail("must keep the original Run"),
        initialize_profile=None,
        executor_spec=lambda selected_run_id, action_id, generation: ExecutorSpec(
            task=task,
            action_id=action_id,
            run_id=selected_run_id,
            generation=generation,
            state_root=state_root.resolve(),
        ),
        execute=execute,
    )
    request = LifecycleRequest(task=task, kind=action_kind, payload=payload)  # type: ignore[arg-type]
    fixture_before = fixture.read_bytes()
    invocation_history_before = list(state["agent_invocation_history"])

    run_path = state_root / "runs" / f"{run_id}.json"
    run_before_proof = run_path.read_bytes()
    with pytest.raises(SimulatedProcessCrash, match="Host exit proof"):
        lifecycle.submit(request)
    proven_control = control.load(task)
    assert proven_control is not None
    assert proven_control["action"]["status"] in {"accepted", "applying"}
    assert proven_control["executor"]["status"] == "exited"
    assert "reconciliation_required" not in proven_control["executor"]
    assert run_path.read_bytes() == run_before_proof
    assert host.observe_count == 1
    assert host.ensure_count == 0

    closed_state, _resumed, receipt = lifecycle.submit(request)

    assert closed_state["status"] == (
        "execution_failed" if action_kind == "run" else state["status"]
    )
    assert receipt.action_id == original_action["action_id"]
    assert receipt.executor_generation == 1
    assert host.observe_count == 1
    assert host.ensure_count == 0
    assert business_calls == 0
    assert fixture.read_bytes() == fixture_before
    if action_kind == "run":
        assert len(closed_state["agent_invocation_history"]) == len(
            invocation_history_before
        )
        assert (
            closed_state["agent_invocation_history"][0]["binding_id"]
            == invocation_history_before[0]["binding_id"]
        )
    else:
        assert closed_state["agent_invocation_history"] == invocation_history_before
    closed_control_bytes = control_path.read_bytes()

    repeated_state, _resumed, repeated_receipt = lifecycle.submit(request)
    assert repeated_state == closed_state
    assert repeated_receipt.action_id == receipt.action_id
    assert control_path.read_bytes() == closed_control_bytes
    assert host.observe_count == 1
    assert host.ensure_count == 0
    assert business_calls == 0

    if action_kind != "run":
        return

    resumed_host = FakeExecutorHost()
    resumed_lifecycle = RunLifecycle(
        states=states,
        control=control,
        host=resumed_host,
        task=task,
        preflight=lambda: states.load_current_run(run_id),
        select_run=lambda _action: (states.load_current_run(run_id) or {}, True),
        initialize_profile=None,
        executor_spec=lambda selected_run_id, action_id, generation: ExecutorSpec(
            task=task,
            action_id=action_id,
            run_id=selected_run_id,
            generation=generation,
            state_root=state_root.resolve(),
        ),
        execute=lambda _run_id: states.load_current_run(run_id) or {},
    )
    _resumed_state, _resumed, resumed_receipt = resumed_lifecycle.submit(
        LifecycleRequest(
            task=task,
            kind="resume",
            payload={"parent": 1, "run_id": run_id},
            allow_terminal_successor=True,
        )
    )
    assert resumed_receipt.executor_generation == 2
    assert resumed_host.start_count == 1


def test_corrupt_control_reconciles_only_from_an_exact_run_receipt(
    tmp_path: Path,
) -> None:
    task = _task(tmp_path)
    control = TaskControlStore(tmp_path / "state")
    payload = {"parent": 156}
    claim = control.claim_action(task, kind="run", payload=payload)
    assert claim.action is not None
    action = claim.action
    receipt = {
        "protocol": 1,
        "action_id": action["action_id"],
        "kind": action["kind"],
        "payload_digest": action["payload_digest"],
        "run_id": "run-1",
        "executor_generation": action["executor_generation"],
    }
    control.path_for(task).write_text("not json", encoding="utf-8")

    repaired = control.reconcile_from_run(
        task,
        {"action_application_receipt": receipt},
        payload=payload,
    )

    assert repaired is not None
    assert repaired["action"]["action_id"] == action["action_id"]
    assert repaired["action"]["payload"] == payload
    assert repaired["action"]["run_id"] == "run-1"
    control.path_for(task).write_text("not json", encoding="utf-8")
    with pytest.raises(ActionReconciliationError):
        control.reconcile_from_run(
            task,
            {"action_application_receipt": receipt},
            payload={"parent": 999},
        )
    with pytest.raises(ActionReconciliationError, match="Run ID"):
        control.reconcile_from_run(
            task,
            {"run_id": "run-other", "action_application_receipt": receipt},
            payload=payload,
        )


def test_concurrent_public_runs_share_one_task_action_and_executor(
    git_repo: Path, tmp_path: Path
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": ticket()})
    agents = run_agents(git_repo / "agents.json")
    started = tmp_path / "agent-started"
    release = tmp_path / "release-agent"
    agent_data = json.loads(agents.read_text(encoding="utf-8"))
    agent_data["invocation_gate"] = {
        "role": "development",
        "started_file": str(started),
        "release_file": str(release),
        "timeout_seconds": 30,
    }
    agents.write_text(json.dumps(agent_data), encoding="utf-8")
    environment = os.environ.copy()
    environment.update(_isolated_environment(tmp_path / "concurrent"))
    source = str(Path(__file__).resolve().parents[1] / "src")
    isolated_home = tmp_path / "home"
    environment.update(
        {
            "HOME": str(isolated_home),
            "XDG_CONFIG_HOME": str(isolated_home / "config"),
            "XDG_DATA_HOME": str(isolated_home / "data"),
            "XDG_STATE_HOME": str(isolated_home / "state"),
            "PATH": os.pathsep.join(
                (str(Path(sys.executable).parent), "/usr/bin", "/bin")
            ),
            "PYTHONPATH": source,
        }
    )
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
        "--json",
    ]

    first = subprocess.Popen(
        command,
        cwd=git_repo,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    deadline = time.monotonic() + 10
    while not started.exists():
        if first.poll() not in {None, 0}:
            stdout, stderr = first.communicate()
            raise AssertionError(f"first run failed early: {stdout}\n{stderr}")
        if time.monotonic() >= deadline:
            first.kill()
            raise AssertionError("first run did not reach the controlled Agent gate")
        os.sched_yield()

    second = subprocess.Popen(
        command,
        cwd=git_repo,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    state_path = next((git_repo / ".agent-run" / "runs").glob("*.json"))
    control_path = next((git_repo / ".agent-run" / "task-control").glob("*.json"))
    state_before_conflict = state_path.read_bytes()
    control_before_conflict = control_path.read_bytes()
    conflict = subprocess.run(
        [*command, "--ticket-review-rounds", "1"],
        cwd=git_repo,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert conflict.returncode == 0, f"{conflict.stdout}\n{conflict.stderr}"
    conflict_output = json.loads(conflict.stdout)
    _assert_public_action_receipt(conflict_output, submission="attached")
    assert state_path.read_bytes() == state_before_conflict
    assert control_path.read_bytes() == control_before_conflict

    release.touch()
    first_stdout, first_stderr = first.communicate(timeout=30)
    second_stdout, second_stderr = second.communicate(timeout=30)

    assert first.returncode == 0, f"{first_stdout}\n{first_stderr}"
    assert second.returncode == 0, f"{second_stdout}\n{second_stderr}"
    first_output = json.loads(first_stdout)
    second_output = json.loads(second_stdout)
    submissions = [
        first_output["action"]["submission"],
        second_output["action"]["submission"],
    ]
    assert sorted(submissions) == ["attached", "started"]
    _assert_public_action_receipt(first_output, submission=submissions[0])
    _assert_public_action_receipt(second_output, submission=submissions[1])
    state = load_only_run_state(git_repo)
    assert state["status"] == "run_approval_pending"
    development_invocations = [
        invocation
        for invocation in state["agent_invocation_history"]
        if invocation.get("role") == "development"
    ]
    assert len(development_invocations) == 1
    controls = list((git_repo / ".agent-run" / "task-control").glob("*.json"))
    assert len(controls) == 1
    control_record = json.loads(controls[0].read_text(encoding="utf-8"))
    assert control_record["action"]["status"] == "completed"
    assert control_record["executor"]["status"] == "exited"


def test_different_parent_runs_reach_agents_without_shared_state_lock(
    git_repo: Path, tmp_path: Path
) -> None:
    def parent_fixture(path: Path, number: int) -> Path:
        return write_fixture(
            path,
            issues={},
            parent={
                "number": number,
                "title": f"Parent {number}",
                "body": "Deliver the standalone Parent request.",
                "sub_issues": [],
                "sub_issue_order_reliable": True,
            },
        )

    fixture_one = parent_fixture(git_repo / "github-one.json", 1)
    fixture_two = parent_fixture(git_repo / "github-two.json", 2)
    agents_one = _parent_only_agents(git_repo / "agents-one.json")
    agents_two = _parent_only_agents(git_repo / "agents-two.json")
    started_one = tmp_path / "parent-one-started"
    started_two = tmp_path / "parent-two-started"
    release_one = tmp_path / "parent-one-release"
    release_two = tmp_path / "parent-two-release"
    for agents, started, release, thread_id in (
        (agents_one, started_one, release_one, "parent-one-developer"),
        (agents_two, started_two, release_two, "parent-two-developer"),
    ):
        data = json.loads(agents.read_text(encoding="utf-8"))
        data["developments"][0]["thread_id"] = thread_id
        data["invocation_gate"] = {
            "role": "development",
            "started_file": str(started),
            "release_file": str(release),
            "timeout_seconds": 10,
        }
        agents.write_text(json.dumps(data), encoding="utf-8")

    environment = os.environ.copy()
    environment.update(_isolated_environment(tmp_path / "different-parents"))
    source = str(Path(__file__).resolve().parents[1] / "src")
    environment["PYTHONPATH"] = source

    def command(parent: int, fixture: Path, agents: Path) -> list[str]:
        return [
            sys.executable,
            "-m",
            "agent_run",
            "run",
            str(parent),
            "--agent-fixture",
            str(agents),
            "--github-fixture",
            str(fixture),
            "--json",
        ]

    processes: list[subprocess.Popen[str]] = []
    try:
        processes.append(
            subprocess.Popen(
                command(1, fixture_one, agents_one),
                cwd=git_repo,
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        )
        deadline = time.monotonic() + 10
        while not started_one.exists():
            if processes[0].poll() is not None:
                stdout, stderr = processes[0].communicate()
                raise AssertionError(f"first run failed early: {stdout}\n{stderr}")
            if time.monotonic() >= deadline:
                raise AssertionError("first Parent did not reach the Agent barrier")
            os.sched_yield()

        processes.append(
            subprocess.Popen(
                command(2, fixture_two, agents_two),
                cwd=git_repo,
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        )
        deadline = time.monotonic() + 10
        while not started_two.exists():
            if processes[1].poll() is not None:
                stdout, stderr = processes[1].communicate()
                raise AssertionError(f"second run failed early: {stdout}\n{stderr}")
            if time.monotonic() >= deadline:
                raise AssertionError(
                    "different Parent was blocked before reaching the Agent barrier"
                )
            os.sched_yield()

        release_one.touch()
        release_two.touch()
        results = [process.communicate(timeout=30) for process in processes]
        for process, (stdout, stderr) in zip(processes, results):
            assert process.returncode == 0, f"{stdout}\n{stderr}"
        states = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in sorted((git_repo / ".agent-run" / "runs").glob("*.json"))
        ]
        assert len(states) == 2
        assert {state["parent"]["number"] for state in states} == {1, 2}
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.communicate()


def test_repeated_run_reuses_the_completed_action_after_receipt_loss(
    git_repo: Path, tmp_path: Path
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": ticket()})
    agents = run_agents(git_repo / "agents.json")
    environment = _isolated_environment(tmp_path / "receipt")

    first = run_cli(
        git_repo,
        fixture,
        "run",
        "1",
        "--agent-fixture",
        str(agents),
        extra_env=environment,
    )
    first_output = stdout_json(first)
    state_path = next((git_repo / ".agent-run" / "runs").glob("*.json"))
    control_path = next((git_repo / ".agent-run" / "task-control").glob("*.json"))
    fixture_before = json.loads(fixture.read_text(encoding="utf-8"))
    state_before = state_path.read_bytes()
    control_before = control_path.read_bytes()

    second = run_cli(
        git_repo,
        fixture,
        "run",
        "1",
        "--agent-fixture",
        str(agents),
        extra_env=environment,
    )
    second_output = stdout_json(second)

    assert first.returncode == second.returncode == 0
    assert first_output["run_id"] == second_output["run_id"]
    _assert_public_action_receipt(first_output, submission="started")
    _assert_public_action_receipt(second_output, submission="attached")
    assert json.loads(fixture.read_text(encoding="utf-8")) == fixture_before
    assert state_path.read_bytes() == state_before
    assert control_path.read_bytes() == control_before
    state = load_only_run_state(git_repo)
    assert (
        sum(
            invocation.get("role") == "development"
            for invocation in state["agent_invocation_history"]
        )
        == 1
    )


def test_executor_caller_can_read_its_action_after_a_successor_is_claimed(
    tmp_path: Path,
) -> None:
    task = _task(tmp_path)
    control = TaskControlStore(tmp_path / "state")
    first = control.claim_action(task, kind="run", payload={"parent": 156})
    assert first.action_id is not None
    control.bind_run(task, first.action_id, "run-1")
    states = _InMemoryRunState()

    class ReplacingHost(FakeExecutorHost):
        def ensure(self, *args: object, **kwargs: object) -> object:
            observation = super().ensure(*args, **kwargs)  # type: ignore[arg-type]
            control.claim_action(task, kind="run", payload={"parent": 157})
            return observation

    lifecycle = RunLifecycle(
        states=states,  # type: ignore[arg-type]
        control=control,
        host=ReplacingHost(),  # type: ignore[arg-type]
        task=task,
        preflight=lambda: dict(states.value),
        select_run=lambda _action: pytest.fail("the action is already bound"),
        initialize_profile=None,
        executor_spec=lambda run_id, action_id, generation: ExecutorSpec(
            task=task,
            action_id=action_id,
            run_id=run_id,
            generation=generation,
        ),
        execute=lambda _run_id: dict(states.value),
        sleep=lambda _seconds: None,
    )

    state, _resumed, receipt = lifecycle.submit(
        LifecycleRequest(task=task, kind="run", payload={"parent": 156})
    )

    assert state["run_id"] == "run-1"
    assert receipt.action_id == first.action_id
    assert receipt.status == "completed"
    assert receipt.attached is True
    current = control.load(task)
    assert current is not None
    assert current["action"]["action_id"] != first.action_id
    historical = control.snapshot(task, first.action_id)
    assert historical is not None
    assert historical["action"]["action_id"] == first.action_id


def test_run_can_replace_a_terminal_receipt_with_a_successor_action(
    tmp_path: Path,
) -> None:
    task = _task(tmp_path)
    control = TaskControlStore(tmp_path / "state")
    first = control.claim_action(task, kind="run", payload={"parent": 156})
    assert first.action_id is not None
    control.bind_run(task, first.action_id, "run-1")
    control.begin_executor(task, action_id=first.action_id, run_id="run-1")
    control.finish_executor(
        task,
        action_id=first.action_id,
        generation=1,
        result_status="run_approval_pending",
    )
    state = {"run_id": "run-1", "status": "run_approval_pending"}
    prepare_action_application_receipt(state, first.action or {})
    states = _InMemoryRunState()
    states.value = state
    successor = control.claim_action(task, kind="run", payload={"parent": 156})
    assert successor.action_id is not None
    assert successor.action_id != first.action_id

    lifecycle = RunLifecycle(
        states=states,  # type: ignore[arg-type]
        control=control,
        host=FakeExecutorHost(),
        task=task,
        preflight=lambda: dict(states.value),
        select_run=lambda _action: (dict(states.value), True),
        initialize_profile=None,
        executor_spec=lambda run_id, action_id, generation: ExecutorSpec(
            task=task,
            action_id=action_id,
            run_id=run_id,
            generation=generation,
        ),
        execute=lambda _run_id: dict(states.value),
        sleep=lambda _seconds: None,
    )

    result_state, _resumed, receipt = lifecycle.submit(
        LifecycleRequest(task=task, kind="run", payload={"parent": 156})
    )

    assert result_state["run_id"] == "run-1"
    assert receipt.action_id == successor.action_id
    assert receipt.status == "completed"
    assert (
        result_state["action_application_receipt"]["action_id"] == successor.action_id
    )
    historical = control.snapshot(task, first.action_id)
    assert historical is not None
    assert historical["action"]["status"] == "completed"


@pytest.mark.parametrize(
    (
        "crash_after_save",
        "expected_development_invocations",
        "initial_invocation_status",
    ),
    [(3, 0, None), (9, 1, "running"), (11, 1, "completed")],
)
def test_repeated_run_reconciles_proven_executor_crash_without_replay(
    git_repo: Path,
    tmp_path: Path,
    crash_after_save: int,
    expected_development_invocations: int,
    initial_invocation_status: str | None,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": ticket()})
    agents = run_agents(git_repo / "agents.json")
    environment = _isolated_environment(tmp_path / "receipt-crash")

    interrupted = run_cli(
        git_repo,
        fixture,
        "run",
        "1",
        "--agent-fixture",
        str(agents),
        "--crash-after-save",
        str(crash_after_save),
        extra_env=environment,
    )
    assert interrupted.returncode == 2
    interrupted_state = load_only_run_state(git_repo)
    receipt = interrupted_state.get("action_application_receipt")
    assert isinstance(receipt, dict)
    assert isinstance(receipt.get("action_id"), str)
    state_path = next((git_repo / ".agent-run" / "runs").glob("*.json"))
    control_path = next((git_repo / ".agent-run" / "task-control").glob("*.json"))
    interrupted_control = json.loads(control_path.read_text(encoding="utf-8"))
    interrupted_action = interrupted_control["action"]
    interrupted_fixture_bytes = fixture.read_bytes()
    interrupted_development_count = sum(
        invocation.get("role") == "development"
        for invocation in interrupted_state["agent_invocation_history"]
    )
    assert interrupted_development_count == expected_development_invocations
    assert interrupted_control["action"]["status"] == (
        "completed" if expected_development_invocations == 1 else "applying"
    )
    assert interrupted_control["executor"]["status"] == "absent"
    if initial_invocation_status is not None:
        assert (
            interrupted_state["active_agent_invocation"]["status"]
            == initial_invocation_status
        )
    if initial_invocation_status == "running":
        inspected = run_cli(
            git_repo,
            fixture,
            "status",
            str(interrupted_state["run_id"]),
            "--json",
            extra_env=environment,
        )
        assert inspected.returncode == 0
        inspected_json = stdout_json(inspected)
        assert inspected_json["executor_control"]["activity"] == "not_running"
        assert inspected_json["progress"]["current_agent"]["is_active"] is False

    recovered = run_cli(
        git_repo,
        fixture,
        "run",
        "1",
        "--agent-fixture",
        str(agents),
        extra_env=environment,
    )
    assert recovered.returncode == 2, f"{recovered.stdout}\n{recovered.stderr}"
    recovered_diagnostics = json.loads(recovered.stdout)["diagnostics"]
    assert len(recovered_diagnostics) == 1
    assert recovered_diagnostics[0]["code"] == "session_interrupted"
    assert recovered_diagnostics[0]["message"] == (
        "Executor Session 已退出；保留现场并等待显式 Resume"
    )
    operator_gate = recovered_diagnostics[0].get("operator_gate")
    assert isinstance(operator_gate, dict)
    assert operator_gate["work_subject"] == "ticket:3"
    assert operator_gate["action_kind"] == "execution_failure"
    assert operator_gate["reason"] == "session_interrupted"
    recovered_state = load_only_run_state(git_repo)
    require_current_run_state(recovered_state)
    assert recovered_state["status"] == "execution_failed"
    assert recovered_state["terminal_kind"] == "execution_failed"
    if initial_invocation_status == "running":
        assert recovered_state["active_agent_invocation"]["status"] == "failed"
        assert (
            recovered_state["active_agent_invocation"]["error"]
            == "session_interrupted"
        )
    elif initial_invocation_status == "completed":
        assert recovered_state["active_agent_invocation"]["status"] == "completed"
    assert (
        sum(
            invocation.get("role") == "development"
            for invocation in recovered_state["agent_invocation_history"]
        )
        == interrupted_development_count
    )
    assert fixture.read_bytes() == interrupted_fixture_bytes
    recovered_control = json.loads(control_path.read_text(encoding="utf-8"))
    assert recovered_control["action"]["action_id"] == interrupted_action["action_id"]
    assert (
        recovered_control["action"]["executor_generation"]
        == interrupted_action["executor_generation"]
    )
    assert (
        recovered_control["executor"]["generation"]
        == interrupted_control["executor"]["generation"]
    )
    assert recovered_control["executor"]["status"] == "exited"
    assert recovered_control["executor"]["failure"] == "session_interrupted"
    assert recovered_control["action"]["status"] == (
        "completed" if expected_development_invocations == 1 else "failed"
    )
    if crash_after_save == 3:
        resumed = run_cli(
            git_repo,
            fixture,
            "resume",
            str(recovered_state["run_id"]),
            "--agent-fixture",
            str(agents),
            extra_env=environment,
        )
        assert resumed.returncode == 0, f"{resumed.stdout}\n{resumed.stderr}"
        resumed_control = json.loads(control_path.read_text(encoding="utf-8"))
        assert resumed_control["action"]["action_id"] != interrupted_action["action_id"]
        assert resumed_control["action"]["executor_generation"] == 2
        assert resumed_control["executor"]["generation"] == 2


@pytest.mark.parametrize(
    ("crash_after_save", "expected_invocation_status"),
    [(3, None), (9, "running"), (11, "completed")],
)
def test_session_closeout_retries_only_the_control_commit(
    git_repo: Path,
    tmp_path: Path,
    crash_after_save: int,
    expected_invocation_status: str | None,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": ticket()})
    agents = run_agents(git_repo / "agents.json")
    environment = _isolated_environment(tmp_path / "session-closeout")
    interrupted = run_cli(
        git_repo,
        fixture,
        "run",
        "1",
        "--agent-fixture",
        str(agents),
        "--crash-after-save",
        str(crash_after_save),
        extra_env=environment,
    )
    assert interrupted.returncode == 2

    state_root = git_repo / ".agent-run"
    states = StateStore(state_root)
    initial = load_only_run_state(git_repo)
    run_id = str(initial["run_id"])
    task = TaskKey(git_repo, "example/project", 1)

    class CrashAfterRunSaveStore(TaskControlStore):
        injected = False

        def fail_session_from_application_receipt(
            self, *args: object, **kwargs: object
        ) -> dict[str, object]:
            persist = kwargs["persist_run_failure"]
            assert callable(persist)

            def persist_then_crash() -> None:
                persist()
                if not self.injected:
                    self.injected = True
                    raise SimulatedProcessCrash(
                        "after Run failure save before Task Control close"
                    )

            kwargs["persist_run_failure"] = persist_then_crash
            return super().fail_session_from_application_receipt(  # type: ignore[arg-type]
                *args, **kwargs
            )

    control = CrashAfterRunSaveStore(state_root)
    control_path = control.path_for(task)
    control_before = control_path.read_bytes()
    original_control = json.loads(control_before)
    action = original_control["action"]
    fixture_before = fixture.read_bytes()
    host = FakeExecutorHost()
    business_calls = 0

    def execute(_run_id: str) -> dict[str, object]:
        nonlocal business_calls
        business_calls += 1
        return states.load_current_run(run_id) or {}

    lifecycle = RunLifecycle(
        states=states,
        control=control,
        host=host,
        task=task,
        preflight=lambda: states.load_current_run(run_id),
        select_run=lambda _action: pytest.fail("must retain the original Run"),
        initialize_profile=None,
        executor_spec=lambda selected_run_id, action_id, generation: ExecutorSpec(
            task=task,
            action_id=action_id,
            run_id=selected_run_id,
            generation=generation,
            state_root=state_root.resolve(),
        ),
        execute=execute,
    )
    request = LifecycleRequest(
        task=task,
        kind=str(action["kind"]),  # type: ignore[arg-type]
        payload=dict(action["payload"]),
    )

    with pytest.raises(SimulatedProcessCrash, match="after Run failure save"):
        lifecycle.submit(request)
    after_run_save = states.load_current_run(run_id)
    assert after_run_save is not None
    require_current_run_state(after_run_save)
    gate = after_run_save["diagnostics"][0]["operator_gate"]
    assert gate["phase"] != "execution_failed"
    assert session_interruption_is_persisted(after_run_save)
    successor_state = dict(after_run_save)
    successor_state["action_application_receipt"] = {
        **after_run_save["action_application_receipt"],
        "action_id": "successor-action",
        "executor_generation": action["executor_generation"] + 1,
    }
    assert not session_interruption_is_persisted(successor_state)
    control_in_window = json.loads(control_path.read_text(encoding="utf-8"))
    assert control_in_window["action"]["action_id"] == action["action_id"]
    assert control_in_window["action"]["status"] == action["status"]
    assert control_in_window["executor"]["status"] == "absent"
    assert (
        control_in_window["executor"]["generation"]
        == action["executor_generation"]
    )
    if expected_invocation_status is not None:
        assert initial["active_agent_invocation"]["status"] == (
            expected_invocation_status
        )

    closed, _resumed, receipt = lifecycle.submit(request)
    require_current_run_state(closed)
    assert closed["diagnostics"][0]["operator_gate"] == gate
    assert receipt.action_id == action["action_id"]
    assert receipt.executor_generation == action["executor_generation"]
    closed_control = json.loads(control_path.read_text(encoding="utf-8"))
    assert closed_control["action"]["action_id"] == action["action_id"]
    assert closed_control["executor"]["generation"] == action["executor_generation"]
    assert closed_control["executor"]["status"] == "exited"
    assert host.start_count == 0
    assert business_calls == 0
    assert fixture.read_bytes() == fixture_before

    resume_host = FakeExecutorHost()
    resume_lifecycle = RunLifecycle(
        states=states,
        control=control,
        host=resume_host,
        task=task,
        preflight=lambda: states.load_current_run(run_id),
        select_run=lambda _action: (states.load_current_run(run_id) or {}, True),
        initialize_profile=None,
        executor_spec=lambda selected_run_id, action_id, generation: ExecutorSpec(
            task=task,
            action_id=action_id,
            run_id=selected_run_id,
            generation=generation,
            state_root=state_root.resolve(),
        ),
        execute=lambda _run_id: states.load_current_run(run_id) or {},
    )
    _resumed_state, _resumed, resume_receipt = resume_lifecycle.submit(
        LifecycleRequest(
            task=task,
            kind="resume",
            payload={"parent": 1, "run_id": run_id},
            allow_terminal_successor=True,
        )
    )
    assert resume_receipt.executor_generation == 2
    assert resume_host.start_count == 1


def test_repeated_run_fails_closed_after_action_acceptance_before_run_receipt(
    git_repo: Path, tmp_path: Path
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": ticket()})
    agents = run_agents(git_repo / "agents.json")
    environment = _isolated_environment(tmp_path / "acceptance-crash")

    interrupted = run_cli(
        git_repo,
        fixture,
        "run",
        "1",
        "--agent-fixture",
        str(agents),
        "--crash-after-save",
        "1",
        extra_env=environment,
    )
    assert interrupted.returncode == 2
    interrupted_state = load_only_run_state(git_repo)
    assert "action_application_receipt" not in interrupted_state
    state_path = next((git_repo / ".agent-run" / "runs").glob("*.json"))
    interrupted_state_bytes = state_path.read_bytes()
    control_path = next((git_repo / ".agent-run" / "task-control").glob("*.json"))
    interrupted_control = json.loads(control_path.read_text(encoding="utf-8"))
    interrupted_action = interrupted_control["action"]

    recovered = run_cli(
        git_repo,
        fixture,
        "run",
        "1",
        "--agent-fixture",
        str(agents),
        extra_env=environment,
    )
    assert recovered.returncode == 2, f"{recovered.stdout}\n{recovered.stderr}"
    assert json.loads(recovered.stdout)["diagnostics"][0]["code"] == "task_control"
    assert state_path.read_bytes() == interrupted_state_bytes
    recovered_control = json.loads(control_path.read_text(encoding="utf-8"))
    assert recovered_control["action"]["action_id"] == interrupted_action["action_id"]
    assert (
        recovered_control["action"]["executor_generation"]
        == interrupted_action["executor_generation"]
    )
    assert (
        recovered_control["executor"]["generation"]
        == interrupted_control["executor"]["generation"]
    )


@pytest.mark.parametrize("control_case", ["missing", "corrupt"])
def test_terminal_run_receipt_still_requires_host_exit_proof(
    git_repo: Path, tmp_path: Path, control_case: str
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": ticket()})
    agents = run_agents(git_repo / "agents.json")
    environment = _isolated_environment(tmp_path / "terminal-receipt")
    first = run_cli(
        git_repo,
        fixture,
        "run",
        "1",
        "--agent-fixture",
        str(agents),
        extra_env=environment,
    )
    first_output = stdout_json(first)
    state_path = next((git_repo / ".agent-run" / "runs").glob("*.json"))
    control_path = next((git_repo / ".agent-run" / "task-control").glob("*.json"))
    state_before = state_path.read_bytes()
    fixture_before = fixture.read_bytes()
    if control_case == "missing":
        control_path.unlink()
    else:
        control_path.write_text("not json", encoding="utf-8")

    repeated = run_cli(
        git_repo,
        fixture,
        "run",
        "1",
        "--agent-fixture",
        str(agents),
        extra_env=environment,
    )
    repeated_output = stdout_json(repeated)

    assert repeated.returncode == 2, f"{repeated.stdout}\n{repeated.stderr}"
    assert repeated_output["diagnostics"][0]["code"] == "executor_start_unknown"
    _assert_public_action_receipt(first_output, submission="started")
    assert state_path.read_bytes() == state_before
    assert fixture.read_bytes() == fixture_before
    repaired_controls = list((git_repo / ".agent-run" / "task-control").glob("*.json"))
    assert len(repaired_controls) == 1
    repaired_control = json.loads(repaired_controls[0].read_text(encoding="utf-8"))
    repaired_action = repaired_control["action"]
    receipt = json.loads(state_path.read_text(encoding="utf-8"))[
        "action_application_receipt"
    ]
    assert repaired_control["run_id"] == receipt["run_id"] == first_output["run_id"]
    assert repaired_action["status"] == "accepted"
    assert repaired_control["executor"]["reconciliation_required"] is True
    assert repaired_action["application_observed"] is True
    assert all(
        repaired_action[key] == receipt[key]
        for key in ("action_id", "kind", "payload_digest", "run_id")
    )
    repaired_before = repaired_controls[0].read_bytes()
    retried = run_cli(
        git_repo, fixture, "run", "1", "--agent-fixture", str(agents),
        extra_env=environment,
    )
    assert retried.returncode == 2
    assert stdout_json(retried)["diagnostics"][0]["code"] == "executor_start_unknown"
    assert repaired_controls[0].read_bytes() == repaired_before
    assert state_path.read_bytes() == state_before
    assert fixture.read_bytes() == fixture_before

    repaired_controls[0].unlink()
    blocked_state = state_path.read_bytes()
    blocked_fixture = fixture.read_bytes()
    blocked = run_cli(
        git_repo,
        fixture,
        "resume",
        first_output["run_id"],
        "--agent-fixture",
        str(agents),
        extra_env=environment,
    )

    assert blocked.returncode == 2, f"{blocked.stdout}\n{blocked.stderr}"
    assert stdout_json(blocked)["diagnostics"][0]["code"] == "task_control"
    assert state_path.read_bytes() == blocked_state
    assert fixture.read_bytes() == blocked_fixture
    assert not repaired_controls[0].exists()


@pytest.mark.parametrize("control_case", ["missing", "corrupt"])
@pytest.mark.parametrize("host_status", ["exited", "unknown", "conflict"])
def test_public_run_reconciles_receipt_only_control_before_replay(
    git_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    control_case: str,
    host_status: str,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": ticket()})
    agents = run_agents(git_repo / "agents.json")
    environment = _isolated_environment(tmp_path / "public-reconciliation")
    first = run_cli(
        git_repo,
        fixture,
        "run",
        "1",
        "--agent-fixture",
        str(agents),
        "--crash-after-save",
        "9",
        extra_env=environment,
    )
    assert first.returncode == 2, f"{first.stdout}\n{first.stderr}"

    state_root = git_repo / ".agent-run"
    state_path = next((state_root / "runs").glob("*.json"))
    control_path = next((state_root / "task-control").glob("*.json"))
    original_control = json.loads(control_path.read_text(encoding="utf-8"))
    original_action = original_control["action"]
    original_generation = original_action["executor_generation"]
    state_before = state_path.read_bytes()
    fixture_before = fixture.read_bytes()
    agents_before = agents.read_bytes()
    invocation_history_before = json.loads(state_before)["agent_invocation_history"]
    if control_case == "missing":
        control_path.unlink()
    else:
        control_path.write_text("not json", encoding="utf-8")

    calls = {"readiness": 0, "observe": 0, "ensure": 0, "prepare": 0}

    class UsageLease:
        def fileno(self) -> int:
            return 1

    class ReconciliationHost:
        def check_readiness(self) -> None:
            calls["readiness"] += 1

        def prepare_environment(self, _arguments: object) -> None:
            calls["prepare"] += 1

        def observe(
            self, spec: ExecutorSpec, _store: TaskControlStore
        ) -> HostObservation:
            calls["observe"] += 1
            assert spec.generation == original_generation
            return HostObservation(
                host_status,  # type: ignore[arg-type]
                original_generation,
                None,
                False,
                "original Executor ownership is not proven"
                if host_status != "exited"
                else None,
                runner_binding="0" * 16 if host_status == "exited" else None,
            )

        def ensure(self, *_args: object, **_kwargs: object) -> HostObservation:
            calls["ensure"] += 1
            raise AssertionError("reconciliation must not start another Executor")

        def cleanup_startup(self, _spec: ExecutorSpec) -> None:
            pass

    host = ReconciliationHost()
    monkeypatch.chdir(git_repo)
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(cli, "_running_active_runner", lambda: True)
    monkeypatch.setattr(
        cli, "runner_usage_lease", lambda _path: nullcontext(UsageLease())
    )
    monkeypatch.setattr(cli, "SystemdUserExecutorHost", lambda **_options: host)
    monkeypatch.setattr(
        cli,
        "GhGitHubReader",
        lambda _repo, *, working_directory: cli.FixtureGitHubReader(fixture),
    )

    control_after_first: bytes | None = None
    state_after_first: bytes | None = None
    for attempt in range(2):
        capsys.readouterr()
        return_code = cli.main(
            ["run", "1", "--repo", "example/project", "--json"]
        )
        output = json.loads(capsys.readouterr().out)
        if host_status == "exited":
            assert return_code == 2
            if attempt == 0:
                assert output["status"] == "execution_failed"
                _assert_public_action_receipt(output, submission="attached")
            else:
                assert output["result"] == "rejected"
                assert output["status"] == "execution_failed"
        else:
            assert return_code == 2
            assert output["diagnostics"][0]["code"] == "executor_start_unknown"
        assert fixture.read_bytes() == fixture_before
        assert agents.read_bytes() == agents_before
        if attempt == 0:
            control_after_first = control_path.read_bytes()
            state_after_first = state_path.read_bytes()
        else:
            assert control_path.read_bytes() == control_after_first
            assert state_path.read_bytes() == state_after_first

    recovered = json.loads(control_path.read_text(encoding="utf-8"))
    assert recovered["action"]["action_id"] == original_action["action_id"]
    assert recovered["action"]["executor_generation"] == original_generation
    assert calls["ensure"] == 0
    assert calls["prepare"] == 0
    assert calls["observe"] == (1 if host_status == "exited" else 2)
    current_state = json.loads(state_path.read_text(encoding="utf-8"))
    if host_status == "exited":
        assert current_state["status"] == "execution_failed"
        assert len(current_state["agent_invocation_history"]) == len(
            invocation_history_before
        )
    else:
        assert state_path.read_bytes() == state_before


def test_status_and_history_do_not_touch_run_or_task_control(
    git_repo: Path, tmp_path: Path
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": ticket()})
    agents = run_agents(git_repo / "agents.json")
    environment = _isolated_environment(tmp_path / "read-only")
    first = run_cli(
        git_repo,
        fixture,
        "run",
        "1",
        "--agent-fixture",
        str(agents),
        extra_env=environment,
    )
    assert first.returncode == 0, f"{first.stdout}\n{first.stderr}"
    run_id = stdout_json(first)["run_id"]
    run_path = next((git_repo / ".agent-run" / "runs").glob("*.json"))
    control_path = next((git_repo / ".agent-run" / "task-control").glob("*.json"))
    run_before = run_path.read_bytes()
    control_before = control_path.read_bytes()

    for command in ("status", "history"):
        inspected = run_cli(
            git_repo,
            fixture,
            command,
            str(run_id),
            "--json",
            extra_env=environment,
        )
        assert inspected.returncode == 0, f"{inspected.stdout}\n{inspected.stderr}"
        serialized = json.dumps(stdout_json(inspected), ensure_ascii=False)
        assert "binding_token" not in serialized
        assert "process_start_token" not in serialized
        assert run_path.read_bytes() == run_before
        assert control_path.read_bytes() == control_before


@pytest.mark.parametrize(
    ("control_case", "reason"),
    [
        ("missing", "task_control_missing"),
        ("corrupt", "task_control_invalid"),
        ("inconsistent", "task_control_invalid"),
        ("inconsistent_generation", "task_control_invalid"),
    ],
)
def test_status_and_history_mark_unavailable_task_control_unknown_without_writes(
    git_repo: Path, tmp_path: Path, control_case: str, reason: str
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": ticket()})
    agents = run_agents(git_repo / "agents.json")
    environment = _isolated_environment(tmp_path / "read-only-corrupt-control")
    first = run_cli(
        git_repo,
        fixture,
        "run",
        "1",
        "--agent-fixture",
        str(agents),
        extra_env=environment,
    )
    assert first.returncode == 0, f"{first.stdout}\n{first.stderr}"
    run_id = stdout_json(first)["run_id"]
    control_path = next((git_repo / ".agent-run" / "task-control").glob("*.json"))
    if control_case == "missing":
        control_path.unlink()
    elif control_case == "corrupt":
        control_path.write_bytes(b'{"authorization":"Bearer secret-token"')
    elif control_case == "inconsistent":
        control = json.loads(control_path.read_text(encoding="utf-8"))
        control["run_id"] = "run-other"
        control_path.write_text(json.dumps(control), encoding="utf-8")
    else:
        control = json.loads(control_path.read_text(encoding="utf-8"))
        control["executor"]["generation"] += 1
        control_path.write_text(json.dumps(control), encoding="utf-8")
    persistent_roots = [git_repo, Path(environment["XDG_STATE_HOME"])]
    before = {str(root): _file_snapshot(root) for root in persistent_roots}

    for command in ("status", "history"):
        machine = run_cli(
            git_repo,
            fixture,
            command,
            str(run_id),
            "--json",
            extra_env=environment,
        )
        assert machine.returncode == 0, f"{machine.stdout}\n{machine.stderr}"
        machine_output = stdout_json(machine)
        executor_control = machine_output["executor_control"]
        assert executor_control == {
            "activity": "unknown",
            "reason": reason,
        }
        current_agent = machine_output.get("progress", {}).get("current_agent")
        if current_agent is not None:
            assert current_agent["is_active"] is False
        serialized = json.dumps(machine_output, ensure_ascii=False)
        assert "binding_token" not in serialized
        assert "process_start_token" not in serialized
        human = run_cli(
            git_repo,
            fixture,
            command,
            str(run_id),
            extra_env=environment,
        )
        assert human.returncode == 0, f"{human.stdout}\n{human.stderr}"
        assert "Agent 活跃状态: 无法确认" in human.stdout
        assert "secret-token" not in human.stdout
        assert {str(root): _file_snapshot(root) for root in persistent_roots} == before


@pytest.mark.parametrize(
    ("host_status", "expected_activity"),
    [
        ("running", "running"),
        ("absent", "not_running"),
        ("exited", "not_running"),
        ("starting", "unknown"),
        ("unknown", "unknown"),
        ("conflict", "unknown"),
        ("unavailable", "unknown"),
        ("synthetic", "unknown"),
        ("control_starting_host_running", "unknown"),
        ("running_without_handshake", "unknown"),
        ("running_pid_conflict", "unknown"),
    ],
)
def test_status_and_history_project_only_read_only_host_proof(
    git_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    host_status: str,
    expected_activity: str,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": ticket()})
    agents = run_agents(git_repo / "agents.json")
    environment = _isolated_environment(tmp_path / f"projection-{host_status}")
    first = run_cli(
        git_repo,
        fixture,
        "run",
        "1",
        "--agent-fixture",
        str(agents),
        extra_env=environment,
    )
    assert first.returncode == 0, f"{first.stdout}\n{first.stderr}"
    run_id = stdout_json(first)["run_id"]
    control_path = next((git_repo / ".agent-run" / "task-control").glob("*.json"))
    control = json.loads(control_path.read_text(encoding="utf-8"))
    control["executor"]["status"] = (
        "absent"
        if host_status == "synthetic"
        else "starting"
        if host_status == "control_starting_host_running"
        else "running"
    )
    control["executor"]["pid"] = 456 if host_status == "running_pid_conflict" else 123
    if host_status == "running_without_handshake":
        control["executor"]["handshake_at"] = None
    if host_status == "synthetic":
        control["executor"]["reconciliation_required"] = True
        control["executor"]["binding_token"] = "reconciliation-required"
        control["executor"]["pid"] = None
        control["executor"]["process_start_token"] = None
        control["executor"]["handshake_at"] = None
    control_path.write_text(json.dumps(control), encoding="utf-8")

    observe_calls = 0

    def observe(
        spec: ExecutorSpec,
        store: TaskControlStore,
        **_kwargs: object,
    ) -> HostObservation:
        nonlocal observe_calls
        del store
        observe_calls += 1
        if host_status == "synthetic":
            raise AssertionError("synthetic ownership must remain unknown")
        if host_status == "unavailable":
            raise OSError("systemd unavailable")
        observed_status = (
            "running"
            if host_status
            in {
                "control_starting_host_running",
                "running_without_handshake",
                "running_pid_conflict",
            }
            else host_status
        )
        return HostObservation(
            observed_status,  # type: ignore[arg-type]
            None if observed_status == "absent" else spec.generation,
            123 if observed_status == "running" else None,
            observed_status == "running",
        )

    monkeypatch.setattr(cli, "observe_systemd_executor", observe)
    monkeypatch.chdir(git_repo)
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    persistent_roots = [git_repo, Path(environment["XDG_STATE_HOME"])]
    before = {str(root): _file_snapshot(root) for root in persistent_roots}

    for command in ("status", "history"):
        assert cli.main([command, str(run_id), "--json"]) == 0
        output = json.loads(capsys.readouterr().out)
        assert output["executor_control"]["activity"] == expected_activity
        assert {str(root): _file_snapshot(root) for root in persistent_roots} == before

    assert observe_calls == (0 if host_status == "synthetic" else 2)


def test_status_and_history_bypass_an_active_action_without_persistent_writes(
    git_repo: Path, tmp_path: Path
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": ticket()})
    agents = run_agents(git_repo / "agents.json")
    started = tmp_path / "read-only-active-started"
    release = tmp_path / "read-only-active-release"
    agent_data = json.loads(agents.read_text(encoding="utf-8"))
    agent_data["invocation_gate"] = {
        "role": "development",
        "started_file": str(started),
        "release_file": str(release),
        "timeout_seconds": 30,
    }
    agents.write_text(json.dumps(agent_data), encoding="utf-8")
    environment = _isolated_environment(tmp_path / "read-only-active")
    source = str(Path(__file__).resolve().parents[1] / "src")
    environment["PYTHONPATH"] = source
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
        "--json",
    ]
    first = subprocess.Popen(
        command,
        cwd=git_repo,
        env={**os.environ, **environment},
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 10
        while not started.exists():
            if first.poll() is not None:
                stdout, stderr = first.communicate()
                raise AssertionError(f"active run exited early: {stdout}\n{stderr}")
            if time.monotonic() >= deadline:
                raise AssertionError(
                    "active run did not reach the controlled Agent gate"
                )
            os.sched_yield()

        run_id = load_only_run_state(git_repo)["run_id"]
        persistent_roots = [git_repo, tmp_path / "read-only-active"]
        before = {str(root): _file_snapshot(root) for root in persistent_roots}
        for command_name in ("status", "history"):
            inspected = run_cli(
                git_repo,
                fixture,
                command_name,
                str(run_id),
                "--json",
                extra_env=environment,
            )
            assert inspected.returncode == 0, f"{inspected.stdout}\n{inspected.stderr}"
            assert stdout_json(inspected)["run_id"] == run_id
            assert {
                str(root): _file_snapshot(root) for root in persistent_roots
            } == before
        rejected = run_cli(
            git_repo,
            fixture,
            "resume",
            str(run_id),
            extra_env=environment,
        )
        assert rejected.returncode == 2
        assert stdout_json(rejected)["diagnostics"][0]["code"] == "action_busy"
        assert {str(root): _file_snapshot(root) for root in persistent_roots} == before
    finally:
        release.touch()
        stdout, stderr = first.communicate(timeout=30)
    assert first.returncode == 0, f"{stdout}\n{stderr}"


def test_old_run_without_lifecycle_protocol_is_read_only_but_not_mutable(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"2": ticket()})
    started = seed_run(git_repo, fixture, "1")
    run_id = stdout_json(started)["run_id"]
    state_path = next((git_repo / ".agent-run" / "runs").glob("*.json"))
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state.pop("lifecycle_action_protocol")
    state_path.write_text(json.dumps(state), encoding="utf-8")
    before = state_path.read_bytes()
    persistent_before = _file_snapshot(git_repo)

    for command in ("status", "history"):
        inspected = run_cli(git_repo, fixture, command, run_id, "--json")
        assert inspected.returncode == 0, f"{inspected.stdout}\n{inspected.stderr}"
        assert state_path.read_bytes() == before

    rejected = run_cli(git_repo, fixture, "resume", run_id)
    assert rejected.returncode == 2
    assert stdout_json(rejected)["status"] == "incompatible_run_state"
    assert state_path.read_bytes() == before

    rejected_run = run_cli(git_repo, fixture, "run", "1")
    assert rejected_run.returncode == 2
    assert stdout_json(rejected_run)["status"] == "incompatible_run_state"
    assert _file_snapshot(git_repo) == persistent_before


def test_custom_state_dir_cannot_fork_a_canonical_unfinished_run(
    git_repo: Path, tmp_path: Path
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": ticket()})
    agents = run_agents(git_repo / "agents.json")
    started = tmp_path / "custom-state-started"
    release = tmp_path / "custom-state-release"
    agent_data = json.loads(agents.read_text(encoding="utf-8"))
    agent_data["invocation_gate"] = {
        "role": "development",
        "started_file": str(started),
        "release_file": str(release),
        "timeout_seconds": 30,
    }
    agents.write_text(json.dumps(agent_data), encoding="utf-8")
    environment = _isolated_environment(tmp_path / "custom-state")
    source = str(Path(__file__).resolve().parents[1] / "src")
    environment["PYTHONPATH"] = source
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
        "--json",
    ]
    first = subprocess.Popen(
        command,
        cwd=git_repo,
        env={**os.environ, **environment},
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 10
        while not started.exists():
            if first.poll() is not None:
                stdout, stderr = first.communicate()
                raise AssertionError(f"canonical run exited early: {stdout}\n{stderr}")
            if time.monotonic() >= deadline:
                raise AssertionError(
                    "canonical run did not reach the controlled Agent gate"
                )
            os.sched_yield()

        canonical_run = next((git_repo / ".agent-run" / "runs").glob("*.json"))
        canonical_before = canonical_run.read_bytes()
        alternate_state = tmp_path / "alternate-state"
        blocked = run_cli(
            git_repo,
            fixture,
            "run",
            "1",
            "--state-dir",
            str(alternate_state),
            "--agent-fixture",
            str(agents),
            extra_env=environment,
        )
        assert blocked.returncode == 2
        assert stdout_json(blocked)["diagnostics"][0]["code"] == "task_control"
        assert not list((alternate_state / "runs").glob("*.json"))
        assert canonical_run.read_bytes() == canonical_before
    finally:
        release.touch()
        stdout, stderr = first.communicate(timeout=30)
    assert first.returncode == 0, f"{stdout}\n{stderr}"


def test_default_run_routes_to_an_unfinished_custom_state_dir(
    git_repo: Path, tmp_path: Path
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": ticket()})
    agents = run_agents(git_repo / "agents.json")
    environment = _isolated_environment(tmp_path / "custom-first")
    custom_state = tmp_path / "custom-state"

    first = run_cli(
        git_repo,
        fixture,
        "run",
        "1",
        "--state-dir",
        str(custom_state),
        "--agent-fixture",
        str(agents),
        extra_env=environment,
    )
    first_output = stdout_json(first)
    assert first.returncode == 0, f"{first.stdout}\n{first.stderr}"

    repeated = run_cli(
        git_repo,
        fixture,
        "run",
        "1",
        "--agent-fixture",
        str(agents),
        extra_env=environment,
    )
    repeated_output = stdout_json(repeated)

    assert repeated.returncode == 0, f"{repeated.stdout}\n{repeated.stderr}"
    assert repeated_output["run_id"] == first_output["run_id"]
    _assert_public_action_receipt(first_output, submission="started")
    _assert_public_action_receipt(repeated_output, submission="attached")
    assert len(list((custom_state / "runs").glob("*.json"))) == 1
    assert not list((git_repo / ".agent-run" / "runs").glob("*.json"))


def test_default_run_interrupt_does_not_make_cli_a_run_writer(
    git_repo: Path, tmp_path: Path
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": ticket()})
    agents = run_agents(git_repo / "agents.json")
    environment = _isolated_environment(tmp_path / "custom-interrupt")
    custom_state = tmp_path / "custom-interrupt-state"
    started = tmp_path / "custom-interrupt-started"
    release = tmp_path / "custom-interrupt-release"

    initial = seed_run(
        git_repo,
        fixture,
        "1",
        "--state-dir",
        str(custom_state),
        extra_env=environment,
    )
    assert initial.returncode == 0, f"{initial.stdout}\n{initial.stderr}"
    seed_idle_control(
        TaskControlStore(git_repo / ".agent-run"),
        TaskKey(git_repo, "example/project", 1),
        str(stdout_json(initial)["run_id"]),
        state_dir=custom_state,
    )

    agent_data = json.loads(agents.read_text(encoding="utf-8"))
    agent_data["invocation_gate"] = {
        "role": "development",
        "started_file": str(started),
        "release_file": str(release),
        "timeout_seconds": 30,
    }
    agents.write_text(json.dumps(agent_data), encoding="utf-8")
    source = str(Path(__file__).resolve().parents[1] / "src")
    environment["PYTHONPATH"] = source
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
    owner = subprocess.Popen(
        command,
        cwd=git_repo,
        env={**os.environ, **environment},
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 10
        while not started.exists():
            if owner.poll() is not None:
                stdout, stderr = owner.communicate()
                raise AssertionError(f"routed run exited early: {stdout}\n{stderr}")
            if time.monotonic() >= deadline:
                raise AssertionError(
                    "routed run did not reach the controlled Agent gate"
                )
            os.sched_yield()
        custom_run_path = next((custom_state / "runs").glob("*.json"))
        state_before_interrupt = custom_run_path.read_bytes()
        owner.send_signal(signal.SIGINT)
        stdout, stderr = owner.communicate(timeout=30)
        assert owner.returncode == 130, f"{stdout}\n{stderr}"
        assert "操作状态: 已中断" in stdout
        assert "交付状态: 进行中" in stdout
        assert "下一步: agent-run run 1 --repo example/project" in stdout
        assert "active" not in stdout
        assert "<run-id>" not in stdout
        custom_run_id = str(json.loads(custom_run_path.read_text())["run_id"])
        assert custom_run_id not in stdout
        control_path = next((git_repo / ".agent-run" / "task-control").glob("*.json"))
        control_during_observation_exit = json.loads(
            control_path.read_text(encoding="utf-8")
        )
        assert custom_run_path.read_bytes() == state_before_interrupt
        assert control_during_observation_exit["action"]["status"] == "completed"
        assert control_during_observation_exit["executor"]["status"] == "running"
        assert control_during_observation_exit["executor"]["pid"] != owner.pid

        release.touch()
        deadline = time.monotonic() + 10
        while True:
            final_control = json.loads(control_path.read_text(encoding="utf-8"))
            if final_control["executor"]["status"] == "exited":
                break
            if time.monotonic() >= deadline:
                raise AssertionError("detached fixture Executor did not finish")
            time.sleep(0.01)
    finally:
        release.touch()
        if owner.poll() is None:
            owner.communicate(timeout=30)

    custom_state_value = json.loads(custom_run_path.read_text(encoding="utf-8"))
    assert custom_state_value["status"] == "run_approval_pending"
    assert final_control["action"]["status"] == "completed"
    assert final_control["executor"]["status"] == "exited"
    assert final_control["executor"]["failure"] is None
    assert not list((git_repo / ".agent-run" / "runs").glob("*.json"))


def test_default_run_routes_to_a_custom_seeded_run(
    git_repo: Path, tmp_path: Path
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": ticket()})
    agents = run_agents(git_repo / "agents.json")
    environment = _isolated_environment(tmp_path / "custom-start-first")
    custom_state = tmp_path / "custom-start-state"

    started = seed_run(
        git_repo,
        fixture,
        "1",
        "--state-dir",
        str(custom_state),
        extra_env=environment,
    )
    started_output = stdout_json(started)
    assert started.returncode == 0, f"{started.stdout}\n{started.stderr}"
    seed_idle_control(
        TaskControlStore(git_repo / ".agent-run"),
        TaskKey(git_repo, "example/project", 1),
        str(stdout_json(started)["run_id"]),
        state_dir=custom_state,
    )

    continued = run_cli(
        git_repo,
        fixture,
        "run",
        "1",
        "--agent-fixture",
        str(agents),
        extra_env=environment,
    )
    continued_output = stdout_json(continued)

    assert continued.returncode == 0, f"{continued.stdout}\n{continued.stderr}"
    assert continued_output["result"] == "resumed"
    assert continued_output["run_id"] == started_output["run_id"]
    assert len(list((custom_state / "runs").glob("*.json"))) == 1
    assert not list((git_repo / ".agent-run" / "runs").glob("*.json"))


def test_default_run_attaches_to_custom_state_during_executor_handshake(
    git_repo: Path, tmp_path: Path
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": ticket()})
    agents = run_agents(git_repo / "agents.json")
    started = tmp_path / "custom-handshake-started"
    release = tmp_path / "custom-handshake-release"
    agent_data = json.loads(agents.read_text(encoding="utf-8"))
    agent_data["invocation_gate"] = {
        "role": "development",
        "started_file": str(started),
        "release_file": str(release),
        "timeout_seconds": 30,
    }
    agents.write_text(json.dumps(agent_data), encoding="utf-8")
    environment = _isolated_environment(tmp_path / "custom-handshake")
    source = str(Path(__file__).resolve().parents[1] / "src")
    environment["PYTHONPATH"] = source
    custom_state = tmp_path / "custom-handshake-state"
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
        "--json",
    ]
    first = subprocess.Popen(
        [*command, "--state-dir", str(custom_state)],
        cwd=git_repo,
        env={**os.environ, **environment},
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    second: subprocess.Popen[str] | None = None
    try:
        deadline = time.monotonic() + 10
        while not started.exists():
            if first.poll() is not None:
                stdout, stderr = first.communicate()
                raise AssertionError(f"custom run exited early: {stdout}\n{stderr}")
            if time.monotonic() >= deadline:
                raise AssertionError(
                    "custom run did not reach the controlled Agent gate"
                )
            os.sched_yield()
        custom_run_path = next((custom_state / "runs").glob("*.json"))
        custom_run_id = json.loads(custom_run_path.read_text(encoding="utf-8"))[
            "run_id"
        ]

        second = subprocess.Popen(
            command,
            cwd=git_repo,
            env={**os.environ, **environment},
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for _ in range(1000):
            if second.poll() is not None:
                break
            os.sched_yield()
        release.touch()
        second_stdout, second_stderr = second.communicate(timeout=30)
        first_stdout, first_stderr = first.communicate(timeout=30)
        assert first.returncode == 0, f"{first_stdout}\n{first_stderr}"
        assert second.returncode == 0, f"{second_stdout}\n{second_stderr}"
        second_output = json.loads(second_stdout)
        assert second_output["run_id"] == custom_run_id
    finally:
        release.touch()
        if second is not None and second.poll() is None:
            second.communicate(timeout=30)
        if first.poll() is None:
            first.communicate(timeout=30)
    assert len(list((custom_state / "runs").glob("*.json"))) == 1
    assert not list((git_repo / ".agent-run" / "runs").glob("*.json"))
