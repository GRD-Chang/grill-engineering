from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_run.agents import AgentBackend
from agent_run.agent_invocation import select_publication_thread
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
from agent_run.delivery_protocol import GitHubPublisher
from agent_run.git import GitRepository
from agent_run.human_responses import current_human_response_history
from agent_run.state import StateStore
from agent_run.review_budget import RUN_POLICY, ensure_budget, previous_review_context


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
        return current_base == job.get("base_sha")

    def development_request(
        self, state: dict[str, Any], job: dict[str, Any], checkout: Path
    ) -> dict[str, Any]:
        request = {
            "acceptance_scope": "parent_only",
            "parent_issue_url": _parent(state)["url"],
            "run_id": state["run_id"],
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
        if job.get("repair_source") == "git_integrity":
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
        return request

    def review_request(
        self, state: dict[str, Any], job: dict[str, Any], checkout: Path
    ) -> dict[str, Any]:
        request = {
            "acceptance_scope": "parent_only",
            "parent_issue_url": _parent(state)["url"],
            "run_id": state["run_id"],
            "parent": _parent(state),
            "base_sha": job["base_sha"],
            "candidate_sha": job["candidate_sha"],
            "current_review_identity": {
                "reviewed_base_sha": str(job["base_sha"]),
                "reviewed_candidate_sha": str(job["candidate_sha"]),
                "reviewed_candidate_tree": self.git.resolve(
                    f"{job['candidate_sha']}^{{tree}}"
                ),
            },
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
        previous = previous_review_context(job)
        if previous is not None:
            request["previous_acceptance_artifact"] = previous["artifact"]
            request["previous_review_identity"] = previous["identity"]
        return request

    def acceptance_record(
        self,
        state: dict[str, Any],
        job: dict[str, Any],
        reviewer_thread_id: str,
        artifact: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "acceptance_scope": "parent_only",
            "reviewed_base_sha": job["base_sha"],
            "reviewed_candidate_sha": job["candidate_sha"],
            "reviewed_candidate_tree": self.git.resolve(
                f"{job['candidate_sha']}^{{tree}}"
            ),
            "effective_revision": job["effective_revision"],
            "reviewer_thread_id": reviewer_thread_id,
            "artifact": artifact,
        }

    def acceptance_is_current(
        self, state: dict[str, Any], job: dict[str, Any], acceptance: dict[str, Any]
    ) -> bool:
        return (
            acceptance.get("reviewed_base_sha") == job.get("base_sha")
            and acceptance.get("reviewed_candidate_sha") == job.get("candidate_sha")
            and acceptance.get("reviewed_candidate_tree")
            == self.git.resolve(f"{job['candidate_sha']}^{{tree}}")
            and not self.revision_changed(state, job)
        )

    def revision_changed(self, state: dict[str, Any], job: dict[str, Any]) -> bool:
        return _mapping(state, "parent").get("revision") != job.get(
            "effective_revision"
        )

    def requires_explicit_approval(
        self, _state: dict[str, Any], _job: dict[str, Any]
    ) -> bool:
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
        return self.owner.git.create_publication_commit(
            checkout,
            candidate_sha=str(job["candidate_sha"]),
            base_sha=str(job["base_sha"]),
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
        self, _state: dict[str, Any], _job: dict[str, Any], _live: dict[str, Any]
    ) -> bool:
        return True

    def merge(
        self, state: dict[str, Any], job: dict[str, Any], publication: dict[str, Any]
    ) -> str:
        return self.owner.github.squash_merge(
            pr_number=int(job["pr_number"]),
            expected_head_sha=str(job["publication_sha"]),
            run_branch=str(_mapping(state, "base")["branch"]),
            commit_message=str(publication["commit_message"]),
        )

    def merge_description(self, _job: dict[str, Any]) -> str:
        return "squash merge"

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
        base_branch = str(_mapping(state, "base")["branch"])
        base_sha = self.git.resolve(base_branch)
        self.git.reset_checkout_to_base(checkout, base_branch)
        for key in (
            "candidate_sha",
            "publication",
            "publication_sha",
            "acceptance_artifact",
            "acceptance_record",
            "publication_attempts",
            "publication_thread_id",
            "last_publication_error",
            "approval_grant",
            "repair_source",
            "ci_evidence",
            "publication_authority",
            "fallback_publication_receipt",
            "deterministic_integration_record",
            "required_checks",
            "required_checks_mode",
            "next_attempt_kind",
            "last_review_candidate_sha",
            "final_ci_fix_failure_head",
        ):
            job.pop(key, None)
        job.update(
            {
                "base_sha": base_sha,
                "phase": "developing",
                "validation_attempts": 0,
            }
        )
        state["status"] = "parent_delivery_pending"
        state["diagnostics"] = []

    @staticmethod
    def _escalate(state: dict[str, Any], job: dict[str, Any], code: str) -> None:
        job["blocked_reason"] = code
        if code == "review_budget_exhausted":
            budget = ensure_budget(job, RUN_POLICY)
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


def _parent(state: dict[str, Any]) -> dict[str, Any]:
    parent = dict(_mapping(state, "parent"))
    parent["url"] = (
        f"https://github.com/{state['repository']}/issues/{parent['number']}"
    )
    return parent
