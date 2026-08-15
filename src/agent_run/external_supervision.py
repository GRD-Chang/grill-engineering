from __future__ import annotations

"""Bounded supervision of eventually-consistent GitHub lifecycle state."""

from dataclasses import dataclass
from time import monotonic, sleep
from typing import Any, Callable

CHECKS_BUDGET_SECONDS = 45 * 60
GITHUB_CONVERGENCE_BUDGET_SECONDS = 10 * 60
POLL_INTERVAL_SECONDS = 5
_RESUMABLE_STATUSES = frozenset({"waiting_checks", "waiting_merge", "waiting_external"})
_NON_CONVERGING_GITHUB_ERRORS = frozenset(
    {
        "ambiguous_run_pr",
        "github_invalid_response",
        "invalid_fixture",
        "invalid_parent",
        "missing_parent",
        "missing_pull_request",
        "stale_run_pr",
    }
)


@dataclass(frozen=True)
class WaitingBoundary:
    kind: str
    budget_seconds: int
    waiting_for: str


def waiting_boundary(state: dict[str, Any]) -> WaitingBoundary | None:
    """Return the current externally-converging boundary, if any."""

    status = state.get("status")
    if status == "waiting_checks":
        return WaitingBoundary(
            kind="required_checks",
            budget_seconds=CHECKS_BUDGET_SECONDS,
            waiting_for=f"{_waiting_object(state)} 的 GitHub Required Checks",
        )
    if status in {"waiting_merge", "waiting_external"}:
        return WaitingBoundary(
            kind="github_convergence",
            budget_seconds=GITHUB_CONVERGENCE_BUDGET_SECONDS,
            waiting_for=_waiting_object(state),
        )
    return None


class ExternalSupervisor:
    """Own a single foreground waiting window without creating a daemon."""

    def __init__(
        self,
        *,
        now: Callable[[], float] = monotonic,
        sleeper: Callable[[float], None] = sleep,
        poll_interval_seconds: int = POLL_INTERVAL_SECONDS,
    ) -> None:
        self.now = now
        self.sleeper = sleeper
        self.poll_interval_seconds = poll_interval_seconds
        self._started: dict[str, float] = {}

    def before_retry(self, state: dict[str, Any]) -> bool:
        """Sleep until a retry or persist a recoverable supervision timeout."""

        boundary = waiting_boundary(state)
        if boundary is None:
            return True
        started = self._started.setdefault(
            f"{boundary.kind}:{boundary.waiting_for}", self.now()
        )
        elapsed = max(0, int(self.now() - started))
        if elapsed >= boundary.budget_seconds:
            resume_status = state.get("status")
            state.update(
                {
                    "status": "supervision_timeout",
                    "terminal_kind": "supervision_timeout",
                    "supervision_wait": {
                        "resume_status": resume_status,
                        "kind": boundary.kind,
                        "waiting_for": boundary.waiting_for,
                        "phase": _phase(state),
                        "elapsed_seconds": elapsed,
                        "budget_seconds": boundary.budget_seconds,
                    },
                    "diagnostics": [
                        {
                            "code": "supervision_timeout",
                            "message": "外部状态在本次监督窗口内未收敛",
                            "waiting_for": boundary.waiting_for,
                            "phase": _phase(state),
                            "elapsed_seconds": elapsed,
                            "budget_seconds": boundary.budget_seconds,
                            "next_action": "重新执行 agent-run run 或 agent-run resume 以开始新的等待窗口",
                        }
                    ],
                }
            )
            return False
        self.sleeper(min(self.poll_interval_seconds, boundary.budget_seconds - elapsed))
        return True


def restore_supervision_wait(state: dict[str, Any]) -> None:
    """Start a fresh foreground window from a persisted supervision pause."""

    wait = state.get("supervision_wait")
    resume_status = wait.get("resume_status") if isinstance(wait, dict) else None
    if resume_status not in _RESUMABLE_STATUSES:
        raise ValueError("supervision timeout is missing its resumable boundary")
    state.update(
        {
            "status": resume_status,
            "terminal_kind": resume_status,
            "diagnostics": [],
        }
    )
    state.pop("supervision_wait", None)


def is_supervised_wait(state: dict[str, Any]) -> bool:
    """Whether a GitHub read failure must remain within foreground supervision."""

    return waiting_boundary(state) is not None


def is_github_convergence_error(code: str) -> bool:
    """Whether a GitHub read error can reasonably resolve by polling."""

    return code not in _NON_CONVERGING_GITHUB_ERRORS


def is_github_refresh_wait(state: dict[str, Any]) -> bool:
    """Whether Controller's latest authority refresh itself failed to read."""

    return state.get("github_refresh_pending") is True


def wait_for_github_convergence(
    state: dict[str, Any], *, code: str, message: str, waiting_for: str
) -> None:
    """Persist a recoverable 10-minute GitHub convergence boundary."""

    state.update(
        {
            "status": "waiting_external",
            "terminal_kind": "waiting_external",
            "diagnostics": [
                {
                    "code": code,
                    "message": message,
                    "waiting_for": waiting_for,
                }
            ],
        }
    )


def wait_for_github_refresh(
    state: dict[str, Any], *, code: str, message: str, waiting_for: str
) -> None:
    """Persist a failed Controller refresh without suppressing later retries."""

    wait_for_github_convergence(
        state, code=code, message=message, waiting_for=waiting_for
    )
    state["github_refresh_pending"] = True


def _waiting_object(state: dict[str, Any]) -> str:
    active = state.get("active_ticket_job")
    if isinstance(active, dict) and isinstance(active.get("pr_number"), int):
        return f"Ticket PR #{active['pr_number']} 的 GitHub 对账"
    publication = state.get("run_publication")
    if isinstance(publication, dict) and isinstance(publication.get("pr_number"), int):
        return f"Run PR #{publication['pr_number']} 的 GitHub 对账"
    parent = state.get("parent_job")
    if isinstance(parent, dict) and isinstance(parent.get("pr_number"), int):
        return f"Parent PR #{parent['pr_number']} 的 GitHub 对账"
    return "GitHub 外部状态"


def _phase(state: dict[str, Any]) -> str:
    for key in ("run_publication", "run_acceptance", "active_ticket_job", "parent_job"):
        value = state.get(key)
        phase = value.get("phase") if isinstance(value, dict) else None
        if isinstance(phase, str):
            return phase
    status = state.get("status")
    return status if isinstance(status, str) else "unknown"
