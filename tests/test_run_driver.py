from __future__ import annotations

import threading

import pytest

from agent_run.controller import Controller
from agent_run.external_supervision import ExternalSupervisor
from agent_run.git import GitRepository
from agent_run.github_fixture import FixtureGitHubPublisher, FixtureGitHubReader
from agent_run.run_driver import (
    DirectRunOperations,
    RunDriver,
    RunOutcome,
    RunOutcomeKind,
    RunStep,
    _progress_marker,
)
from agent_run.state import StateStore
from agent_run.task_control import (
    ActionReconciliationError,
    TaskControlBusyError,
    TaskControlStore,
    TaskKey,
)
from conftest import write_fixture


@pytest.mark.parametrize(
    ("state", "expected_kind", "expected_step"),
    [
        ({"status": "active"}, RunOutcomeKind.PROGRESS, RunStep.DELIVER),
        (
            {"status": "waiting_external"},
            RunOutcomeKind.EXTERNAL_WAIT,
            RunStep.DELIVER,
        ),
        (
            {
                "status": "waiting_external",
                "run_acceptance": {
                    "phase": "repairing",
                    "repair_job": {"phase": "waiting_checks"},
                },
            },
            RunOutcomeKind.EXTERNAL_WAIT,
            RunStep.ACCEPT,
        ),
        (
            {
                "status": "waiting_merge",
                "run_acceptance": {
                    "phase": "repairing",
                    "repair_job": {"phase": "merging"},
                },
            },
            RunOutcomeKind.EXTERNAL_WAIT,
            RunStep.ACCEPT,
        ),
        (
            {"status": "waiting_merge"},
            RunOutcomeKind.EXTERNAL_WAIT,
            RunStep.DELIVER,
        ),
        ({"status": "ready_for_human"}, RunOutcomeKind.HUMAN_GATE, None),
        ({"status": "execution_failed"}, RunOutcomeKind.EXECUTION_FAILURE, None),
        (
            {"status": "unsupported_scope_change"},
            RunOutcomeKind.DETERMINISTIC_CONTRADICTION,
            None,
        ),
        (
            {"status": "deterministic_contradiction"},
            RunOutcomeKind.DETERMINISTIC_CONTRADICTION,
            None,
        ),
        (
            {
                "status": "progress_exhausted",
                "terminal_kind": "temporarily_no_work",
            },
            RunOutcomeKind.PROGRESS,
            None,
        ),
        (
            {
                "status": "progress_exhausted",
                "terminal_kind": "waiting_human",
            },
            RunOutcomeKind.HUMAN_GATE,
            None,
        ),
        ({"status": "requeue_required"}, RunOutcomeKind.REQUEUE_REQUIRED, None),
        ({"status": "completed"}, RunOutcomeKind.TERMINAL_COMPLETION, None),
        ({"status": "abandoned"}, RunOutcomeKind.TERMINAL_ABANDONMENT, None),
    ],
)
def test_driver_classifies_each_persistent_boundary_independently(
    state: dict[str, object], expected_kind: RunOutcomeKind, expected_step: RunStep | None
) -> None:
    outcome = DirectRunOperations.classify(state)

    assert outcome.kind is expected_kind
    assert outcome.next_step is expected_step


@pytest.mark.parametrize(
    "status",
    [
        "ready_for_human",
        "execution_failed",
        "unsupported_scope_change",
        "deterministic_contradiction",
        "requeue_required",
        "completed",
        "abandoned",
    ],
)
def test_driver_never_auto_dispatches_a_non_progress_boundary(status: str) -> None:
    class Operations:
        controller = object()

        @staticmethod
        def classify(state: dict[str, object]):
            return DirectRunOperations.classify(state)

        @staticmethod
        def dispatch(*_args: object) -> None:
            raise AssertionError("non-progress boundary must not auto-dispatch")

    class States:
        @staticmethod
        def save_run(*_args: object) -> None:
            raise AssertionError("terminal boundary must not be rewritten")

    state: dict[str, object] = {"run_id": "run-1", "status": status}
    result = RunDriver(operations=Operations(), states=States(), supervisor=object()).advance(state)  # type: ignore[arg-type]

    assert result is state


def test_driver_persists_backoff_before_sleep_and_a_restart_uses_it(tmp_path) -> None:
    state: dict[str, object] = {"run_id": "run-1", "status": "waiting_external"}
    states = StateStore(tmp_path)
    now = [0.0]
    sleeps: list[float] = []

    class SimulatedCrash(BaseException):
        pass

    def sleeper(seconds: float) -> None:
        sleeps.append(seconds)
        persisted = states.load_run("run-1")
        assert persisted is not None
        assert persisted["supervision_window"]["retry_count"] == 1
        assert persisted["supervision_window"]["last_retry_delay_seconds"] == 5
        raise SimulatedCrash

    class Operations:
        controller = object()

        @staticmethod
        def classify(current: dict[str, object]):
            return DirectRunOperations.classify(current)

        @staticmethod
        def dispatch(_step: RunStep, _run_id: str):
            return DirectRunOperations.classify(state)

    driver = RunDriver(
        operations=Operations(),
        states=states,
        supervisor=ExternalSupervisor(
            now=lambda: now[0], sleeper=sleeper, poll_interval_seconds=5
        ),
    )

    with pytest.raises(SimulatedCrash):
        driver.advance(state)

    persisted = states.load_run("run-1")
    assert persisted is not None
    assert persisted["status"] == "waiting_external"

    restarted = ExternalSupervisor(
        now=lambda: now[0], sleeper=sleeps.append, poll_interval_seconds=5
    )
    assert restarted.before_retry(persisted)
    assert sleeps == [5, 10]


def test_driver_records_failure_when_progress_identity_does_not_change(tmp_path) -> None:
    state: dict[str, object] = {
        "run_id": "run-1",
        "status": "active",
        "timeline": [{"at": "first", "status": "active"}],
    }
    class States:
        current = state

        @classmethod
        def save_run(cls, _run_id: str, current: dict[str, object]) -> None:
            cls.current = current

        @classmethod
        def load_current_run(cls, _run_id: str) -> dict[str, object]:
            return cls.current

    failures: list[str] = []

    class Controller:
        def record_execution_failure(self, run_id: str, message: str) -> bool:
            assert run_id == "run-1"
            failures.append(message)
            failed = dict(state)
            failed.update(
                {
                    "status": "execution_failed",
                    "terminal_kind": "execution_failed",
                    "diagnostics": [{"code": "controller_no_progress"}],
                }
            )
            States.save_run(run_id, failed)
            return True

    class Operations:
        controller = Controller()

        @staticmethod
        def classify(current: dict[str, object]) -> RunOutcome:
            return DirectRunOperations.classify(current)

        @staticmethod
        def dispatch(_step: RunStep, _run_id: str) -> RunOutcome:
            # A new timeline entry is not durable progress by itself.
            state["timeline"] = [{"at": "second", "status": "active"}]
            return RunOutcome(RunOutcomeKind.PROGRESS, state, RunStep.DELIVER)

    result = RunDriver(
        operations=Operations(),
        states=States(),  # type: ignore[arg-type]
        supervisor=object(),
    ).advance(state)

    assert result["status"] == "execution_failed"
    assert len(failures) == 1
    assert failures[0].startswith("controller_no_progress:")


def test_driver_does_not_apply_no_progress_guard_to_external_waits(tmp_path) -> None:
    state: dict[str, object] = {"run_id": "run-1", "status": "waiting_external"}
    states = StateStore(tmp_path)
    retries = 0

    class Controller:
        def record_execution_failure(self, *_args: object) -> bool:
            raise AssertionError("external waiting must not trigger no-progress failure")

    class Operations:
        controller = Controller()

        @staticmethod
        def classify(current: dict[str, object]) -> RunOutcome:
            return DirectRunOperations.classify(current)

        @staticmethod
        def dispatch(_step: RunStep, _run_id: str) -> RunOutcome:
            return DirectRunOperations.classify(state)

    class Supervisor:
        @staticmethod
        def observe(_state: dict[str, object]) -> None:
            pass

        @staticmethod
        def before_retry(
            _state: dict[str, object], *, persist_before_sleep: object
        ) -> bool:
            nonlocal retries
            retries += 1
            return False

        @staticmethod
        def now() -> float:
            return 0.0

    result = RunDriver(
        operations=Operations(),
        states=states,
        supervisor=Supervisor(),  # type: ignore[arg-type]
    ).advance(state)

    assert result is state
    assert retries == 1


def test_resume_refresh_authority_is_used_only_until_the_initial_retry_succeeds() -> None:
    class States:
        current = {
            "run_id": "run-1",
            "status": "waiting_external",
            "github_refresh_pending": True,
        }

        @classmethod
        def load_current_run(cls, _run_id: str):
            return cls.current

    states = States()
    ordinary_refreshes: list[str] = []
    explicit_refreshes: list[str] = []

    class Controller:
        @staticmethod
        def resume(run_id: str):
            ordinary_refreshes.append(run_id)
            return {"run_id": run_id, "status": "ready_for_human"}, True

    def retry_explicit_resume(run_id: str):
        explicit_refreshes.append(run_id)
        return {"run_id": run_id, "status": "active"}, True

    operations = DirectRunOperations(
        controller=Controller(),
        states=states,
        git=object(),
        github_reader=object(),
        publisher_factory=lambda: object(),
        agents=object(),
        resume_pending_refresh=retry_explicit_resume,
    )

    first, _ = operations._refresh("run-1")
    assert first["status"] == "active"
    assert explicit_refreshes == ["run-1"]
    assert ordinary_refreshes == []

    states.current = {
        "run_id": "run-1",
        "status": "waiting_external",
        "github_refresh_pending": True,
    }
    second, _ = operations._refresh("run-1")
    assert second["status"] == "ready_for_human"
    assert explicit_refreshes == ["run-1"]
    assert ordinary_refreshes == ["run-1"]


def test_successful_resume_disarms_future_explicit_refresh_retry() -> None:
    class States:
        current = {"run_id": "run-1", "status": "active"}

        @classmethod
        def load_current_run(cls, _run_id: str):
            return cls.current

    ordinary_refreshes: list[str] = []
    explicit_refreshes: list[str] = []

    class Controller:
        @staticmethod
        def resume(run_id: str):
            ordinary_refreshes.append(run_id)
            return {"run_id": run_id, "status": "ready_for_human"}, True

    def retry_explicit_resume(run_id: str):
        explicit_refreshes.append(run_id)
        return {"run_id": run_id, "status": "active"}, True

    operations = DirectRunOperations(
        controller=Controller(),
        states=States(),
        git=object(),
        github_reader=object(),
        publisher_factory=lambda: object(),
        agents=object(),
        resume_pending_refresh=retry_explicit_resume,
        use_current_state_once=True,
    )

    first, _ = operations._refresh("run-1")
    assert first["status"] == "active"
    assert explicit_refreshes == []
    assert ordinary_refreshes == []

    States.current = {
        "run_id": "run-1",
        "status": "waiting_external",
        "github_refresh_pending": True,
    }
    second, _ = operations._refresh("run-1")
    assert second["status"] == "ready_for_human"
    assert explicit_refreshes == []
    assert ordinary_refreshes == ["run-1"]


def test_publication_operation_retry_observations_are_not_progress() -> None:
    state: dict[str, object] = {
        "status": "waiting_external",
        "run_publication": {
            "phase": "waiting_external",
            "publication_operation_retry": {"attempts": 1, "limit": 5},
            "last_publication_error": "first readback failure",
        },
    }
    before = _progress_marker(state)

    state["run_publication"] = {
        "phase": "waiting_external",
        "publication_operation_retry": {"attempts": 2, "limit": 5},
        "last_publication_error": "second readback failure",
    }

    assert _progress_marker(state) == before


def test_dispatch_fence_rejects_old_external_operation_and_state_commit(
    tmp_path,
) -> None:
    task = TaskKey(tmp_path / "checkout", "example/project", 156)
    control = TaskControlStore(tmp_path / "control")
    states = StateStore(tmp_path / "runs")
    initial = {"run_id": "run-1", "status": "active"}
    states.save_run("run-1", initial)
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

    def fence() -> None:
        control.assert_executor_current(
            task,
            action_id=claim.action_id,
            generation=reservation.generation,
            run_id="run-1",
        )

    def state_commit_transaction() -> object:
        return control._executor_current_transaction(
            task,
            action_id=claim.action_id,
            generation=reservation.generation,
            run_id="run-1",
        )

    states._set_write_guard(fence, transaction=state_commit_transaction)

    agent_entered = threading.Event()
    generation_replaced = threading.Event()

    class Agents:
        def develop(self, _request: dict[str, object]) -> None:
            agent_entered.set()
            if not generation_replaced.wait(timeout=2):
                raise AssertionError("generation replacement did not reach the Agent barrier")

    def replace_generation() -> None:
        if not agent_entered.wait(timeout=2):
            return
        control.mark_executor_absent(
            task,
            action_id=claim.action_id,
            generation=reservation.generation,
        )
        control.begin_executor(
            task,
            action_id=claim.action_id,
            run_id="run-1",
            reclaim=True,
        )
        generation_replaced.set()

    class Git:
        def resolve(self, _reference: str) -> str:
            return "unreachable"

    operations = DirectRunOperations(
        controller=object(),
        states=states,
        git=Git(),
        github_reader=object(),
        publisher_factory=lambda: object(),
        agents=Agents(),
        before_external_step=fence,
    )

    def deliver(_run_id: str) -> RunOutcome:
        operations.agents.develop({})
        operations.git.resolve("after-agent")
        operations.states.save_run(
            "run-1", {"run_id": "run-1", "status": "execution_failed"}
        )
        return RunOutcome(
            RunOutcomeKind.PROGRESS,
            {"run_id": "run-1", "status": "active"},
            None,
        )

    operations.deliver = deliver  # type: ignore[method-assign]

    replacer = threading.Thread(target=replace_generation, daemon=True)
    replacer.start()
    try:
        with pytest.raises(ActionReconciliationError, match="generation"):
            operations.dispatch(RunStep.DELIVER, "run-1")
    finally:
        agent_entered.set()
        generation_replaced.set()
        replacer.join(timeout=2)
    assert not replacer.is_alive()

    with pytest.raises(ActionReconciliationError, match="generation"):
        states.save_run(
            "run-1", {"run_id": "run-1", "status": "execution_failed"}
        )
    assert states.load_run("run-1") == initial


def test_direct_operations_fence_controller_github_and_publisher(
    tmp_path, git_repo
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={
            "2": {
                "number": 2,
                "title": "Ticket 2",
                "body": "Deliver ticket 2.",
                "state": "OPEN",
                "labels": ["ready-for-agent"],
                "blocked_by": [],
            }
        },
    )
    task = TaskKey(git_repo, "example/project", 156)
    control = TaskControlStore(tmp_path / "control")
    states = StateStore(tmp_path / "runs")
    git = GitRepository(git_repo)
    reader = FixtureGitHubReader(fixture)
    controller = Controller(reader, git, states)
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

    def fence() -> None:
        control.assert_executor_current(
            task,
            action_id=claim.action_id,
            generation=reservation.generation,
            run_id="run-1",
        )

    operations = DirectRunOperations(
        controller=controller,
        states=states,
        git=git,
        github_reader=reader,
        publisher_factory=lambda: FixtureGitHubPublisher(fixture, git),
        agents=object(),
        before_external_step=fence,
    )
    control.mark_executor_absent(
        task,
        action_id=claim.action_id,
        generation=reservation.generation,
    )
    control.begin_executor(
        task,
        action_id=claim.action_id,
        run_id="run-1",
        reclaim=True,
    )
    before = fixture.read_text(encoding="utf-8")

    for operation in (
        lambda: operations.controller.resume("run-1"),
        lambda: operations.github_reader.repository(),
        lambda: operations.publisher.ensure_run_repair_branch(
            branch="agent-run/test", base_branch="main"
        ),
    ):
        with pytest.raises(ActionReconciliationError):
            operation()

    assert fixture.read_text(encoding="utf-8") == before


def test_state_commit_transaction_serializes_generation_replacement(tmp_path) -> None:
    task = TaskKey(tmp_path / "checkout", "example/project", 156)
    control = TaskControlStore(tmp_path / "control")

    class BarrierStateStore(StateStore):
        def __init__(self, root) -> None:
            super().__init__(root)
            self.commit_entered = threading.Event()
            self.allow_commit = threading.Event()
            self.pause_commit = False

        def _save_run_unlocked(self, run_id, state) -> None:
            if self.pause_commit:
                self.pause_commit = False
                self.commit_entered.set()
                if not self.allow_commit.wait(timeout=2):
                    raise AssertionError("state commit barrier was not released")
            super()._save_run_unlocked(run_id, state)

    states = BarrierStateStore(tmp_path / "runs")
    initial = {"run_id": "run-1", "status": "active"}
    states.save_run("run-1", initial)
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

    def state_commit_transaction() -> object:
        return control._executor_current_transaction(
            task,
            action_id=claim.action_id,
            generation=reservation.generation,
            run_id="run-1",
        )

    states._set_write_guard(
        lambda: control.assert_executor_current(
            task,
            action_id=claim.action_id,
            generation=reservation.generation,
            run_id="run-1",
        ),
        transaction=state_commit_transaction,
    )
    states.pause_commit = True
    writer_errors: list[BaseException] = []
    commit_finished = threading.Event()

    def write_state() -> None:
        try:
            states.save_run("run-1", {"run_id": "run-1", "status": "committed"})
        except BaseException as error:  # pragma: no cover - diagnostic propagation
            writer_errors.append(error)
        finally:
            commit_finished.set()

    generation_replaced = threading.Event()
    revocation_attempted = threading.Event()
    busy_observed = threading.Event()

    def replace_generation() -> None:
        revocation_attempted.set()
        try:
            control.mark_executor_absent(
                task,
                action_id=claim.action_id,
                generation=reservation.generation,
            )
        except TaskControlBusyError:
            busy_observed.set()
            if not states.allow_commit.wait(timeout=2):
                raise AssertionError("state commit barrier was not released")
            if not commit_finished.wait(timeout=2):
                raise AssertionError("state commit did not finish")
            control.mark_executor_absent(
                task,
                action_id=claim.action_id,
                generation=reservation.generation,
            )
        control.begin_executor(
            task,
            action_id=claim.action_id,
            run_id="run-1",
            reclaim=True,
        )
        generation_replaced.set()

    writer = threading.Thread(target=write_state, daemon=True)
    writer.start()
    assert states.commit_entered.wait(timeout=2)
    replacer = threading.Thread(target=replace_generation, daemon=True)
    replacer.start()
    assert revocation_attempted.wait(timeout=2)
    assert busy_observed.wait(timeout=2)
    assert not generation_replaced.is_set()
    states.allow_commit.set()
    writer.join(timeout=2)
    replacer.join(timeout=2)

    assert not writer.is_alive()
    assert not replacer.is_alive()
    assert writer_errors == []
    assert generation_replaced.is_set()
    committed = states.load_run("run-1")
    assert committed is not None
    assert committed["run_id"] == "run-1"
    assert committed["status"] == "committed"
    with pytest.raises(ActionReconciliationError, match="generation"):
        states.save_run(
            "run-1", {"run_id": "run-1", "status": "stale-overwrite"}
        )
    assert states.load_run("run-1")["status"] == "committed"
