from __future__ import annotations

"""Bounded supervision of eventually-consistent GitHub lifecycle state."""

from dataclasses import dataclass
from hashlib import sha256
import json
from time import monotonic, sleep
from typing import Any, Callable

from agent_run.error_safety import bounded_error

CHECKS_BUDGET_SECONDS = 45 * 60
GITHUB_CONVERGENCE_BUDGET_SECONDS = 10 * 60
POLL_INTERVAL_SECONDS = 5
MAX_BACKOFF_SECONDS = 60
_RESUMABLE_STATUSES = frozenset({"waiting_checks", "waiting_merge", "waiting_external"})
_PROVEN_GITHUB_STATE_CONTRADICTIONS = frozenset(
    {
        "ambiguous_run_pr",
        "invalid_fixture",
        "invalid_parent",
        "missing_parent",
        "stale_run_pr",
        "foreign_run_pr",
        "github_invalid_response",
        "change_pr_identity_mismatch",
        "change_pr_head_drift",
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

    def before_retry(
        self,
        state: dict[str, Any],
        *,
        persist_before_sleep: Callable[[], None] | None = None,
    ) -> bool:
        """Sleep until a retry or persist a recoverable supervision timeout."""

        boundary = waiting_boundary(state)
        if boundary is None:
            return True
        window = self.observe(state)
        if window is None:  # pragma: no cover - guarded by waiting_boundary above
            return True
        prior_retries = window.get("retry_count")
        retries = (prior_retries if isinstance(prior_retries, int) else 0) + 1
        window["retry_count"] = retries
        observation = _last_external_error(state)
        window["latest_observation"] = observation
        availability = state.get("credential_availability")
        if isinstance(availability, dict) and boundary.kind == "github_convergence":
            availability["retry_count"] = retries
        deadline = window["deadline"]
        if not isinstance(deadline, (int, float)):
            raise ValueError("supervision window is missing its deadline")
        remaining = deadline - self.now()
        if remaining > 0:
            delay = min(_retry_delay_seconds(retries, self.poll_interval_seconds), remaining)
            # Persist the intended delay before sleeping, including crash recovery.
            window["last_retry_delay_seconds"] = delay
            if persist_before_sleep is not None:
                persist_before_sleep()
            remaining = deadline - self.now()
            if remaining > 0:
                self.sleeper(min(delay, remaining))
            if self.now() < deadline:
                return True

        self._expire(state, boundary, window)
        return False

    def _expire(
        self, state: dict[str, Any], boundary: WaitingBoundary, window: dict[str, object]
    ) -> None:
        started_at = window["started_at"]
        if not isinstance(started_at, (int, float)):
            raise ValueError("supervision window is missing its start time")
        elapsed = max(boundary.budget_seconds, int(self.now() - started_at))
        resume_status = state.get("status")
        last_error = _last_external_error(state)
        supervision_wait: dict[str, Any] = {
            "resume_status": resume_status,
            "kind": boundary.kind,
            "waiting_for": boundary.waiting_for,
            "identity": window["identity"],
            "phase": _phase(state),
            "started_at": window["started_at"],
            "deadline": window["deadline"],
            "elapsed_seconds": elapsed,
            "budget_seconds": boundary.budget_seconds,
            "retry_count": window["retry_count"],
            "latest_observation": window["latest_observation"],
        }
        diagnostic: dict[str, Any] = {
            "code": "supervision_timeout",
            "message": "外部状态在本次监督窗口内未收敛",
            "waiting_for": boundary.waiting_for,
            "phase": _phase(state),
            "elapsed_seconds": elapsed,
            "budget_seconds": boundary.budget_seconds,
            "next_action": _run_recovery_action(state),
        }
        if last_error is not None:
            diagnostic["last_error"] = last_error
        availability = state.get("credential_availability")
        if isinstance(availability, dict):
            failure_class = availability.get("failure_class")
            retry_count = availability.get("retry_count")
            if isinstance(failure_class, str):
                supervision_wait["credential_failure_class"] = failure_class
                diagnostic["credential_failure_class"] = failure_class
            if isinstance(retry_count, int):
                supervision_wait["retry_count"] = retry_count
                diagnostic["retry_count"] = retry_count
            http_status = availability.get("http_status")
            if type(http_status) is int and 100 <= http_status <= 599:
                supervision_wait["credential_http_status"] = http_status
                diagnostic["credential_http_status"] = http_status
        state.update(
            {
                "status": "supervision_timeout",
                "terminal_kind": "supervision_timeout",
                "supervision_wait": supervision_wait,
                "diagnostics": [diagnostic],
            }
        )

    def observe(self, state: dict[str, Any]) -> dict[str, object] | None:
        """Create or recover the durable window as soon as a wait is observed.

        The Driver persists the changed state before its first retry.  This
        makes an abrupt process exit indistinguishable from an ordinary
        restart: both continue to use the same deadline for the same waiting
        identity.
        """

        boundary = waiting_boundary(state)
        if boundary is None:
            return None
        return ensure_supervision_window(state, now=self.now)


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
    state.pop("supervision_window", None)


def is_supervised_wait(state: dict[str, Any]) -> bool:
    """Whether a GitHub read failure must remain within foreground supervision."""

    return waiting_boundary(state) is not None


def is_github_convergence_error(code: str) -> bool:
    """Whether a GitHub read failure remains safe to reconcile by polling.

    GitHub adapters preserve structured contradictions in ``code``. Every
    other read failure is deliberately treated as an unknown external state:
    the Harness records it and retries bounded reads, rather than inferring a
    network, authentication, permission, or proxy cause from command output.
    """

    return not is_proven_github_state_contradiction(code)


def is_proven_github_state_contradiction(code: str) -> bool:
    """Whether GitHub returned a fact that cannot safely be retried."""

    return code in _PROVEN_GITHUB_STATE_CONTRADICTIONS


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
                    "message": bounded_error(message),
                    "waiting_for": waiting_for,
                }
            ],
        }
    )


def ensure_supervision_window(
    state: dict[str, Any], *, now: Callable[[], float] | None = None
) -> dict[str, object] | None:
    """Save an observable window whenever a public path enters a wait.

    The normal ``run`` loop observes a wait before it retries it.  Internal
    initialization and approval paths can instead return immediately after
    recording a wait, so they establish the same durable window themselves.
    """

    boundary = waiting_boundary(state)
    if boundary is None:
        return None
    return _supervision_window(state, boundary, now=(now or monotonic)())


def supervision_window_matches(
    state: dict[str, Any],
    window: dict[str, object],
    *,
    boundary: WaitingBoundary | None = None,
) -> bool:
    """Whether a persisted window still belongs to the supplied state."""

    current_boundary = boundary if boundary is not None else waiting_boundary(state)
    window_kind = window.get("kind")
    if isinstance(window_kind, str) and window_kind in {
        "required_checks",
        "github_convergence",
    } and (current_boundary is None or current_boundary.kind != window_kind):
        # A refresh can temporarily project a wait to another lifecycle
        # status.  The persisted window kind is the boundary being resumed;
        # use it to compare the stable delivery identity without resetting
        # the deadline or following a replaced frontier.
        current_boundary = WaitingBoundary(
            kind=window_kind,
            budget_seconds=(
                CHECKS_BUDGET_SECONDS
                if window_kind == "required_checks"
                else GITHUB_CONVERGENCE_BUDGET_SECONDS
            ),
            waiting_for=_waiting_object(state),
        )
    return (
        current_boundary is not None
        and window.get("identity") == _window_identity(state, current_boundary)
    )


def _last_external_error(state: dict[str, Any]) -> dict[str, str] | None:
    diagnostics = state.get("diagnostics")
    if not isinstance(diagnostics, list) or not diagnostics:
        return None
    latest = diagnostics[0]
    if not isinstance(latest, dict):
        return None
    code = latest.get("code")
    message = latest.get("message")
    if not isinstance(code, str) or not isinstance(message, str):
        return None
    return {"code": code, "message": bounded_error(message)}


def _retry_delay_seconds(retry_count: int, base_delay_seconds: int) -> int:
    """Return the deterministic capped delay for one persisted retry count."""

    if retry_count < 1:
        raise ValueError("retry count must be positive")
    if base_delay_seconds < 1:
        raise ValueError("poll interval must be positive")
    delay = base_delay_seconds * 2 ** (retry_count - 1)
    return delay if delay < MAX_BACKOFF_SECONDS else MAX_BACKOFF_SECONDS


def wait_for_github_refresh(
    state: dict[str, Any], *, code: str, message: str, waiting_for: str
) -> None:
    """Persist a failed Controller refresh without suppressing later retries."""

    wait_for_github_convergence(
        state, code=code, message=message, waiting_for=waiting_for
    )
    state["github_refresh_pending"] = True
    ensure_supervision_window(state)


def _waiting_object(state: dict[str, Any]) -> str:
    availability = state.get("credential_availability")
    if isinstance(availability, dict):
        change_job = availability.get("change_job")
        if isinstance(change_job, str) and change_job:
            return f"{change_job} Worker credential availability"
    active = state.get("active_ticket_job")
    if isinstance(active, dict) and isinstance(active.get("pr_number"), int):
        return f"Ticket PR #{active['pr_number']} 的 GitHub 对账"
    acceptance = state.get("run_acceptance")
    repair = acceptance.get("repair_job") if isinstance(acceptance, dict) else None
    if isinstance(repair, dict) and isinstance(repair.get("pr_number"), int):
        return f"Run Repair PR #{repair['pr_number']} 的 GitHub 对账"
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


def clear_supervision_window(state: dict[str, Any]) -> bool:
    """Discard a completed window only after its subject reaches progress."""

    # A refresh can temporarily classify the enclosing Run as progress while
    # its Worker is still waiting for the first credential. Availability is
    # the authoritative boundary in that interval, so only a successful
    # Worker start may clear the shared window.
    if isinstance(state.get("credential_availability"), dict):
        return False
    return state.pop("supervision_window", None) is not None


def _supervision_window(
    state: dict[str, Any], boundary: WaitingBoundary, *, now: float
) -> dict[str, object]:
    identity = _window_identity(state, boundary)
    existing = state.get("supervision_window")
    if isinstance(existing, dict) and existing.get("identity") == identity:
        started_at = existing.get("started_at")
        deadline = existing.get("deadline")
        if (
            isinstance(started_at, (int, float))
            and isinstance(deadline, (int, float))
            and now >= started_at
        ):
            return existing
    window: dict[str, object] = {
        "identity": identity,
        "kind": boundary.kind,
        "waiting_for": boundary.waiting_for,
        "phase": _phase(state),
        "started_at": now,
        "deadline": now + boundary.budget_seconds,
        "budget_seconds": boundary.budget_seconds,
        "resume_action": "run",
        "retry_count": 0,
        "last_retry_delay_seconds": None,
        "latest_observation": _last_external_error(state),
    }
    head_sha, base_sha = _waiting_ref_facts(state)
    if head_sha is not None:
        window["head_sha"] = head_sha
    if base_sha is not None:
        window["base_sha"] = base_sha
    state["supervision_window"] = window
    return window


def public_supervision_snapshot(
    state: dict[str, Any], *, now: float | None = None
) -> dict[str, object] | None:
    """Return the stable, credential-safe wait projection for CLI readers."""

    wait = state.get("supervision_wait")
    timed_out = isinstance(wait, dict)
    if not timed_out:
        wait = state.get("supervision_window")
    if not isinstance(wait, dict):
        return None
    if now is None:
        now = monotonic()
    deadline = wait.get("deadline")
    remaining = (
        0
        if timed_out
        else max(0, int(deadline - now))
        if isinstance(deadline, (int, float))
        else None
    )
    head_sha = wait.get("head_sha")
    base_sha = wait.get("base_sha")
    if not isinstance(head_sha, str) or not isinstance(base_sha, str):
        inferred_head, inferred_base = _waiting_ref_facts(state)
        head_sha = head_sha if isinstance(head_sha, str) else inferred_head
        base_sha = base_sha if isinstance(base_sha, str) else inferred_base
    observation = wait.get("latest_observation")
    if not isinstance(observation, dict):
        observation = _last_external_error(state)
    next_action = _run_recovery_action(state)
    retry_count = wait.get("retry_count", 0)
    # Entering a wait follows one completed external observation (the read
    # that produced ``waiting_*``).  A public reader may race the Driver's
    # first delayed retry, so project that initial observation as retry one
    # instead of briefly exposing a contradictory zero-retry snapshot.
    if not timed_out and type(retry_count) is int and retry_count < 1:
        retry_count = 1
    snapshot: dict[str, object] = {
        "kind": wait.get("kind"),
        "subject": wait.get("waiting_for"),
        "head_sha": head_sha,
        "base_sha": base_sha,
        "started_at": wait.get("started_at"),
        "deadline": deadline,
        "remaining_seconds": remaining,
        "retry_count": retry_count,
        "latest_observation": observation,
        "next_action": next_action,
        # Operators need the recovery instruction before an active foreground
        # window expires too.  It is not the current action while that
        # foreground process is still supervising the wait.
        "timeout_resume_action": next_action,
    }
    failure_class = wait.get("credential_failure_class")
    if isinstance(failure_class, str):
        snapshot["credential_failure_class"] = failure_class
    http_status = wait.get("credential_http_status")
    if type(http_status) is int and 100 <= http_status <= 599:
        snapshot["credential_http_status"] = http_status
    return snapshot


def _waiting_ref_facts(state: dict[str, Any]) -> tuple[str | None, str | None]:
    acceptance = state.get("run_acceptance")
    repair = acceptance.get("repair_job") if isinstance(acceptance, dict) else None
    for job in (
        state.get("active_ticket_job"),
        repair,
        state.get("parent_job"),
        state.get("run_publication"),
    ):
        if not isinstance(job, dict):
            continue
        head_sha = next(
            (
                value
                for name in ("head_sha", "publication_sha", "candidate_sha")
                if isinstance((value := job.get(name)), str)
            ),
            None,
        )
        base_sha = job.get("base_sha")
        if isinstance(head_sha, str) or isinstance(base_sha, str):
            return (
                head_sha if isinstance(head_sha, str) else None,
                base_sha if isinstance(base_sha, str) else None,
            )
    base = state.get("base")
    return None, base.get("sha") if isinstance(base, dict) and isinstance(base.get("sha"), str) else None


def _run_recovery_action(state: dict[str, Any]) -> str | None:
    run_id = state.get("run_id")
    if state.get("status") == "supervision_timeout" and isinstance(run_id, str):
        return f"agent-run resume {run_id}"
    parent = state.get("parent")
    parent_number = parent.get("number") if isinstance(parent, dict) else None
    return f"agent-run run {parent_number}" if isinstance(parent_number, int) else None


def _window_identity(state: dict[str, Any], boundary: WaitingBoundary) -> str:
    availability = state.get("credential_availability")
    if isinstance(availability, dict):
        # A first-mint outage belongs to one pending Worker, not to whichever
        # mutable Run phase the authority projection happens to expose while
        # that Worker is retried.  Keep this identity deliberately narrow so
        # a normal refresh cannot silently open a new ten-minute window.
        credential_subject = {
            "run_id": state.get("run_id"),
            "kind": boundary.kind,
            "credential_availability": {
                item: availability.get(item)
                for item in ("change_job", "phase", "failure_class")
                if availability.get(item) is not None
            },
        }
        return json.dumps(
            credential_subject,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    subject: dict[str, object] = {
        "run_id": state.get("run_id"),
        "kind": boundary.kind,
        "run_branch": state.get("run_branch"),
    }
    base = state.get("base")
    if isinstance(base, dict):
        subject["run_base"] = {
            item: base.get(item)
            for item in ("branch", "sha")
            if base.get(item) is not None
        }
    for key in ("active_ticket_job", "parent_job", "run_publication"):
        value = state.get(key)
        if isinstance(value, dict):
            identity = {
                item: value.get(item)
                for item in (
                    "ticket_number",
                    "pr_number",
                    "ticket_branch",
                    "parent_branch",
                    "head_sha",
                    "base_sha",
                    "publication_sha",
                )
                if value.get(item) is not None
            }
            if review_facts := _review_boundary_facts(value):
                identity["review_boundary"] = review_facts
            subject[key] = identity
    acceptance = state.get("run_acceptance")
    repair = acceptance.get("repair_job") if isinstance(acceptance, dict) else None
    if isinstance(repair, dict):
        repair_identity = {
            item: repair.get(item)
            for item in (
                "repair_generation",
                "pr_number",
                "base_sha",
                "candidate_sha",
                "publication_sha",
            )
            if repair.get(item) is not None
        }
        if review_facts := _review_boundary_facts(repair):
            repair_identity["review_boundary"] = review_facts
        subject["run_repair_job"] = repair_identity
    return json.dumps(subject, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _review_boundary_facts(value: dict[str, Any]) -> dict[str, object]:
    """Keep a compact fingerprint of facts that bind a reviewed delivery."""

    facts: dict[str, object] = {
        item: value[item]
        for item in ("candidate_sha", "effective_revision")
        if value.get(item) is not None
    }
    for name, keys in (
        (
            "acceptance_record",
            (
                "reviewed_base_sha",
                "reviewed_candidate_sha",
                "reviewed_candidate_tree",
                "effective_revision",
            ),
        ),
        (
            "record",
            (
                "pr_head_sha",
                "run_head_sha",
                "default_head_sha",
                "expected_merge_tree",
                "parent_revision",
                "ticket_graph_revision",
            ),
        ),
    ):
        nested = value.get(name)
        if not isinstance(nested, dict):
            continue
        nested_facts = {
            item: nested[item] for item in keys if nested.get(item) is not None
        }
        completions = nested.get("ticket_completion_records")
        if isinstance(completions, list):
            encoded = json.dumps(
                completions, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
            nested_facts["ticket_completion_records_sha256"] = sha256(encoded).hexdigest()
        if nested_facts:
            facts[name] = nested_facts
    return facts
