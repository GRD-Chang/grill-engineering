from __future__ import annotations

from collections.abc import Callable
from typing import Any

from agent_run.approval_grant import (
    acceptance_fingerprint,
    create_grant,
    grant_authority,
    grant_matches,
)
from agent_run.delivery_cleanup import (
    DeliveryCleanupEngine,
    remove_run_worktrees,
    require_clean_run_worktrees,
)
from agent_run.external_supervision import (
    ensure_supervision_window,
    is_github_convergence_error,
    wait_for_github_convergence,
)
from agent_run.github import GitHubReadError, MergeOutcomeUnknownError
from agent_run.run_currentness import ticket_completion_records
from agent_run.run_publication_shared import RunPublicationShared
from agent_run.run_publication_update import reconcile_publication_update, wait_for_merge_readback


def prepare_run_approval(state: dict[str, Any]) -> None:
    """Bind human intent to the existing accepted object before fact refresh."""
    publication = state["run_publication"]
    if publication.get("phase") != "ready_for_approval" or publication.get("approval_grant") is not None:
        return
    publication.pop("approval_update", None)
    publication["approval_grant"] = create_grant(run_approval_grant_authority(
        state, str(publication["record"]["run_head_sha"]),
    ))


def run_approval_grant_authority(state: dict[str, Any], run_head: str) -> dict[str, object]:
    run = state["run_acceptance"]
    return grant_authority(
        repository=str(state["repository"]),
        pr_number=int(state["run_publication"]["pr_number"]),
        head_branch=str(state["run_branch"]), head_sha=run_head,
        base_branch=str(state["base"]["branch"]),
        base_sha=str(run["acceptance_record"]["reviewed_default_base_sha"]),
        acceptance_fingerprint=acceptance_fingerprint(
            run["acceptance_record"], run["acceptance_artifact"],
        ),
    )


class RunPublicationApproval(RunPublicationShared):
    """Handle approval, revision, abandonment, and closeout of a Final Run PR."""

    def approve(
        self,
        run_id: str,
        *,
        prepare_state: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        state = self._load(run_id)
        publication = self._publication_state(state)
        recovering_merge = (
            isinstance(publication.get("merge_intent"), dict)
        )
        if publication["phase"] == "merged":
            return self._complete_parent_closeout(state)
        if publication["phase"] not in {
            "ready_for_approval",
            "waiting_checks",
            "waiting_external",
        }:
            raise ValueError("Run Publication is not ready for explicit approval")
        run = self._mapping(state, "run_acceptance")
        record = self._mapping(publication, "record")
        pr_number = self._integer(publication, "pr_number")
        run_head = self.git.resolve(str(state["run_branch"]))
        prepare_run_approval(state)
        # A later read may observe the default branch after our merge. Keep
        # the original human grant while reconciling that exact merge intent;
        # an OPEN PR still has to pass the existing grant/currentness checks.
        if prepare_state is not None:
            prepare_state(state)
        self._save(state)
        if not self._refresh_currentness(state):
            return self._save(state)
        try:
            live = self.github.live_pull_request(pr_number)
            if live.get("state") != "MERGED" and not reconcile_publication_update(
                self, state, live,
            ):
                return state
        except (GitHubReadError, OSError, TimeoutError) as error:
            if recovering_merge:
                return wait_for_merge_readback(self, state, str(error))
            if isinstance(error, GitHubReadError) and not is_github_convergence_error(
                error.code
            ):
                raise
            publication["required_checks_observation_status"] = "unavailable"
            publication["phase"] = "waiting_external"
            if isinstance(error, GitHubReadError):
                code = error.code
                message = error.message
            else:
                code = "github_final_run_pr_observation_failed"
                message = str(error)
            wait_for_github_convergence(
                state,
                code=code,
                message=message,
                waiting_for=f"Run PR #{pr_number} Required Checks observation",
            )
            ensure_supervision_window(state)
            return self._save(state)
        if live.get("state") == "MERGED":
            self._require_persisted_merge_intent(
                publication, state, record, pr_number, run_head
            )
            # A merged PR's live base SHA is now the advanced default ref;
            # the original base SHA is verified below as the first parent.
            self._require_final_pr_identity(
                live=live,
                run_head=run_head,
                branch=str(state["run_branch"]),
                repository=str(state["repository"]),
                expected_base_sha=None,
            )
            integrated = live.get("integrated_sha")
            if (
                isinstance(integrated, str)
                and record.get("pr_head_sha") == live.get("head_sha")
                and live.get("integrated_parents")
                == [record.get("default_head_sha"), record.get("run_head_sha")]
                and live.get("integrated_tree") == record.get("expected_merge_tree")
            ):
                return self._mark_merged_and_close_parent(state, integrated)
            return self._block_merged_boundary(
                state,
                "merged final Run PR does not match its reviewed publication boundary",
            )
        if not self._acceptance_is_current(state, run):
            return self._invalidate_for_fresh_acceptance(state)
        authority = run_approval_grant_authority(state, run_head)
        if (
            record.get("pr_head_sha") != live.get("head_sha")
            or record.get("run_head_sha") != run_head
            or record.get("default_head_sha") != self.default_head_sha
            or record.get("ticket_completion_records")
            != ticket_completion_records(state)
            or live.get("base_sha") != self.default_head_sha
            or live.get("base_branch") != self.default_branch
        ):
            return self._invalidate_for_fresh_acceptance(state)
        if recovering_merge and not grant_matches(
            publication.get("approval_grant"), authority
        ):
            return self._invalidate_for_fresh_acceptance(state)
        if live.get("state") != "OPEN":
            raise GitHubReadError(
                "final_run_pr_not_open",
                "Final Run PR is not open at the approved publication boundary",
            )
        if live.get("mergeable") is not True:
            return self._invalidate_for_fresh_acceptance(state)
        observation = self._observe_required_checks(
            state, run, publication, pr_number, run_head
        )
        if observation is None:
            return state
        checks = str(observation["result"])
        try:
            self._record_agent_run_status(pr_number, run, run_head, checks)
        except Exception as error:
            # The grant and Action receipt are already durable here. Retain
            # the failing external step in the Action failure history so a
            # later status query does not reduce it to a Host exit message.
            raise RuntimeError(
                f"Final Run PR status comment write failed: {error}"
            ) from error
        routed = self._route_required_checks(
            state, publication, pr_number, run_head, observation
        )
        if routed is not None:
            return routed
        if not grant_matches(publication.get("approval_grant"), authority):
            return self._invalidate_for_fresh_acceptance(state)
        if not self._revalidate_final_run_pr_before_merge(
            state, publication, pr_number, run_head
        ):
            return state
        merge_intent = self._prepare_merge_intent(
            publication, state, record, pr_number, run_head
        )
        attempts = merge_intent["attempts"]
        if attempts >= 3:
            return wait_for_merge_readback(
                self, state,
                "Final Run merge intent exhausted exact reconciliation attempts",
            )
        merge_intent["attempts"] = attempts + 1
        self._save(state)
        try:
            integrated = self.github.normal_merge(
                pr_number=pr_number,
                expected_head_sha=run_head,
                expected_head_branch=str(state["run_branch"]),
                expected_head_repository=str(state["repository"]),
                expected_base_branch=self.default_branch,
                expected_base_sha=self.default_head_sha,
                expected_base_repository=str(state["repository"]),
            )
            merged = self.github.live_pull_request(pr_number)
        except (MergeOutcomeUnknownError, OSError, TimeoutError) as error:
            return wait_for_merge_readback(self, state, str(error))
        except GitHubReadError as error:
            if not is_github_convergence_error(error.code):
                raise
            return wait_for_merge_readback(self, state, str(error))
        # The merge has advanced the live base ref; its original SHA is
        # still verified as the first parent below.
        self._require_final_pr_identity(
            live=merged,
            run_head=run_head,
            branch=str(state["run_branch"]),
            repository=str(state["repository"]),
            expected_base_sha=None,
        )
        if (
            merged.get("state") != "MERGED"
            or merged.get("integrated_sha") != integrated
            or merged.get("integrated_parents")
            != [self.default_head_sha, live.get("head_sha")]
            or merged.get("integrated_tree") != record.get("expected_merge_tree")
        ):
            return self._block_merged_boundary(
                state,
                "final Run merge result does not preserve the reviewed boundary",
            )
        return self._mark_merged_and_close_parent(state, integrated)

    def has_current_approval_grant(self, run_id: str) -> bool:
        """Check whether an earlier human approval still binds this final PR."""
        state = self._load(run_id)
        publication = self._publication_state(state)
        run = state.get("run_acceptance")
        pr_number = publication.get("pr_number")
        if not isinstance(run, dict) or not isinstance(pr_number, int):
            return False
        if publication.get("phase") == "waiting_external" or isinstance(publication.get("merge_intent"), dict):
            return isinstance(publication.get("approval_grant"), dict)
        try:
            authority = run_approval_grant_authority(
                state, self.git.resolve(str(state["run_branch"])),
            )
        except (KeyError, TypeError, ValueError):
            return False
        return grant_matches(publication.get("approval_grant"), authority)

    def recover_closeout(
        self,
        run_id: str,
        *,
        prepare_state: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        state = self._load(run_id)
        publication = self._publication_state(state)
        if publication.get("phase") != "merged":
            raise ValueError("Run Publication is not awaiting Parent closeout")
        if prepare_state is not None:
            prepare_state(state)
            self._save(state)
        return self._complete_parent_closeout(state)

    def revise(
        self,
        run_id: str,
        feedback: str,
        *,
        prepare_state: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        if not feedback.strip():
            raise ValueError("revision feedback must be non-empty")
        state = self._load(run_id)
        publication = self._publication_state(state)
        if publication["phase"] in {"merged", "abandoned"}:
            raise ValueError("a completed or abandoned Run cannot be revised")
        if state.get("status") not in {"ready_for_human", "run_approval_pending"}:
            raise ValueError("revise is available only at an explicit human gate")
        self._queue_repair(
            state,
            repair_source="human_revision",
            human_feedback=feedback.strip(),
        )
        publication["phase"] = "stale"
        state["terminal_kind"] = "final_revision_requested"
        if prepare_state is not None:
            prepare_state(state)
        return self._save(state)

    def abandon(self, run_id: str, *, discard_worktree: bool = False) -> dict[str, Any]:
        state = self._load(run_id)
        if state.get("status") in {"completed", "abandoned"}:
            return state
        publication = self._publication_state(state)
        if publication["phase"] == "merged":
            raise ValueError("a merged Run cannot be abandoned")
        if publication["phase"] == "abandoned":
            return state
        if not discard_worktree:
            require_clean_run_worktrees(self.git, self.states, run_id)
        abandonment = state.get("run_abandonment")
        if not isinstance(abandonment, dict):
            final_pr_number = publication.get("pr_number")
            abandonment = {
                "phase": "pending",
                "kind": "ticket_run",
                "change_prs": [
                    {"pr_number": number, "status": "pending"}
                    for number in _change_pr_numbers(state)
                ],
                "tickets": _ticket_recovery_obligations(state),
                "final_pr": (
                    {"pr_number": final_pr_number, "status": "pending"}
                    if isinstance(final_pr_number, int)
                    else None
                ),
            }
            state.update(
                {
                    "run_abandonment": abandonment,
                    "status": "abandonment_pending",
                    "terminal_kind": "abandonment_pending",
                    "diagnostics": [],
                }
            )
            # Delivery waiting has ended. Abandonment owns subsequent remote
            # reconciliation, including close responses that may be lost.
            for job in [*state.get("ticket_jobs", {}).values(), state.get("active_ticket_job")]:
                if isinstance(job, dict):
                    job.pop("external_wait", None)
                    job.pop("supervision_wait", None)
            for key in ("supervision_window", "supervision_wait", "credential_availability"):
                state.pop(key, None)
            self._save(state)
        for ticket in _obligation_list(abandonment, "tickets"):
            if ticket.get("eligible") is None:
                recorded_ownership = (
                    ticket.get("recorded_ownership")
                    if isinstance(ticket.get("recorded_ownership"), dict)
                    else None
                )
                ownership = self.github.ticket_close_ownership(
                    ticket_number=int(ticket["ticket_number"]),
                    run_id=run_id,
                    recorded_ownership=recorded_ownership,
                )
                ticket["expected_ownership"] = ownership
                ticket["eligible"] = ownership is not None
                self._save(state)
        for change_pr in _obligation_list(abandonment, "change_prs"):
            if change_pr.get("status") != "completed":
                self.github.abandon_change_pr(int(change_pr["pr_number"]))
                change_pr["status"] = "completed"
                self._save(state)
        for ticket in _obligation_list(abandonment, "tickets"):
            if ticket.get("eligible") is True and ticket.get("status") != "completed":
                expected_ownership = ticket.get("expected_ownership")
                if not isinstance(expected_ownership, dict):
                    raise ValueError("eligible Ticket recovery requires ownership")
                recovered = self.github.recover_abandoned_ticket(
                    ticket_number=int(ticket["ticket_number"]),
                    run_id=run_id,
                    pr_number=int(ticket["pr_number"]),
                    integrated_sha=str(ticket["integrated_sha"]),
                    expected_ownership=expected_ownership,
                )
                ticket["status"] = "completed" if recovered else "not_owned"
                self._save(state)
            elif ticket.get("eligible") is False:
                ticket["status"] = "not_owned"
        final_pr = abandonment.get("final_pr")
        if isinstance(final_pr, dict) and final_pr.get("status") != "completed":
            self.github.abandon_run_pr(int(final_pr["pr_number"]))
            final_pr["status"] = "completed"
            self._save(state)
        remove_run_worktrees(
            self.git,
            self.states,
            run_id,
            discard_worktree=discard_worktree,
        )
        abandonment["phase"] = "completed"
        publication["phase"] = "abandoned"
        state.update(
            {"status": "abandoned", "terminal_kind": "abandoned", "diagnostics": []}
        )
        return self._save(state)

    def _mark_merged_and_close_parent(
        self, state: dict[str, Any], integrated_sha: str
    ) -> dict[str, Any]:
        publication = self._publication_state(state)
        publication.update({"phase": "merged", "integrated_sha": integrated_sha})
        state.update(
            {
                "status": "parent_closeout_pending",
                "terminal_kind": "parent_closeout_pending",
                "diagnostics": [],
            }
        )
        self._save(state)
        return self._complete_parent_closeout(state)

    def _prepare_merge_intent(
        self,
        publication: dict[str, Any],
        state: dict[str, Any],
        record: dict[str, Any],
        pr_number: int,
        run_head: str,
    ) -> dict[str, Any]:
        expected = self._merge_intent_boundary(
            state, record, pr_number, run_head, self.default_head_sha
        )
        existing = publication.get("merge_intent")
        if existing is None:
            intent = {**expected, "attempts": 0}
            publication["merge_intent"] = intent
            self._save(state)
            return intent
        if not isinstance(existing, dict) or any(
            existing.get(key) != value for key, value in expected.items()
        ) or type(existing.get("attempts")) is not int or existing["attempts"] < 0:
            raise GitHubReadError(
                "merge_intent_mismatch",
                "Persisted Final Run merge intent does not match the approved boundary",
            )
        return existing

    def _require_persisted_merge_intent(
        self,
        publication: dict[str, Any],
        state: dict[str, Any],
        record: dict[str, Any],
        pr_number: int,
        run_head: str,
    ) -> None:
        intent = publication.get("merge_intent")
        base_sha = record.get("default_head_sha")
        if not isinstance(base_sha, str):
            raise ValueError("Final Run publication record is missing default head")
        expected = self._merge_intent_boundary(
            state, record, pr_number, run_head, base_sha
        )
        if not isinstance(intent, dict) or type(intent.get("attempts")) is not int or intent["attempts"] < 1 or any(
            intent.get(key) != value for key, value in expected.items()
        ):
            raise GitHubReadError(
                "merge_intent_mismatch",
                "Merged Final Run PR has no matching persisted merge intent",
            )

    def _merge_intent_boundary(
        self,
        state: dict[str, Any],
        record: dict[str, Any],
        pr_number: int,
        run_head: str,
        base_sha: str,
    ) -> dict[str, Any]:
        return {
            "pr_number": pr_number,
            "repository": str(state["repository"]),
            "head_branch": str(state["run_branch"]),
            "head_sha": run_head,
            "base_branch": self.default_branch,
            "base_sha": base_sha,
            "acceptance_boundary": dict(record),
        }

    def _complete_parent_closeout(self, state: dict[str, Any]) -> dict[str, Any]:
        publication = self._publication_state(state)
        integrated_sha = publication.get("integrated_sha")
        if not isinstance(integrated_sha, str) or not integrated_sha:
            raise ValueError("merged final Run is missing its integrated SHA")

        def record_close_intent(intent: dict[str, Any]) -> None:
            publication["parent_close_intent"] = intent
            self._save(state)

        self.github.close_parent_issue(
            parent_number=int(self._mapping(state, "parent")["number"]),
            run_id=str(state["run_id"]),
            pr_number=self._integer(publication, "pr_number"),
            integrated_sha=integrated_sha,
            delivery_type="Final Run",
            close_intent=publication.get("parent_close_intent"),
            record_intent=record_close_intent,
        )
        publication["parent_closed"] = True
        state.update({"status": "completed", "terminal_kind": "merged", "diagnostics": []})
        self._save(state)
        return DeliveryCleanupEngine(
            git=self.git, states=self.states, github=self.github
        ).complete_final_run(state)

    def _block_merged_boundary(
        self, state: dict[str, Any], message: str
    ) -> dict[str, Any]:
        publication = self._publication_state(state)
        publication["phase"] = "merged_boundary_mismatch"
        state.update(
            {
                "status": "blocked",
                "terminal_kind": "merged_boundary_mismatch",
                "diagnostics": [
                    {"code": "merged_boundary_mismatch", "message": message}
                ],
            }
        )
        return self._save(state)


def _change_pr_numbers(state: dict[str, Any]) -> list[int]:
    numbers: set[int] = set()
    jobs = state.get("ticket_jobs")
    if isinstance(jobs, dict):
        for job in jobs.values():
            if isinstance(job, dict) and isinstance(job.get("pr_number"), int):
                numbers.add(int(job["pr_number"]))
    return sorted(numbers)


def _ticket_recovery_obligations(
    state: dict[str, Any],
) -> list[dict[str, Any]]:
    obligations: list[dict[str, Any]] = []
    jobs = state.get("ticket_jobs")
    if not isinstance(jobs, dict):
        return obligations
    for job in jobs.values():
        if not isinstance(job, dict) or job.get("phase") not in {"merged", "completed"}:
            continue
        ticket_number = job.get("ticket_number")
        pr_number = job.get("pr_number")
        integrated_sha = job.get("integrated_sha")
        if (
            isinstance(ticket_number, int)
            and isinstance(pr_number, int)
            and isinstance(integrated_sha, str)
        ):
            obligations.append(
                {
                    "ticket_number": ticket_number,
                    "pr_number": pr_number,
                    "integrated_sha": integrated_sha,
                    "eligible": None,
                    "recorded_ownership": (
                        job.get("ticket_close_ownership")
                        if isinstance(job.get("ticket_close_ownership"), dict)
                        else (
                            job.get("ticket_close_intent")
                            if isinstance(job.get("ticket_close_intent"), dict)
                            else None
                        )
                    ),
                    "status": "pending",
                }
            )
    return sorted(obligations, key=lambda item: int(item["ticket_number"]))


def _obligation_list(
    abandonment: dict[str, Any], key: str
) -> list[dict[str, Any]]:
    value = abandonment.get(key)
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError(f"run_abandonment.{key} must contain objects")
    return value
