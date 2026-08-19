from __future__ import annotations

"""Shared, deterministic delivery lifecycle for a change job.

The controller owns this lifecycle.  A job-specific contract supplies only
identity, request construction, and the one completion side effect (closing a
Ticket or returning control to Run Acceptance).  In particular, contracts do
not get a shortcut from development to a Run Branch write.
"""

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from agent_run.agents import AgentBackend, HumanBlockerResult, PublicationResult
from agent_run.agent_invocation import (
    canonical_fingerprint,
    invocation_event_recorder,
)
from agent_run.artifacts import (
    AcceptanceArtifact,
    PublicationArtifact,
    append_human_blocker_history,
    clear_current_human_blocker,
)
from agent_run.credential_availability import (
    clear_initial_credential_wait,
    resume_initial_credential_wait,
    wait_for_initial_credential,
)
from agent_run.delivery_protocol import GitHubPublisher
from agent_run.external_supervision import is_github_convergence_error
from agent_run.git import GitRepository
from agent_run.github import GitHubReadError, MergeOutcomeUnknownError
from agent_run.publication_pending import publication_pending_diagnostic
from agent_run.worker_credentials import InitialCredentialUnavailable

MAX_MODIFICATION_ATTEMPTS = 10
MAX_PUBLICATION_CONTEXT_ATTEMPTS = 4
MAX_PUBLICATION_ATTEMPTS = MAX_PUBLICATION_CONTEXT_ATTEMPTS + 1


class StaleDisposition(str, Enum):
    """The shared engine's deterministic recovery for a stale change job."""

    BLOCK = "block"
    FRESH_RUN_ACCEPTANCE = "fresh_run_acceptance"


@dataclass(frozen=True)
class ChangeJobContract:
    """The job-local behaviour which is intentionally outside the pipeline.

    All callbacks receive the durable run state and job record.  The pipeline
    retains ownership of phase transitions, candidate/publication commits,
    reviewer identity, Required Checks, exact-head verification, and squash
    completion.  This keeps Ticket and Run Repair jobs from diverging.
    """

    label: Callable[[dict[str, Any]], str]
    branch: Callable[[dict[str, Any]], str]
    base_branch: Callable[[dict[str, Any]], str]
    candidate: Callable[[Path, dict[str, Any], int], str | None]
    development_thread_is_allowed: Callable[[dict[str, Any], str], bool]
    development_request: Callable[
        [dict[str, Any], dict[str, Any], Path], dict[str, Any]
    ]
    publication_request: Callable[
        [dict[str, Any], dict[str, Any], Path], dict[str, Any]
    ]
    review_request: Callable[[dict[str, Any], dict[str, Any], Path], dict[str, Any]]
    prepare_validation: Callable[[Path, dict[str, Any], Path], None]
    ensure_pr: Callable[[dict[str, Any], dict[str, Any], dict[str, Any]], int]
    acceptance_record: Callable[
        [dict[str, Any], dict[str, Any], str, dict[str, Any]], dict[str, Any]
    ]
    acceptance_is_current: Callable[
        [dict[str, Any], dict[str, Any], dict[str, Any]], bool
    ]
    invalidate_stale: Callable[[dict[str, Any], dict[str, Any], Path], None]
    stale_disposition: StaleDisposition
    revision_changed: Callable[[dict[str, Any], dict[str, Any]], bool]
    requires_explicit_approval: Callable[[dict[str, Any], dict[str, Any]], bool]
    after_merge: Callable[[dict[str, Any], dict[str, Any], dict[str, Any]], bool]
    escalate: Callable[[dict[str, Any], dict[str, Any], str], None]
    save: Callable[[dict[str, Any]], dict[str, Any]]
    linked_issue_number: Callable[[dict[str, Any], dict[str, Any]], int] | None = None


def ensure_linked_branch_display(
    *,
    github: GitHubPublisher,
    state: dict[str, Any],
    job: dict[str, Any],
    issue_number: int,
    branch: str,
    head_sha: str,
    save: Callable[[dict[str, Any]], Any],
) -> None:
    """Make the one optional Linked Branch display attempt durable.

    The ref and PR are already authoritative when this function is reached.
    A crash after recording the attempt deliberately becomes indeterminate on
    recovery instead of replaying a potentially successful GitHub mutation.
    """
    existing = job.get("linked_branch_display")
    if isinstance(existing, dict) and existing.get("display_attempted") is True:
        if existing.get("status") not in {"linked", "unavailable", "indeterminate"}:
            existing["status"] = "indeterminate"
            save(state)
        return

    display: dict[str, Any] = {
        "display_attempted": True,
        "status": "indeterminate",
    }
    job["linked_branch_display"] = display
    save(state)
    status = github.link_issue_branch_display(
        issue_number=issue_number, branch=branch, head_sha=head_sha
    )
    display["status"] = status if status in {"linked", "unavailable"} else "unavailable"
    save(state)


def ensure_change_branch_authority(
    *,
    github: GitHubPublisher,
    state: dict[str, Any],
    job: dict[str, Any],
    branch: str,
    base_branch: str,
    save: Callable[[dict[str, Any]], Any],
    ticket_number: int | None = None,
) -> None:
    """Persist exact ref intent before creating or recovering a Change ref."""
    pending = job.get("ticket_write_intent")
    expected_remote_sha = str(job.get("published_sha", job["base_sha"]))
    recovery_remote_sha = expected_remote_sha
    if isinstance(pending, dict) and pending.get("action") == "publish_ticket_ref":
        expected = pending.get("expected_remote_sha")
        head = pending.get("head_sha")
        if not isinstance(expected, str) or not isinstance(head, str):
            raise ValueError("Change publish intent has invalid ref identity")
        expected_remote_sha = expected
        recovery_remote_sha = head
    authority = {
        "branch": branch,
        "base_branch": base_branch,
        "base_sha": str(job["base_sha"]),
        "expected_remote_sha": expected_remote_sha,
        "recovery_remote_sha": recovery_remote_sha,
    }
    created_intent = pending is None
    pending_action: str | None = None
    if created_intent:
        job["ticket_write_intent"] = {
            "action": "ensure_change_branch",
            "authority": authority,
        }
        pending_action = "ensure_change_branch"
        save(state)
    elif not isinstance(pending, dict):
        raise ValueError("Change ref has an invalid write intent")
    elif pending.get("action") == "ensure_change_branch":
        pending_action = "ensure_change_branch"
        if pending.get("authority") != authority:
            raise ValueError("Change branch intent does not match durable authority")
    elif pending.get("action") == "ensure_ticket_pr":
        # The ref is already published.  Preserve the durable PR intent so
        # the shared delivery loop can recover it by exact PR readback.
        pass
    elif pending.get("action") != "publish_ticket_ref":
        raise ValueError("Change ref has an unknown write intent")
    if ticket_number is None:
        github.ensure_change_branch(
            branch=branch, base_branch=base_branch,
            expected_base_sha=authority["base_sha"],
            expected_remote_sha=expected_remote_sha,
            recovery_remote_sha=recovery_remote_sha,
        )
    else:
        github.ensure_ticket_branch(
            ticket_number=ticket_number, branch=branch, base_branch=base_branch,
            expected_base_sha=authority["base_sha"],
            expected_remote_sha=expected_remote_sha,
            recovery_remote_sha=recovery_remote_sha,
        )
    if created_intent or pending_action == "ensure_change_branch":
        job.pop("ticket_write_intent", None)
        save(state)


class ChangeDeliveryEngine:
    """One Development -> Candidate -> Review -> Publication -> Merge loop.

    ``job`` uses the existing durable phase values so Ticket records remain
    backward compatible.  Run Repair records use the same fields but have no
    Ticket number and their ``after_merge`` callback never closes an Issue.
    """

    def __init__(
        self,
        *,
        git: GitRepository,
        github: GitHubPublisher,
        agents: AgentBackend,
        contract: ChangeJobContract,
    ) -> None:
        self.git = git
        self.github = github
        self.agents = agents
        self.contract = contract

    def run(
        self, state: dict[str, Any], job: dict[str, Any], checkout: Path
    ) -> dict[str, Any]:
        try:
            while True:
                phase = str(job["phase"])
                if phase in {"completed", "blocked"}:
                    return state
                if phase == "publication_pending":
                    # A new explicit delivery attempt is allowed to retry
                    # publication only; the accepted Candidate and Fresh
                    # Acceptance remain the durable boundary.
                    job["phase"] = "accepted"
                    job["publication_attempts"] = 0
                    job.pop("last_publication_error", None)
                    state["status"] = "active"
                    state["diagnostics"] = []
                    self.contract.save(state)
                    continue
                if phase == "merged":
                    live = self.github.live_pull_request(int(job["pr_number"]))
                    if not self.contract.after_merge(state, job, live):
                        return state
                    job["phase"] = "completed"
                    return self.contract.save(state)
                if phase == "escalating":
                    self.contract.escalate(state, job, str(job["escalation_code"]))
                    job["phase"] = "blocked"
                    return self.contract.save(state)
                if phase in {"developing", "repairing"}:
                    self._develop(state, job, checkout)
                if job["phase"] == "committing_candidate":
                    if not self._commit_candidate(state, job, checkout):
                        return state
                if job["phase"] == "candidate":
                    self._review(state, job, checkout)
                if job["phase"] == "reviewing":
                    # The reviewer may have been interrupted after its attempt
                    # was durably announced but before its result was saved.
                    # Keep that attempt in the timeline, then request a fresh
                    # independent reviewer on resume.
                    job["phase"] = "candidate"
                    self.contract.save(state)
                    continue
                if job["phase"] == "accepted":
                    self._publication(state, job, checkout)
                if job["phase"] == "escalating":
                    continue
                if job["phase"] in {"publishing", "waiting_checks", "merging"}:
                    terminal = self._publish_and_merge(state, job, checkout)
                    if terminal:
                        return state
                    continue
                if job["phase"] == "publication_pending":
                    return state
                if job["phase"] == "stale":
                    return state
                if job["phase"] in {"developing", "repairing"}:
                    continue
                if job["phase"] in {"completed", "blocked"}:
                    return state
                raise ValueError(f"unknown Ticket phase: {job['phase']}")
        except _TerminalChangeJob:
            return state
        except InitialCredentialUnavailable as error:
            self._wait_for_initial_credential(
                state, job, http_status=error.http_status
            )
            return state

    def _develop(
        self, state: dict[str, Any], job: dict[str, Any], checkout: Path
    ) -> None:
        self._resume_after_initial_credential(state, job)
        self._reject_stale(
            state,
            job,
            checkout,
            "Development did not start after requirements changed",
        )
        pending = job.get("pending_attempt")
        attempt = (
            pending
            if isinstance(pending, int)
            and pending > int(job.get("modification_attempts", 0))
            else int(job.get("modification_attempts", 0)) + 1
        )
        # Persist the actual Worker attempt before invoking Codex.  This
        # makes a foreground status query truthful even while Codex is still
        # running, and leaves an observable recovery boundary on interruption.
        job["pending_attempt"] = attempt
        self.contract.save(state)
        request = self.contract.development_request(state, job, checkout)
        request["_invocation_event"] = self._invocation_events(
            state, job, request, role="development", phase=str(job["phase"])
        )
        request["_currentness_check"] = lambda: self._agent_is_current(state, job)
        if job.get("development_new_thread") is True:
            request["_invocation_mode"] = "new-thread"
        elif job.get("development_failure_resume") is True:
            request["_invocation_mode"] = "resume"
        result = self.agents.develop(request)
        clear_initial_credential_wait(
            state, work_subject=self.contract.label(job)
        )
        if isinstance(result, HumanBlockerResult):
            if not self.contract.development_thread_is_allowed(state, result.thread_id):
                raise ValueError("Change Job Development Thread is not independent")
            _record_development_thread(job, result.thread_id, result.replaced_thread_id)
            job.pop("development_new_thread", None)
            job.pop("development_failure_resume", None)
            self._wait_for_human(
                state, job, phase=str(job["phase"]), blockers=result.human_blockers
            )
            return
        if not result.thread_id.strip() or not result.summary.strip():
            raise ValueError("Development result is incomplete")
        if not self.contract.development_thread_is_allowed(state, result.thread_id):
            raise ValueError("Change Job Development Thread is not independent")
        _record_development_thread(job, result.thread_id, result.replaced_thread_id)
        job.pop("development_new_thread", None)
        job.pop("development_failure_resume", None)
        job["development_summary"] = result.summary
        clear_current_human_blocker(job)
        job["phase"] = "committing_candidate"
        self.contract.save(state)
        self._reject_stale(
            state,
            job,
            checkout,
            "Development result was discarded after requirements changed",
        )

    def _wait_for_initial_credential(
        self,
        state: dict[str, Any],
        job: dict[str, Any],
        *,
        http_status: int | None,
    ) -> None:
        wait_for_initial_credential(
            state,
            work_subject=self.contract.label(job),
            phase=str(job["phase"]),
            resume_status=str(state.get("status", "active")),
            http_status=http_status,
        )
        self.contract.save(state)

    def _resume_after_initial_credential(
        self, state: dict[str, Any], job: dict[str, Any]
    ) -> None:
        if resume_initial_credential_wait(
            state, work_subject=self.contract.label(job)
        ):
            self.contract.save(state)

    def _commit_candidate(
        self, state: dict[str, Any], job: dict[str, Any], checkout: Path
    ) -> bool:
        attempt = int(job["pending_attempt"])
        candidate = self.contract.candidate(checkout, job, attempt)
        if candidate is None:
            job.pop("pending_attempt", None)
            return self._block(
                state,
                job,
                "no_code_changes",
                "Development Attempt produced no code changes",
            )
        job.update(
            {
                "candidate_sha": candidate,
                "modification_attempts": attempt,
                "phase": "candidate",
            }
        )
        job.pop("pending_attempt", None)
        self.contract.save(state)
        return True

    def _publication(
        self, state: dict[str, Any], job: dict[str, Any], checkout: Path
    ) -> None:
        while True:
            if not self._publication_is_current(state, job):
                self._invalidate_stale(state, job, checkout)
                self.contract.save(state)
                return
            try:
                request = self.contract.publication_request(state, job, checkout)
            except GitHubReadError as error:
                if not is_github_convergence_error(error.code):
                    raise
                attempts = int(job.get("publication_attempts", 0)) + 1
                job["publication_attempts"] = attempts
                job["last_publication_error"] = str(error)
                if attempts >= MAX_PUBLICATION_ATTEMPTS:
                    job["phase"] = "publication_pending"
                    state["status"] = "publication_pending"
                    state["terminal_kind"] = "publication_pending"
                    state["diagnostics"] = [
                        publication_pending_diagnostic(
                            subject_key="change_job", subject=self.contract.label(job)
                        )
                    ]
                    self.contract.save(state)
                    return
                self.contract.save(state)
                continue
            job["publication_attempts"] = int(job.get("publication_attempts", 0)) + 1
            self.contract.save(state)
            request["_invocation_event"] = self._invocation_events(
                state, job, request, role="publication", phase="publication"
            )
            request["_currentness_check"] = lambda: self._publication_is_current(
                state, job
            )
            if job.get("publication_new_thread") is True:
                request["_invocation_mode"] = "new-thread"
            elif job.get("publication_failure_resume") is True:
                request["_invocation_mode"] = "resume"
            raw = self.agents.publication(request)
            clear_initial_credential_wait(
                state, work_subject=self.contract.label(job)
            )
            job.pop("publication_failure_resume", None)
            if isinstance(raw, HumanBlockerResult):
                job["publication_thread_id"] = raw.thread_id
                job.pop("publication_new_thread", None)
                self._wait_for_human(
                    state,
                    job,
                    phase="accepted",
                    blockers=raw.human_blockers,
                )
                return
            if isinstance(raw, PublicationResult):
                job.pop("publication_new_thread", None)
                if raw.replaced_thread_id is not None:
                    raise ValueError(
                        "Publication Invocation cannot replace its Thread automatically"
                    )
                elif raw.thread_id != job.get("development_thread_id"):
                    job["publication_thread_id"] = raw.thread_id
                artifact_data = raw.artifact
                parse_publication = PublicationArtifact.parse
            else:
                artifact_data = raw
                parse_publication = (
                    PublicationArtifact.parse
                    if isinstance(raw, dict) and "result_kind" in raw
                    else PublicationArtifact.from_stored
                )
            job.pop("publication_new_thread", None)
            if isinstance(job.get("ticket_number"), int):
                publication = parse_publication(
                    artifact_data, primary_ticket=int(job["ticket_number"])
                )
            else:
                publication = parse_publication(
                    artifact_data, delivery_run=str(job["run_id"])
                )
            break
        clear_current_human_blocker(job)
        if not self._publication_is_current(state, job):
            self._invalidate_stale(state, job, checkout)
            self.contract.save(state)
            return
        sha = self.git.create_publication_commit(
            checkout,
            candidate_sha=str(job["candidate_sha"]),
            base_sha=str(job["base_sha"]),
            message=publication.commit_message,
        )
        job.update(
            {
                "publication": {
                    "commit_message": publication.commit_message,
                    "pr_title": publication.pr_title,
                    "pr_body_markdown": publication.pr_body_markdown,
                },
                "publication_sha": sha,
                "phase": "publishing",
            }
        )
        job.pop("last_publication_error", None)
        self.contract.save(state)
        self._reject_stale(
            state, job, checkout, "Publication was discarded after requirements changed"
        )

    def _invocation_events(
        self,
        state: dict[str, Any],
        job: dict[str, Any],
        request: dict[str, Any],
        *,
        role: str = "publication",
        phase: str,
    ) -> Callable[..., None]:
        work_subject, generation = self._invocation_identity(state, job)
        boundary: dict[str, Any] = {
            "base_sha": str(job["base_sha"]),
        }
        if "candidate_sha" in job:
            boundary["candidate_sha"] = str(job["candidate_sha"])
        acceptance = job.get("acceptance_record")
        if isinstance(acceptance, dict) and "reviewed_candidate_tree" in acceptance:
            boundary["candidate_tree"] = str(acceptance["reviewed_candidate_tree"])
        for key in ("effective_revision", "parent_revision", "ticket_graph_revision"):
            if key in job:
                boundary[key] = job[key]
        if "ticket_completion_records" in job:
            boundary["ticket_completion_records_fingerprint"] = canonical_fingerprint(
                job["ticket_completion_records"]
            )
        return invocation_event_recorder(
            state,
            role=role,
            phase=phase,
            work_subject=work_subject,
            generation=generation,
            invocation_input=request,
            currentness_boundary=boundary,
            save=self.contract.save,
        )

    @staticmethod
    def _invocation_identity(
        state: dict[str, Any], job: dict[str, Any]
    ) -> tuple[str, int]:
        if isinstance(job.get("ticket_number"), int):
            return (
                f"ticket:{int(job['ticket_number'])}",
                int(job.get("ticket_branch_generation", 1)),
            )
        if isinstance(job.get("repair_generation"), int):
            return f"run-repair:{state['run_id']}", int(job["repair_generation"])
        return f"parent-only:{state['run_id']}", int(job.get("parent_generation", 1))

    def _review(
        self, state: dict[str, Any], job: dict[str, Any], checkout: Path
    ) -> None:
        attempt = int(job.get("validation_attempts", 0)) + 1
        job["validation_attempts"] = attempt
        job["phase"] = "reviewing"
        self.contract.save(state)
        validation = (
            checkout.parent / f"validation-{self.contract.label(job)}-{attempt}"
        )
        try:
            self.contract.prepare_validation(checkout, job, validation)
            request = self.contract.review_request(state, job, validation)
            request["_invocation_event"] = self._invocation_events(
                state, job, request, role="fresh_acceptance", phase="reviewing"
            )
            request["_currentness_check"] = lambda: self._agent_is_current(state, job)
            if job.get("review_new_thread") is True:
                request["_invocation_mode"] = "new-thread"
            elif job.get("review_failure_resume") is True:
                request["_invocation_mode"] = "resume"
            review = self.agents.review(request)
        finally:
            self.git.remove_worktree(validation)
        clear_initial_credential_wait(state, work_subject=self.contract.label(job))
        _record_reviewer(
            job, review.thread_id, new_thread=job.get("review_new_thread") is True
        )
        job.pop("review_new_thread", None)
        job.pop("review_failure_resume", None)
        job.pop("review_resume_thread_id", None)
        job.pop("review_human_blocker_resume", None)
        # Persist the identity before parsing the Artifact.  A malformed
        # reviewer response must not make the same Reviewer appear fresh on
        # resume.
        self.contract.save(state)
        self._reject_stale(
            state,
            job,
            checkout,
            "Fresh Acceptance was discarded after requirements changed",
        )
        artifact = AcceptanceArtifact.parse(review.artifact)
        job["acceptance_artifact"] = artifact.raw
        job["acceptance_record"] = self.contract.acceptance_record(
            state, job, review.thread_id, artifact.raw
        )
        # A first rejection has no PR to expose.  For a repair after a PR
        # already exists, update that PR's one status comment immediately so
        # it cannot keep advertising an obsolete passing Candidate.
        existing_pr = job.get("pr_number")
        if artifact.requires_human:
            self._wait_for_human(
                state,
                job,
                phase="candidate",
                blockers=artifact.blocker_evidence,
                code="reviewer_requires_human",
            )
            return
        clear_current_human_blocker(job)
        if isinstance(existing_pr, int):
            self._record_agent_run_status(
                existing_pr,
                job,
                "not_checked",
                next_action=(
                    "repair Fresh Validation findings"
                    if artifact.has_failures
                    else (
                        "await human decision"
                        if artifact.requires_human
                        else "generate publication narrative"
                    )
                ),
            )
        if artifact.is_accepted:
            job["phase"] = "accepted"
        elif int(job["modification_attempts"]) >= MAX_MODIFICATION_ATTEMPTS:
            job["phase"] = "escalating"
            job["escalation_code"] = (
                "reviewer_requires_human"
                if artifact.requires_human
                else "modification_budget_exhausted"
            )
        else:
            job["repair_source"] = "acceptance"
            job["phase"] = "repairing"
        self.contract.save(state)

    def _wait_for_human(
        self,
        state: dict[str, Any],
        job: dict[str, Any],
        *,
        phase: str,
        blockers: tuple[str, ...],
        code: str = "agent_requires_human",
    ) -> None:
        append_human_blocker_history(job, phase=phase, blockers=blockers)
        job.update(
            {
                "human_blockers": list(blockers),
                "human_blocker_phase": phase,
                "phase": "blocked",
                "blocked_reason": code,
            }
        )
        state.update(
            {
                "status": "ready_for_human",
                "terminal_kind": "waiting_human",
                "diagnostics": [
                    {
                        "code": code,
                        "message": blocker,
                    }
                    for blocker in blockers
                ],
            }
        )
        self.contract.save(state)

    def _publish_and_merge(
        self, state: dict[str, Any], job: dict[str, Any], checkout: Path
    ) -> bool:
        if job.get("phase") != "merging":
            self._reject_stale(
                state,
                job,
                checkout,
                "Published-Head Gate rejected stale requirements",
            )
        publication = _mapping(job, "publication")
        branch = self.contract.branch(job)
        existing_pr = job.get("pr_number")
        if isinstance(existing_pr, int):
            existing_live = self.github.live_pull_request(existing_pr)
            if existing_live.get("state") == "MERGED":
                if job.get("phase") != "merging":
                    return self._block(
                        state,
                        job,
                        "unexpected_external_merge",
                        "Change Job PR merged without a persisted Publisher merge intent",
                    )
                integrated = existing_live.get("integrated_sha")
                if not isinstance(integrated, str) or not integrated:
                    raise ValueError("merged Change Job PR is missing integrated SHA")
                job["integrated_sha"] = integrated
                self.github.sync_run_branch(
                    run_branch=self.contract.base_branch(state),
                    integrated_sha=integrated,
                )
                if not self.contract.after_merge(state, job, existing_live):
                    return True
                job["phase"] = "completed"
                self.contract.save(state)
                return True
            if existing_live.get("state") not in {None, "OPEN"}:
                return self._block(
                    state,
                    job,
                    "ticket_pr_closed_unmerged",
                    "Current Change Job PR was closed without merging",
                )
            if job.get("phase") == "merging" and isinstance(
                job.get("integrated_sha"), str
            ):
                state["status"] = "waiting_merge"
                self.contract.save(state)
                return True
        self._reject_stale(
            state, job, checkout, "Published-Head Gate rejected stale requirements"
        )
        self.github.verify_ticket_pr_before_publish(
            branch=branch,
            base_branch=self.contract.base_branch(state),
            expected_head_sha=str(job.get("published_sha", job["base_sha"])),
            expected_base_sha=str(job["base_sha"]),
        )
        publish_intent = {
            "action": "publish_ticket_ref",
            "branch": branch,
            "expected_remote_sha": str(job.get("published_sha", job["base_sha"])),
            "head_sha": str(job["publication_sha"]),
        }
        if job.get("ticket_write_intent") != publish_intent:
            job["ticket_write_intent"] = publish_intent
            self.contract.save(state)
        self.github.publish_branch(
            branch,
            str(job["publication_sha"]),
            expected_remote_sha=str(job.get("published_sha", job["base_sha"])),
        )
        job.pop("ticket_write_intent", None)
        job["published_sha"] = str(job["publication_sha"])
        self.contract.save(state)
        self._reject_stale(
            state,
            job,
            checkout,
            "Published-Head Gate rejected requirements changed during publish",
        )
        pr_intent = {
            "action": "ensure_ticket_pr",
            "branch": branch,
            "base_branch": self.contract.base_branch(state),
            "base_sha": str(job["base_sha"]),
            "head_sha": str(job["publication_sha"]),
        }
        if job.get("ticket_write_intent") != pr_intent:
            job["ticket_write_intent"] = pr_intent
            self.contract.save(state)
        pr_number = self.contract.ensure_pr(state, job, publication)
        job.pop("ticket_write_intent", None)
        job["pr_number"] = pr_number
        self.contract.save(state)
        self._reject_stale(
            state,
            job,
            checkout,
            "Published-Head Gate rejected requirements changed while creating PR",
        )
        created_live = self.github.live_pull_request(pr_number)
        if created_live.get("state") == "MERGED":
            return self._block(
                state,
                job,
                "unexpected_external_merge",
                "Change Job PR merged without a persisted Publisher merge intent",
            )
        if created_live.get("state") not in {None, "OPEN"}:
            return self._block(
                state,
                job,
                "ticket_pr_closed_unmerged",
                "Current Change Job PR was closed without merging",
            )
        link_display = getattr(self.github, "link_issue_branch_display", None)
        if self.contract.linked_issue_number is not None and callable(link_display):
            ensure_linked_branch_display(
                github=self.github,
                state=state,
                job=job,
                issue_number=self.contract.linked_issue_number(state, job),
                branch=branch,
                head_sha=str(job["publication_sha"]),
                save=self.contract.save,
            )
        try:
            checks = self.github.required_checks(pr_number)
        except (GitHubReadError, OSError, TimeoutError) as error:
            if isinstance(error, GitHubReadError) and not is_github_convergence_error(
                error.code
            ):
                raise
            self._record_agent_run_status(
                pr_number,
                job,
                "unavailable",
                next_action="retry Required Checks observation",
            )
            state.update(
                {
                    "status": "waiting_external",
                    "terminal_kind": "waiting_external",
                    "diagnostics": [
                        {
                            "code": "github_checks_observation_pending",
                            "message": "GitHub Required Checks read has not converged",
                            "waiting_for": f"Ticket PR #{pr_number} Required Checks observation",
                        }
                    ],
                }
            )
            self.contract.save(state)
            return True
        self._reject_stale(
            state,
            job,
            checkout,
            "Published-Head Gate rejected requirements changed while reading checks",
        )
        self._record_agent_run_status(pr_number, job, checks)
        if checks == "pending":
            job["phase"] = "waiting_checks"
            state["status"] = "waiting_checks"
            self.contract.save(state)
            return True
        if checks == "fail":
            if int(job["modification_attempts"]) >= MAX_MODIFICATION_ATTEMPTS:
                job.update(
                    {
                        "phase": "escalating",
                        "escalation_code": "modification_budget_exhausted",
                    }
                )
            else:
                job.update(
                    {
                        "phase": "repairing",
                        "repair_source": "required_checks",
                        "ci_evidence": self.github.required_check_evidence(pr_number),
                    }
                )
            self.contract.save(state)
            return False
        if checks not in {"none", "pass"}:
            raise ValueError(f"unknown Required Checks state: {checks}")
        live = self.github.live_pull_request(pr_number)
        acceptance = _mapping(job, "acceptance_record")
        if (
            live.get("head_sha") != job["publication_sha"]
            or live.get("base_branch") != self.contract.base_branch(state)
            or live.get("base_sha") != acceptance.get("reviewed_base_sha")
            or live.get("mergeable") is not True
            or acceptance.get("reviewed_candidate_sha") != job["candidate_sha"]
            or acceptance.get("reviewed_candidate_tree")
            != self.git.resolve(f"{job['publication_sha']}^{{tree}}")
            or not self.contract.acceptance_is_current(state, job, acceptance)
        ):
            self._record_agent_run_status(
                pr_number,
                job,
                checks,
                next_action="blocked: Published-Head Gate rejected live PR state",
            )
            return self._block(
                state,
                job,
                "published_head_mismatch",
                "Published-Head Gate rejected live PR state",
            )
        merge_intent = job.get("merge_intent")
        expected_intent = {
            "head_sha": str(job["publication_sha"]),
            "base_branch": self.contract.base_branch(state),
            "base_sha": str(acceptance["reviewed_base_sha"]),
            "commit_message": str(publication["commit_message"]),
        }
        if "effective_revision" in job:
            expected_intent["effective_revision"] = str(job["effective_revision"])
        if merge_intent is None:
            merge_intent = {**expected_intent, "attempts": 0}
            job["merge_intent"] = merge_intent
        elif not isinstance(merge_intent, dict) or any(
            merge_intent.get(key) != value for key, value in expected_intent.items()
        ):
            return self._block(
                state,
                job,
                "merge_intent_mismatch",
                "Persisted merge intent no longer matches the current PR boundary",
            )
        if self.contract.requires_explicit_approval(state, job):
            job["phase"] = "ready_for_approval"
            state["status"] = "parent_approval_pending"
            state["terminal_kind"] = "waiting_human"
            state["diagnostics"] = []
            self.contract.save(state)
            self._record_agent_run_status(
                pr_number,
                job,
                checks,
                next_action="await explicit maintainer approval",
            )
            return True
        job["phase"] = "merging"
        self.contract.save(state)
        self._record_agent_run_status(
            pr_number,
            job,
            checks,
            next_action="squash merge into the Run Branch",
        )
        attempts = merge_intent.get("attempts", 0)
        if type(attempts) is not int or attempts < 0:
            raise ValueError("merge intent attempts must be a non-negative integer")
        if attempts >= 3:
            return self._wait_for_merge_reconciliation(state, job)
        merge_intent["attempts"] = attempts + 1
        self.contract.save(state)
        try:
            integrated = self.github.squash_merge(
                pr_number=pr_number,
                expected_head_sha=str(job["publication_sha"]),
                run_branch=self.contract.base_branch(state),
                commit_message=str(publication["commit_message"]),
            )
        except MergeOutcomeUnknownError as error:
            history = job.setdefault("merge_reconciliation_history", [])
            if not isinstance(history, list):
                raise ValueError("merge reconciliation history must be an array")
            history.append(
                {
                    "attempt": merge_intent["attempts"],
                    "result": "outcome_unknown",
                    "message": str(error),
                }
            )
            del history[:-3]
            self.contract.save(state)
            return self._wait_for_merge_reconciliation(state, job, str(error))
        job["integrated_sha"] = integrated
        self.contract.save(state)
        self.github.sync_run_branch(
            run_branch=self.contract.base_branch(state), integrated_sha=integrated
        )
        live_after_merge = self.github.live_pull_request(pr_number)
        if live_after_merge.get("state") != "MERGED":
            state["status"] = "waiting_merge"
            self.contract.save(state)
            return True
        if not self.contract.after_merge(state, job, live_after_merge):
            return True
        job["phase"] = "completed"
        self.contract.save(state)
        return True

    def _wait_for_merge_reconciliation(
        self, state: dict[str, Any], job: dict[str, Any], message: str | None = None
    ) -> bool:
        state.update(
            {
                "status": "waiting_external",
                "terminal_kind": "waiting_external",
                "diagnostics": [
                    {
                        "code": "merge_reconciliation_pending",
                        "message": message
                        or "Merge intent reached its retry limit; waiting for GitHub reconciliation",
                        "waiting_for": "squash merge outcome",
                    }
                ],
            }
        )
        self.contract.save(state)
        return True

    def _record_agent_run_status(
        self,
        pr_number: int,
        job: dict[str, Any],
        checks: str,
        *,
        next_action: str | None = None,
    ) -> None:
        artifact = _mapping(job, "acceptance_artifact")
        raw_checks = _mapping(artifact, "checks")
        lane_statuses = {
            lane: str(_mapping(raw_checks, lane)["status"])
            for lane in ("e2e", "standards", "spec")
        }
        if next_action is None:
            if checks == "pending":
                next_action = "wait for Required Checks"
            elif checks == "fail":
                next_action = "repair failed Required Checks"
            elif checks == "not_checked":
                next_action = "verify Published-Head Gate"
            else:
                next_action = "verify Published-Head Gate"
        self.github.record_agent_run_status(
            pr_number,
            {
                "scope": self.contract.label(job),
                "base_sha": str(job["base_sha"]),
                "candidate_sha": str(job["candidate_sha"]),
                "validation_outcome": AcceptanceArtifact.parse(artifact).outcome,
                "lane_statuses": lane_statuses,
                "required_checks": checks,
                "next_action": next_action,
            },
        )

    def _publication_is_current(
        self, state: dict[str, Any], job: dict[str, Any]
    ) -> bool:
        acceptance = job.get("acceptance_record")
        return (
            isinstance(acceptance, dict)
            and self.git.resolve(self.contract.base_branch(state))
            == job.get("base_sha")
            and self.contract.acceptance_is_current(state, job, acceptance)
            and not self.contract.revision_changed(state, job)
        )

    def _agent_is_current(self, state: dict[str, Any], job: dict[str, Any]) -> bool:
        return self.git.resolve(self.contract.base_branch(state)) == job.get(
            "base_sha"
        ) and not self.contract.revision_changed(state, job)

    def _reject_stale(
        self,
        state: dict[str, Any],
        job: dict[str, Any],
        checkout: Path,
        message: str,
    ) -> None:
        if not self._agent_is_current(state, job):
            if self.contract.stale_disposition is StaleDisposition.FRESH_RUN_ACCEPTANCE:
                self._invalidate_stale(state, job, checkout)
                self.contract.save(state)
                raise _TerminalChangeJob()
            self._block(state, job, "effective_revision_mismatch", message)
            raise _TerminalChangeJob()

    def _invalidate_stale(
        self, state: dict[str, Any], job: dict[str, Any], checkout: Path
    ) -> None:
        self.contract.invalidate_stale(state, job, checkout)

    def _block(
        self, state: dict[str, Any], job: dict[str, Any], code: str, message: str
    ) -> bool:
        job.update({"phase": "blocked", "blocked_reason": code})
        state["status"] = "blocked"
        state["diagnostics"] = [
            {"code": code, "message": message, "change_job": self.contract.label(job)}
        ]
        self.contract.save(state)
        return False


class _TerminalChangeJob(Exception):
    pass


def _record_development_thread(
    job: dict[str, Any], thread_id: str, replaced_thread_id: str | None
) -> None:
    if not thread_id.strip():
        raise ValueError("Development Thread ID is empty")
    current = job.get("development_thread_id")
    if current is not None and current != thread_id:
        if replaced_thread_id != current:
            raise ValueError(
                "Development Thread changed without a verified replacement"
            )
        history = _string_list(job, "development_thread_history")
        history.append(str(current))
        job["development_thread_history"] = history
    job["development_thread_id"] = thread_id


def _record_reviewer(
    job: dict[str, Any], thread_id: str, *, new_thread: bool = False
) -> None:
    history = _string_list(job, "development_thread_history")
    development_ids = {str(job.get("development_thread_id", "")), *history}
    reviewers = _string_list(job, "reviewer_thread_ids")
    if thread_id in development_ids:
        raise ValueError("Fresh Acceptance cannot reuse the Development Thread")
    resumed = bool(
        job.get("review_human_blocker_resume")
        or job.get("review_failure_resume")
        or job.get("review_resume_thread_id")
    )
    if resumed and not new_thread and thread_id != latest_reviewer_thread(job):
        raise ValueError("Human Blocker resume requires the latest Reviewer Thread")
    if not thread_id.strip() or (thread_id in reviewers and not resumed):
        raise ValueError("Fresh Acceptance requires a new Reviewer Thread")
    if thread_id not in reviewers:
        reviewers.append(thread_id)
    job["reviewer_thread_ids"] = reviewers


def latest_reviewer_thread(subject: dict[str, Any]) -> str | None:
    """Return the Reviewer Thread eligible for a same-Thread resume."""
    resume_thread = subject.get("review_resume_thread_id")
    if isinstance(resume_thread, str) and resume_thread.strip():
        return resume_thread
    threads = subject.get("reviewer_thread_ids")
    if isinstance(threads, list) and threads and isinstance(threads[-1], str):
        return threads[-1]
    return None


def _mapping(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be an object")
    return value


def _string_list(data: dict[str, Any], key: str) -> list[str]:
    value = data.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{key} must contain strings")
    return list(value)
