from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent_run.agents import AgentBackend
from agent_run.artifacts import AcceptanceArtifact
from agent_run.agent_invocation import select_publication_thread
from agent_run.approval_grant import (
    acceptance_fingerprint,
    grant_authority,
    grant_matches,
)
from agent_run.change_delivery import (
    MAX_PUBLICATION_CONTEXT_ATTEMPTS,
    ChangeDeliveryAdapter,
    ChangeDeliveryEngine,
    ChangeDeliveryPublisher,
    ChangeDeliveryStateStore,
    ChangeJobContract,
    latest_reviewer_thread,
    StaleDisposition,
)
from agent_run.delivery_policy import parent_only_budget_policy_for_job
from agent_run.delivery_protocol import GitHubPublisher
from agent_run.git import GitError, GitRepository
from agent_run.github import MergeOutcomeUnknownError
from agent_run.human_responses import current_human_response_history
from agent_run.state import StateStore
from agent_run.review_budget import ensure_budget
from agent_run.delivery_evidence import clear_stale_delivery_evidence
from agent_run.parent_integration import parent_integration_record_is_valid


class ParentDeliveryAdapter(ChangeDeliveryAdapter):
    """Semantic Adapter for the Parent-only delivery consumer."""

    stale_disposition = StaleDisposition.BLOCK
    classify_required_check_failures = True

    def __init__(self, git: GitRepository, github: GitHubPublisher) -> None:
        self.git = git
        self.github = github

    def development_thread_is_allowed(
        self, _state: dict[str, Any], _thread_id: str
    ) -> bool:
        return True

    def base_is_current(self, current_base: str, job: dict[str, Any]) -> bool:
        if "parent_integration" in job:
            record = job.get("parent_integration")
            if job.get("parent_integration_stage") is True and isinstance(record, dict):
                # Findings repair changes the candidate before the next exact
                # integration. The prior snapshot still owns this baseline.
                return (
                    job.get("base_sha") == record.get("target_sha")
                    and parent_integration_record_is_valid(
                        self.git, {**job, "candidate_sha": record.get("head_sha")}
                    )
                )
            return parent_integration_record_is_valid(self.git, job)
        return current_base == job.get("base_sha")

    def development_request(
        self, state: dict[str, Any], job: dict[str, Any], checkout: Path
    ) -> dict[str, Any]:
        request = {
            "acceptance_scope": "parent_only",
            "parent_issue_url": _parent(state)["url"],
            "run_id": state["run_id"],
            "repository": str(state["repository"]),
            "parent": _parent(state),
            "effective_revision": job["effective_revision"],
            "base_sha": job["base_sha"],
            "head_sha": self.git.checkout_head(checkout),
            "checkout": str(checkout),
            "thread_id": (
                None
                if job.get("development_new_thread")
                else job.get("development_thread_id")
            ),
        }
        if job.get("prior_human_blockers"):
            request["prior_human_blockers"] = job["prior_human_blockers"]
        if history := current_human_response_history(
            job, generation=int(job.get("parent_generation", 1))
        ):
            request["human_response_history"] = history
        if (
            job.get("pending_attempt_kind", job.get("next_attempt_kind")) == "integration"
            and job.get("repair_source") == "acceptance"
        ):
            request["repair_source"] = "integration"
            request["work_source"] = "integration"
            request["integration_evidence"] = json.dumps(
                {
                    "acceptance_artifact": _mapping(job, "acceptance_artifact"),
                    "parent_integration": _mapping(job, "parent_integration"),
                },
                ensure_ascii=False, sort_keys=True, indent=2,
            )
        elif job.get("repair_source") == "git_integrity":
            request["repair_source"] = "git_integrity"
            request["git_integrity_evidence"] = _mapping(
                job, "git_integrity_evidence"
            )
        elif job.get("repair_source") == "acceptance":
            request["repair_source"] = "acceptance"
            request["acceptance_artifact"] = _mapping(job, "acceptance_artifact")
        elif job.get("repair_source") == "required_checks":
            request["repair_source"] = "required_checks"
            request["ci_evidence"] = _mapping(job, "ci_evidence")
        return request

    def publication_request(
        self, state: dict[str, Any], job: dict[str, Any], checkout: Path
    ) -> dict[str, Any]:
        request = {
            "acceptance_scope": "parent_only",
            "parent_issue_url": _parent(state)["url"],
            "run_id": state["run_id"],
            "repository": str(state["repository"]),
            "parent": _parent(state),
            "effective_revision": job["effective_revision"],
            "base_sha": job["base_sha"],
            "candidate_sha": job["candidate_sha"],
            "checkout": str(checkout),
            "thread_id": select_publication_thread(
                job, max_context_attempts=MAX_PUBLICATION_CONTEXT_ATTEMPTS
            ),
            "acceptance_artifact": _mapping(job, "acceptance_artifact"),
        }
        if job.get("prior_human_blockers"):
            request["prior_human_blockers"] = job["prior_human_blockers"]
        if history := current_human_response_history(
            job, generation=int(job.get("parent_generation", 1))
        ):
            request["human_response_history"] = history
        existing_pr = job.get("pr_number")
        if isinstance(existing_pr, int):
            request["existing_pr"] = self.github.publication_context(existing_pr)
        if job.get("repair_source") == "required_checks":
            request["ci_evidence"] = _mapping(job, "ci_evidence")
        if isinstance(job.get("parent_integration"), dict):
            request["integration_record"] = job["parent_integration"]
        return request

    def review_request(
        self, state: dict[str, Any], job: dict[str, Any], checkout: Path
    ) -> dict[str, Any]:
        request = {
            "acceptance_scope": "parent_only",
            "parent_issue_url": _parent(state)["url"],
            "run_id": state["run_id"],
            "repository": str(state["repository"]),
            "parent": _parent(state),
            "base_sha": job["base_sha"],
            "candidate_sha": job["candidate_sha"],
            "current_review_identity": self.review_identity(state, job),
            "effective_revision": job["effective_revision"],
            "checkout": str(checkout),
            "thread_id": (
                latest_reviewer_thread(job)
                if (
                    (
                        job.get("review_human_blocker_resume")
                        or job.get("review_resume_thread_id")
                    )
                    and not job.get("review_new_thread")
                )
                else None
            ),
        }
        if job.get("prior_human_blockers"):
            request["prior_human_blockers"] = job["prior_human_blockers"]
        if history := current_human_response_history(
            job, generation=int(job.get("parent_generation", 1))
        ):
            request["human_response_history"] = history
        previous = self.compatible_review_baseline(state, job)
        if previous is not None:
            request["previous_acceptance_artifact"] = previous["artifact"]
            request["previous_review_identity"] = {
                key: previous[key] for key in request["current_review_identity"]
            }
        if isinstance(job.get("parent_integration"), dict):
            request["work_source"] = "integration"
            request["integration_record"] = job["parent_integration"]
        return request

    def review_identity(
        self, state: dict[str, Any], job: dict[str, Any]
    ) -> dict[str, Any]:
        return {
            "acceptance_scope": "parent_only",
            "parent_number": _mapping(state, "parent")["number"],
            "effective_revision": job["effective_revision"],
            "reviewed_base_sha": job["base_sha"],
            "reviewed_candidate_sha": job["candidate_sha"],
            "reviewed_candidate_tree": self.git.resolve(
                f"{job['candidate_sha']}^{{tree}}"
            ),
            "expected_merge_tree": self.git.expected_merge_tree(
                default_head_sha=str(job["base_sha"]),
                run_head_sha=str(job["candidate_sha"]),
            ),
        }

    def compatible_review_baseline(
        self, state: dict[str, Any], job: dict[str, Any]
    ) -> dict[str, Any] | None:
        for key in ("acceptance_record", "previous_parent_acceptance_record"):
            record = job.get(key)
            if not isinstance(record, dict) or (
                record.get("acceptance_scope") != "parent_only"
                or record.get("parent_number") != _mapping(state, "parent")["number"]
                or record.get("effective_revision") != job.get("effective_revision")
                or self.revision_changed(state, job)
            ):
                continue
            identity_fields = (
                "reviewed_base_sha", "reviewed_candidate_sha",
                "reviewed_candidate_tree", "expected_merge_tree", "reviewer_thread_id",
            )
            if not all(
                isinstance(record.get(field), str) and record[field]
                for field in identity_fields
            ):
                continue
            try:
                artifact = AcceptanceArtifact.parse(record.get("artifact"))
            except ValueError:
                continue
            if not any(
                check["status"] == "blocked" for check in artifact.checks.values()
            ):
                return record
        return None

    def acceptance_record(
        self,
        state: dict[str, Any],
        job: dict[str, Any],
        reviewer_thread_id: str,
        artifact: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            **self.review_identity(state, job),
            "reviewer_thread_id": reviewer_thread_id,
            "artifact": artifact,
        }

    def acceptance_is_current(
        self, state: dict[str, Any], job: dict[str, Any], acceptance: dict[str, Any]
    ) -> bool:
        if self.revision_changed(state, job):
            return False
        try:
            artifact = AcceptanceArtifact.parse(acceptance.get("artifact"))
            current = self.review_identity(state, job)
        except (GitError, ValueError, KeyError):
            return False
        return (
            artifact.is_accepted
            and isinstance(acceptance.get("reviewer_thread_id"), str)
            and bool(acceptance["reviewer_thread_id"].strip())
            and acceptance.get("artifact") == job.get("acceptance_artifact")
            and all(acceptance.get(key) == value for key, value in current.items())
        )

    def revision_changed(self, state: dict[str, Any], job: dict[str, Any]) -> bool:
        return _mapping(state, "parent").get("revision") != job.get(
            "effective_revision"
        )

    def requires_explicit_approval(
        self, state: dict[str, Any], job: dict[str, Any]
    ) -> bool:
        return not grant_matches(
            job.get("approval_grant"), parent_approval_grant_authority(state, job)
        )

    def resume_after_required_checks_failure(
        self, state: dict[str, Any], job: dict[str, Any]
    ) -> bool:
        job.pop("approval_grant", None)
        state.update(
            {
                "status": "parent_delivery_pending",
                "terminal_kind": None,
                "diagnostics": [],
            }
        )
        return True

    def linked_issue_number(
        self, state: dict[str, Any], _job: dict[str, Any]
    ) -> int:
        return int(_mapping(state, "parent")["number"])

    def invocation_identity(
        self, state: dict[str, Any], job: dict[str, Any]
    ) -> tuple[str, int]:
        return ChangeDeliveryEngine._invocation_identity(state, job)


class ParentDeliveryPublisher(ChangeDeliveryPublisher):
    """Parent-only mutations behind the shared Publisher seam."""

    def __init__(self, owner: ParentDeliveryLoop) -> None:
        self.owner = owner

    def commit_candidate(
        self, checkout: Path, job: dict[str, Any], attempt: int
    ) -> str | None:
        return self.owner.git.commit_candidate(
            checkout,
            ticket_number=0,
            attempt=attempt,
            expected_head=str(
                job.get("managed_checkout_head")
                or job.get("candidate_sha")
                or job["base_sha"]
            ),
            candidate_intent=(
                job.get("candidate_commit_intent")
                if isinstance(job.get("candidate_commit_intent"), dict)
                else None
            ),
        )

    def create_publication_commit(
        self, checkout: Path, job: dict[str, Any], message: str
    ) -> str:
        record = job.get("parent_integration")
        source = str(record["source_sha"]) if isinstance(record, dict) else None
        published = job.get("published_sha")
        if isinstance(published, str) and (
            source is None or self.owner.git.is_ancestor(source, published)
        ):
            source = published
        return self.owner.git.create_publication_commit(
            checkout,
            candidate_sha=str(job["candidate_sha"]),
            base_sha=str(job["base_sha"]),
            published_sha=source,
            message=message,
        )

    def prepare_validation(
        self, _checkout: Path, job: dict[str, Any], validation: Path
    ) -> None:
        self.owner.git.prepare_validation_checkout(
            head_sha=str(job["candidate_sha"]), checkout=validation
        )

    def ensure_pr(
        self,
        state: dict[str, Any],
        job: dict[str, Any],
        publication: dict[str, Any],
    ) -> int:
        return self.owner.github.ensure_change_pr(
            branch=str(job["parent_branch"]),
            base_branch=str(_mapping(state, "base")["branch"]),
            title=str(publication["pr_title"]),
            body=self.owner._render_pr_body(state, publication),
            expected_head_sha=str(job["publication_sha"]),
            expected_base_sha=str(job["base_sha"]),
        )

    def live_pull_request(
        self, _state: dict[str, Any], _job: dict[str, Any], pr_number: int
    ) -> dict[str, Any]:
        return self.owner.github.live_pull_request(pr_number)

    def invalidate_stale(
        self, state: dict[str, Any], job: dict[str, Any], checkout: Path
    ) -> None:
        self.owner._invalidate_stale(state, job, checkout)

    def after_merge(
        self, state: dict[str, Any], job: dict[str, Any], live: dict[str, Any]
    ) -> bool:
        return self.owner._complete_after_merge(state, job, live)

    def merge(
        self, state: dict[str, Any], job: dict[str, Any], _publication: dict[str, Any]
    ) -> str:
        try:
            base = _mapping(state, "base")
            repository = str(state["repository"])
            return self.owner.github.normal_merge(
                pr_number=int(job["pr_number"]),
                expected_head_sha=str(job["publication_sha"]),
                expected_head_branch=str(job["parent_branch"]),
                expected_head_repository=repository,
                expected_base_branch=str(base["branch"]),
                expected_base_sha=str(job["base_sha"]),
                expected_base_repository=repository,
            )
        except OSError as error:
            raise MergeOutcomeUnknownError(str(error)) from error

    def merge_description(self, _job: dict[str, Any]) -> str:
        return "normal merge"

    def escalate(
        self, state: dict[str, Any], job: dict[str, Any], code: str
    ) -> None:
        self.owner._escalate(state, job, code)

    def sync_attempts(self, _state: dict[str, Any], _job: dict[str, Any]) -> None:
        return None


class ParentDeliveryLoop:
    """Adapt the shared change lifecycle to the Parent-only delivery boundary."""

    def __init__(
        self,
        *,
        git: GitRepository,
        states: StateStore,
        github: GitHubPublisher,
        agents: AgentBackend,
    ) -> None:
        self.git = git
        self.states = states
        self.github = github
        self.agents = agents
        self.adapter = ParentDeliveryAdapter(git, github)
        self.publisher = ParentDeliveryPublisher(self)

    def run(
        self, state: dict[str, Any], job: dict[str, Any], checkout: Path
    ) -> dict[str, Any]:
        return self._engine(state, job).run(state, job, checkout)

    def _engine(
        self, state: dict[str, Any], job: dict[str, Any]
    ) -> ChangeDeliveryEngine:
        return ChangeDeliveryEngine(
            git=self.git,
            github=self.github,
            agents=self.agents,
            contract=ChangeJobContract(
                label="parent-only",
                branch=str(job["parent_branch"]),
                base_branch=str(_mapping(state, "base")["branch"]),
                review_budget_policy=parent_only_budget_policy_for_job(
                    job, state_snapshot=state.get("policy_snapshot")
                ),
            ),
            adapter=self.adapter,
            publisher=self.publisher,
            state_store=ChangeDeliveryStateStore(self.states, str(state["run_id"])),
        )

    @staticmethod
    def _render_pr_body(state: dict[str, Any], publication: dict[str, Any]) -> str:
        parent = _mapping(state, "parent")
        return (
            f"Parent Issue: #{int(parent['number'])}\n"
            "Delivery Type: Parent-only\n\n"
            f"{str(publication['pr_body_markdown']).strip()}"
        )

    def _invalidate_stale(
        self, state: dict[str, Any], job: dict[str, Any], checkout: Path
    ) -> None:
        base_sha = str(_mapping(state, "base")["sha"])
        self.git.reset_checkout_to_base(checkout, base_sha)
        clear_stale_delivery_evidence(job)
        job.pop("approval_grant", None)
        job.update(
            {
                "base_sha": base_sha,
                "phase": "developing",
                "validation_attempts": 0,
            }
        )
        state["status"] = "parent_delivery_pending"
        state["diagnostics"] = []

    def _complete_after_merge(
        self, state: dict[str, Any], job: dict[str, Any], live: dict[str, Any]
    ) -> bool:
        base_sha = str(job["base_sha"])
        publication_sha = str(job["publication_sha"])
        integrated = live.get("integrated_sha")
        if (
            not isinstance(integrated, str)
            or live.get("head_sha") != publication_sha
            or live.get("base_branch") != _mapping(state, "base").get("branch")
            or live.get("integrated_parents") != [base_sha, publication_sha]
        ):
            job.update(
                {
                    "phase": "blocked",
                    "blocked_reason": "parent_merged_result_mismatch",
                }
            )
            state["status"] = "blocked"
            state["diagnostics"] = [
                {
                    "code": "parent_merged_result_mismatch",
                    "message": "Merged Parent PR does not match the approved publication",
                }
            ]
            self._save(state)
            return False
        job["integrated_sha"] = integrated
        self._save(state)
        if self.adapter.revision_changed(state, job):
            # The approved merge is irreversible. Preserve its evidence and
            # pause instead of closing a Parent whose requirements changed.
            job.update({
                "phase": "blocked", "blocked_reason": "effective_revision_mismatch",
            })
            state["status"] = "blocked"
            state["diagnostics"] = [{
                "code": "effective_revision_mismatch",
                "message": (
                    "Parent requirements changed after merge; merged facts are preserved"
                ),
            }]
            self._save(state)
            return False

        def record_close_intent(intent: dict[str, Any]) -> None:
            job["parent_close_intent"] = intent
            self._save(state)

        self.github.close_parent_issue(
            parent_number=int(_mapping(state, "parent")["number"]),
            run_id=str(state["run_id"]),
            pr_number=int(job["pr_number"]),
            integrated_sha=integrated,
            delivery_type="Parent-only",
            close_intent=job.get("parent_close_intent"),
            record_intent=record_close_intent,
        )
        job["phase"] = "completed"
        state["status"] = "completed"
        state["terminal_kind"] = "completed"
        state["diagnostics"] = []
        self._save(state)
        return True

    @staticmethod
    def _escalate(state: dict[str, Any], job: dict[str, Any], code: str) -> None:
        job["blocked_reason"] = code
        if code == "review_budget_exhausted":
            budget = ensure_budget(
                job,
                parent_only_budget_policy_for_job(
                    job, state_snapshot=state.get("policy_snapshot")
                ),
            )
            budget["checkpoint_reason"] = code
        state["status"] = "blocked"
        state["diagnostics"] = [{"code": code, "message": "Parent Issue requires explicit human intervention"}]

    def _save(self, state: dict[str, Any]) -> dict[str, Any]:
        self.states.save_run(str(state["run_id"]), state)
        return state


def _mapping(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be an object")
    return value


def parent_approval_grant_authority(
    state: dict[str, Any], job: dict[str, Any]
) -> dict[str, object]:
    return grant_authority(
        repository=str(state["repository"]),
        pr_number=int(job["pr_number"]),
        head_branch=str(job["parent_branch"]),
        head_sha=str(job["publication_sha"]),
        base_branch=str(_mapping(state, "base")["branch"]),
        base_sha=str(job["base_sha"]),
        acceptance_fingerprint=acceptance_fingerprint(
            _mapping(job, "acceptance_record"),
            _mapping(job, "acceptance_artifact"),
        ),
    )


def _parent(state: dict[str, Any]) -> dict[str, Any]:
    parent = dict(_mapping(state, "parent"))
    parent["url"] = (
        f"https://github.com/{state['repository']}/issues/{parent['number']}"
    )
    return parent
