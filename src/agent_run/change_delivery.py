from __future__ import annotations

"""Shared, deterministic delivery lifecycle for a change job.

The controller owns this lifecycle.  A job-specific contract supplies only
identity, request construction, and the one completion side effect (closing a
Ticket or returning control to Run Acceptance).  In particular, contracts do
not get a shortcut from development to a Run Branch write.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from agent_run.agents import AgentBackend, PublicationResult
from agent_run.artifacts import AcceptanceArtifact, PublicationArtifact
from agent_run.delivery_protocol import GitHubPublisher
from agent_run.git import GitRepository
from agent_run.github import GitHubReadError
from agent_run.publication_pending import publication_pending_diagnostic

MAX_MODIFICATION_ATTEMPTS = 10
MAX_PUBLICATION_CONTEXT_ATTEMPTS = 4
MAX_PUBLICATION_ATTEMPTS = MAX_PUBLICATION_CONTEXT_ATTEMPTS + 1


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
    invalidate_stale_publication: Callable[
        [dict[str, Any], dict[str, Any], Path], None
    ]
    revision_changed: Callable[[dict[str, Any], dict[str, Any]], bool]
    requires_explicit_approval: Callable[[dict[str, Any], dict[str, Any]], bool]
    after_merge: Callable[[dict[str, Any], dict[str, Any], dict[str, Any]], bool]
    escalate: Callable[[dict[str, Any], dict[str, Any], str], None]
    save: Callable[[dict[str, Any]], dict[str, Any]]


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
                    terminal = self._publish_and_merge(state, job)
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

    def _develop(
        self, state: dict[str, Any], job: dict[str, Any], checkout: Path
    ) -> None:
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
        result = self.agents.develop(
            self.contract.development_request(state, job, checkout)
        )
        if not result.thread_id.strip() or not result.summary.strip():
            raise ValueError("Development result is incomplete")
        if not self.contract.development_thread_is_allowed(state, result.thread_id):
            raise ValueError("Change Job Development Thread is not independent")
        _record_development_thread(job, result.thread_id, result.replaced_thread_id)
        job["development_summary"] = result.summary
        job["phase"] = "committing_candidate"
        self.contract.save(state)
        self._reject_stale(
            state, job, "Development result was discarded after requirements changed"
        )

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
                self.contract.invalidate_stale_publication(state, job, checkout)
                self.contract.save(state)
                return
            try:
                raw = self.agents.publication(
                    self.contract.publication_request(state, job, checkout)
                )
                if isinstance(raw, PublicationResult):
                    if raw.replaced_thread_id is not None:
                        if not self.contract.development_thread_is_allowed(
                            state, raw.thread_id
                        ):
                            raise ValueError(
                                "Change Job Development Thread is not independent"
                            )
                        _record_development_thread(
                            job, raw.thread_id, raw.replaced_thread_id
                        )
                    elif raw.thread_id != job.get("development_thread_id"):
                        job["publication_thread_id"] = raw.thread_id
                    artifact_data = raw.artifact
                else:
                    artifact_data = raw
                if isinstance(job.get("ticket_number"), int):
                    publication = PublicationArtifact.parse(
                        artifact_data, primary_ticket=int(job["ticket_number"])
                    )
                else:
                    publication = PublicationArtifact.parse(
                        artifact_data, delivery_run=str(job["run_id"])
                    )
            except Exception as error:
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
            break
        if not self._publication_is_current(state, job):
            self.contract.invalidate_stale_publication(state, job, checkout)
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
                "publication_attempts": int(job.get("publication_attempts", 0))
                + 1,
                "publication_sha": sha,
                "phase": "publishing",
            }
        )
        job.pop("last_publication_error", None)
        self.contract.save(state)
        self._reject_stale(
            state, job, "Publication was discarded after requirements changed"
        )

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
            review = self.agents.review(
                self.contract.review_request(state, job, validation)
            )
        finally:
            self.git.remove_worktree(validation)
        _record_reviewer(job, review.thread_id)
        # Persist the identity before parsing the Artifact.  A malformed
        # reviewer response must not make the same Reviewer appear fresh on
        # resume.
        self.contract.save(state)
        self._reject_stale(
            state, job, "Fresh Acceptance was discarded after requirements changed"
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
        if isinstance(existing_pr, int):
            self._record_agent_run_status(
                existing_pr,
                job,
                "not_checked",
                next_action=(
                    "repair Fresh Validation findings"
                    if artifact.verdict == "request_changes"
                    else "await human decision"
                    if artifact.verdict == "human"
                    else "generate publication narrative"
                ),
            )
        if artifact.verdict == "pass":
            job["phase"] = "accepted"
        elif (
            artifact.verdict == "human"
            or int(job["modification_attempts"]) >= MAX_MODIFICATION_ATTEMPTS
        ):
            job["phase"] = "escalating"
            job["escalation_code"] = (
                "reviewer_requires_human"
                if artifact.verdict == "human"
                else "modification_budget_exhausted"
            )
        else:
            job["repair_source"] = "acceptance"
            job["phase"] = "repairing"
        self.contract.save(state)

    def _publish_and_merge(self, state: dict[str, Any], job: dict[str, Any]) -> bool:
        if job.get("phase") != "merging":
            self._reject_stale(
                state, job, "Published-Head Gate rejected stale requirements"
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
            state, job, "Published-Head Gate rejected stale requirements"
        )
        self.github.publish_branch(
            branch,
            str(job["publication_sha"]),
            expected_remote_sha=str(job.get("published_sha", job["base_sha"])),
        )
        job["published_sha"] = str(job["publication_sha"])
        self.contract.save(state)
        self._reject_stale(
            state,
            job,
            "Published-Head Gate rejected requirements changed during publish",
        )
        pr_number = self.contract.ensure_pr(state, job, publication)
        job["pr_number"] = pr_number
        self.contract.save(state)
        self._reject_stale(
            state,
            job,
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
        try:
            checks = self.github.required_checks(pr_number)
        except (GitHubReadError, OSError, TimeoutError):
            self._record_agent_run_status(
                pr_number,
                job,
                "unavailable",
                next_action="retry Required Checks observation",
            )
            job["phase"] = "waiting_checks"
            state["status"] = "waiting_checks"
            self.contract.save(state)
            return True
        self._reject_stale(
            state,
            job,
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
        job["merge_intent"] = {
            "head_sha": str(job["publication_sha"]),
            "base_branch": self.contract.base_branch(state),
            "base_sha": str(acceptance["reviewed_base_sha"]),
            "commit_message": str(publication["commit_message"]),
        }
        if "effective_revision" in job:
            job["merge_intent"]["effective_revision"] = str(job["effective_revision"])
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
        integrated = self.github.squash_merge(
            pr_number=pr_number,
            expected_head_sha=str(job["publication_sha"]),
            run_branch=self.contract.base_branch(state),
            commit_message=str(publication["commit_message"]),
        )
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
                "validation_verdict": str(artifact["verdict"]),
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

    def _reject_stale(
        self, state: dict[str, Any], job: dict[str, Any], message: str
    ) -> None:
        if self.contract.revision_changed(state, job):
            self._block(state, job, "effective_revision_mismatch", message)
            raise _TerminalChangeJob()

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


def _record_reviewer(job: dict[str, Any], thread_id: str) -> None:
    history = _string_list(job, "development_thread_history")
    development_ids = {str(job.get("development_thread_id", "")), *history}
    reviewers = _string_list(job, "reviewer_thread_ids")
    if thread_id in development_ids:
        raise ValueError("Fresh Acceptance cannot reuse the Development Thread")
    if not thread_id.strip() or thread_id in reviewers:
        raise ValueError("Fresh Acceptance requires a new Reviewer Thread")
    reviewers.append(thread_id)
    job["reviewer_thread_ids"] = reviewers


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
