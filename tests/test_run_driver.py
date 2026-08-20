from __future__ import annotations

import pytest

from agent_run.external_supervision import ExternalSupervisor
from agent_run.run_driver import (
    DirectRunOperations,
    RunDriver,
    RunOutcomeKind,
    RunStep,
)
from agent_run.state import StateStore


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
