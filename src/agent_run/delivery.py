from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_run.agents import AgentBackend
from agent_run.change_delivery import ensure_change_branch_authority
from agent_run.delivery_cleanup import DeliveryCleanupEngine
from agent_run.delivery_loop import (
    TicketDeliveryLoop,
    _record_superseded_integration,
)
from agent_run.delivery_protocol import GitHubPublisher
from agent_run.git import GitRepository
from agent_run.revisions import effective_revision
from agent_run.review_budget import new_budget
from agent_run.delivery_policy import policy_snapshot_for_state
from agent_run.ticket_publication_contract import (
    require_active_ticket_publication_authorization,
)
from agent_run.state import StateStore
from agent_run.ticket_phase import (
    BLOCKED_MESSAGES,
    TicketPhase,
    parse_ticket_phase,
    sync_active_ticket_job,
)


class TicketDeliveryEngine:
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

    def deliver(self, run_id: str) -> dict[str, Any]:
        with self.states.locked():
            state = self.states.load_current_run(run_id)
            if state is None:
                raise ValueError(f"unknown Delivery Run: {run_id}")
            job = self._job(state)
            if state.get("status") in {"blocked", "requeue_required"}:
                return state
            if job.get("phase") in {
                "accepted",
                "publication_pending",
                "publishing",
                "waiting_checks",
                "waiting_merge",
                "merging",
            }:
                candidate_sha = job.get("candidate_sha")
                if not isinstance(candidate_sha, str) or not candidate_sha.strip():
                    raise ValueError(
                        "active Ticket publication is missing candidate_sha"
                    )
                candidate_tree = self.git.resolve(f"{candidate_sha}^{{tree}}")
                require_active_ticket_publication_authorization(
                    job,
                    candidate_tree=candidate_tree,
                    location=f"ticket_jobs[{job.get('ticket_number', 'active')}]",
                )
            ticket_number = int(job["ticket_number"])
            checkout = (
                self.states.root
                / "worktrees"
                / run_id
                / f"ticket-{ticket_number}"
            )
            preserve_checkout = False
            checkout_prepared = False
            checkout_existed_before_attempt = checkout.exists()
            checkout_recoverable = self.git.ticket_checkout_matches(
                checkout, str(job["ticket_branch"])
            )
            try:
                self._ensure_ticket_branch(state, job)
                self.git.prepare_ticket_checkout(
                    branch=str(job["ticket_branch"]),
                    base_sha=str(job["base_sha"]),
                    checkout=checkout,
                )
                checkout_prepared = True
                result = TicketDeliveryLoop(
                    git=self.git,
                    states=self.states,
                    github=self.github,
                    agents=self.agents,
                ).run(state, job, checkout)
                if job.get("phase") == TicketPhase.COMPLETED.value:
                    result = DeliveryCleanupEngine(
                        git=self.git, states=self.states, github=self.github
                    ).complete_ticket(result, job)
                preserve_checkout = result.get("status") in {
                    "waiting_checks",
                    "waiting_external",
                    "requeue_required",
                    "blocked",
                    "ready_for_human",
                } or (
                    job.get("blocked_reason") == "agent_requires_human"
                    and job.get("human_blocker_phase")
                    in {"developing", "repairing"}
                )
                return result
            except KeyboardInterrupt:
                preserve_checkout = (
                    checkout_existed_before_attempt
                    or checkout_recoverable
                    or checkout_prepared
                )
                raise
            except BaseException:
                # Once the stable checkout is ready, any interrupted Worker or
                # Publisher phase may have recoverable uncommitted state.
                # Abrupt process exits leave it behind too, so surfaced errors
                # must preserve the same resume semantics.
                preserve_checkout = (
                    checkout_existed_before_attempt
                    or checkout_recoverable
                    or checkout_prepared
                )
                raise
            finally:
                if not preserve_checkout:
                    self.git.remove_worktree(
                        checkout,
                        discard_worktree=not (
                            checkout_existed_before_attempt
                            or checkout_recoverable
                            or checkout_prepared
                        ),
                    )
                    self._remove_empty_worktree_directories(checkout)

    def _ensure_ticket_branch(
        self, state: dict[str, Any], job: dict[str, Any]
    ) -> None:
        ensure_change_branch_authority(
            github=self.github, state=state, job=job,
            branch=str(job["ticket_branch"]), base_branch=str(state["run_branch"]),
            save=self._save, ticket_number=int(job["ticket_number"]),
        )

    def _job(self, state: dict[str, Any]) -> dict[str, Any]:
        active = _mapping(state, "active_ticket_job")
        ticket_number = active.get("ticket_number")
        if not isinstance(ticket_number, int):
            raise ValueError("active Ticket Job has invalid ticket_number")
        tickets = _mapping(_mapping(state, "ticket_graph"), "tickets")
        ticket = _mapping(tickets, str(ticket_number))
        expected_revision = _effective_revision(state, ticket)
        if "phase" in active:
            parse_ticket_phase(active["phase"])
        if "phase" not in active:
            active.update(self._new_job_fields(state, ticket_number, expected_revision))
            self._save(state)
        elif active.get("effective_revision") != expected_revision:
            phase = active.get("phase")
            live_state = self._current_pr_state(active)
            if live_state not in {None, "OPEN", "MERGED"}:
                self._block_closed_current_pr(state, active)
            elif (
                live_state == "MERGED"
                and phase not in {
                    TicketPhase.MERGING.value,
                    TicketPhase.WAITING_MERGE.value,
                    TicketPhase.MERGED.value,
                }
                and active.get("blocked_reason") != "merged_revision_mismatch"
            ):
                self._block_external_merge(state, active)
            else:
                if phase == TicketPhase.MERGED.value:
                    integrated_sha = active.get("integrated_sha")
                    if not isinstance(integrated_sha, str) or not integrated_sha:
                        raise ValueError(
                            "merged Ticket Job is missing integrated SHA"
                        )
                    _record_superseded_integration(
                        active, integrated_sha
                    )
                self._mark_revision_stale(state, active)
                self._save(state)
        elif (
            active.get("phase") == "blocked"
            and active.get("blocked_reason") == "no_code_changes"
        ):
            active["phase"] = TicketPhase.DEVELOPING.value
            active.pop("blocked_reason", None)
            self._save(state)
        elif (
            active.get("phase") == TicketPhase.BLOCKED.value
            and active.get("blocked_reason")
            in {"effective_revision_mismatch", "merged_revision_mismatch"}
        ):
            # The live content may have changed and then returned to the same
            # fingerprint. The persisted mismatch still invalidates every
            # prior artifact, but only an explicit Requeue may create a new
            # generation and budget window.
            self._mark_revision_stale(state, active)
            self._save(state)
        elif active.get("phase") == TicketPhase.BLOCKED.value:
            self._restore_blocked_projection(state, active)
        return active

    def _restore_blocked_projection(
        self, state: dict[str, Any], active: dict[str, Any]
    ) -> None:
        reason = active.get("blocked_reason")
        if not isinstance(reason, str) or reason not in BLOCKED_MESSAGES:
            raise ValueError("blocked Ticket Job has unknown blocked_reason")
        state["status"] = "blocked"
        state["diagnostics"] = [
            {
                "code": reason,
                "message": BLOCKED_MESSAGES[reason],
                "ticket_number": active["ticket_number"],
            }
        ]
        self._save(state)

    @staticmethod
    def _mark_revision_stale(
        state: dict[str, Any], active: dict[str, Any]
    ) -> None:
        generation = active.get("ticket_branch_generation")
        if type(generation) is not int or generation < 1:
            raise ValueError("stale Ticket Job has invalid branch generation")
        state.update(
            {
                "status": "requeue_required",
                "terminal_kind": "requeue_required",
                "diagnostics": [
                    {
                        "code": "ticket_requirements_changed",
                        "message": "Ticket requirements changed; run requeue",
                        "ticket_number": active.get("ticket_number"),
                    }
                ],
                "requeue_required": {
                    "work_subject": f"ticket:{active.get('ticket_number')}",
                    "generation": generation,
                    "reason": "ticket_requirements_changed",
                },
            }
        )

    def _current_pr_state(self, active: dict[str, Any]) -> object:
        pr_number = active.get("pr_number")
        if not isinstance(pr_number, int):
            return None
        return self.github.live_pull_request(pr_number).get("state")

    def _block_closed_current_pr(
        self, state: dict[str, Any], active: dict[str, Any]
    ) -> None:
        active["phase"] = TicketPhase.BLOCKED.value
        active["blocked_reason"] = "ticket_pr_closed_unmerged"
        state["status"] = "blocked"
        state["diagnostics"] = [
            {
                "code": "ticket_pr_closed_unmerged",
                "message": "Current Ticket PR was closed without merging",
                "ticket_number": active["ticket_number"],
            }
        ]
        self._save(state)

    def _block_external_merge(
        self, state: dict[str, Any], active: dict[str, Any]
    ) -> None:
        active["phase"] = TicketPhase.BLOCKED.value
        active["blocked_reason"] = "unexpected_external_merge"
        state["status"] = "blocked"
        state["diagnostics"] = [
            {
                "code": "unexpected_external_merge",
                "message": (
                    "Ticket PR merged without a persisted Publisher "
                    "merge intent"
                ),
                "ticket_number": active["ticket_number"],
            }
        ]
        self._save(state)

    def _new_job_fields(
        self,
        state: dict[str, Any],
        ticket_number: int,
        expected_revision: str,
    ) -> dict[str, Any]:
        retired_generations = state.get("retired_ticket_generations", {})
        retired_generation = (
            retired_generations.get(str(ticket_number), 0)
            if isinstance(retired_generations, dict)
            else 0
        )
        generation = (
            retired_generation + 1
            if isinstance(retired_generation, int)
            else 1
        )
        branch_suffix = (
            f"ticket-{ticket_number}"
            if generation == 1
            else f"ticket-{ticket_number}-generation-{generation}"
        )
        return {
            "ticket_branch": (
                f"agent-run/{state['run_id']}/{branch_suffix}"
            ),
            "ticket_branch_generation": generation,
            "base_sha": self.git.resolve(str(state["run_branch"])),
            "effective_revision": expected_revision,
            "phase": TicketPhase.DEVELOPING.value,
            "modification_attempts": 0,
            "development_thread_id": None,
            "development_thread_history": [],
            "reviewer_thread_ids": [],
            "validation_attempts": 0,
            "acceptance_artifact": None,
            "policy_snapshot": policy_snapshot_for_state(state),
            "review_budget": new_budget(),
            "review_budget_history": [],
        }

    @staticmethod
    def _remove_empty_worktree_directories(checkout: Path) -> None:
        for directory in (checkout.parent, checkout.parent.parent):
            try:
                directory.rmdir()
            except OSError:
                pass

    def _save(self, state: dict[str, Any]) -> None:
        sync_active_ticket_job(state)
        self.states.save_run(str(state["run_id"]), state)


def _effective_revision(
    state: dict[str, Any], ticket: dict[str, Any]
) -> str:
    return effective_revision(
        ticket_revision=str(ticket["content_revision"]),
        parent_revision=str(_mapping(state, "parent")["revision"]),
        graph_revision=str(_mapping(state, "ticket_graph")["revision"]),
    )


def _mapping(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be an object")
    return value
