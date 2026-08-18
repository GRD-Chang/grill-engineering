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
            ticket_number = int(job["ticket_number"])
            checkout = (
                self.states.root
                / "worktrees"
                / run_id
                / f"ticket-{ticket_number}"
            )
            preserve_checkout = False
            checkout_prepared = False
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
                } or (
                    job.get("blocked_reason") == "agent_requires_human"
                    and job.get("human_blocker_phase")
                    in {"developing", "repairing"}
                )
                return result
            except KeyboardInterrupt:
                # An explicit operator cancellation is a terminal cleanup
                # request, unlike a recoverable Worker/process failure.
                raise
            except BaseException:
                # Once the stable checkout is ready, any interrupted Worker or
                # Publisher phase may have recoverable uncommitted state.
                # Abrupt process exits leave it behind too, so surfaced errors
                # must preserve the same resume semantics.
                preserve_checkout = (
                    checkout_recoverable or checkout_prepared
                )
                raise
            finally:
                if not preserve_checkout:
                    self.git.remove_worktree(checkout)
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
            elif live_state == "MERGED" and phase not in {
                TicketPhase.MERGING.value,
                TicketPhase.MERGED.value,
            }:
                if self._can_reset_superseded_integration(active):
                    self._reset_for_revision(
                        state, active, expected_revision
                    )
                    self._save(state)
                else:
                    self._block_external_merge(state, active)
            elif phase == TicketPhase.MERGING.value:
                active["pending_effective_revision"] = expected_revision
                self._save(state)
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
                self._reset_for_revision(state, active, expected_revision)
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
            == "effective_revision_mismatch"
        ):
            # The live content may have changed and then returned to the same
            # fingerprint. The persisted mismatch still invalidates every
            # prior artifact, so rebuild instead of treating the Job as idle.
            self._reset_for_revision(state, active, expected_revision)
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
    def _can_reset_superseded_integration(
        active: dict[str, Any]
    ) -> bool:
        merge_intent = active.get("merge_intent")
        records = active.get("superseded_integrations")
        if (
            active.get("phase") != TicketPhase.BLOCKED.value
            or active.get("blocked_reason") != "merged_revision_mismatch"
            or not isinstance(merge_intent, dict)
            or not isinstance(records, list)
        ):
            return False
        expected_record = {
            "pr_number": active.get("pr_number"),
            "integrated_sha": active.get("integrated_sha"),
            "effective_revision": active.get("effective_revision"),
        }
        return any(
            isinstance(record, dict) and record == expected_record
            for record in records
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
        }

    def _reset_for_revision(
        self,
        state: dict[str, Any],
        active: dict[str, Any],
        expected_revision: str,
    ) -> None:
        for key in (
            "candidate_sha",
            "publication",
            "publication_sha",
            "acceptance_artifact",
            "acceptance_record",
            "repair_source",
            "ci_evidence",
            "integrated_sha",
            "merge_intent",
            "ticket_close_intent",
            "ticket_close_ownership",
            "ticket_closed_by_run",
            "pr_number",
            "pending_effective_revision",
            "blocked_reason",
        ):
            active.pop(key, None)
        active.update(
            {
                "base_sha": self.git.resolve(str(state["run_branch"])),
                "effective_revision": expected_revision,
                "phase": TicketPhase.DEVELOPING.value,
                "modification_attempts": 0,
                "validation_attempts": 0,
                "source_revision_changed": False,
            }
        )

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
