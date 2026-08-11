from __future__ import annotations

from typing import Any

from agent_run.delivery_cleanup import DeliveryCleanupEngine, remove_run_worktrees
from agent_run.git import GitError
from agent_run.run_publication_shared import RunPublicationShared


class RunPublicationApproval(RunPublicationShared):
    """Handle approval, revision, abandonment, and closeout of a Final Run PR."""

    def approve(self, run_id: str) -> dict[str, Any]:
        with self.states.locked():
            state = self._load(run_id)
            publication = self._publication_state(state)
            if publication["phase"] == "merged":
                return self._complete_parent_closeout(state)
            if publication["phase"] != "ready_for_approval":
                raise ValueError("Run Publication is not ready for explicit approval")
            run = self._mapping(state, "run_acceptance")
            if not self._acceptance_is_current(state, run):
                return self._invalidate_for_fresh_acceptance(state)
            record = self._mapping(publication, "record")
            pr_number = self._integer(publication, "pr_number")
            live = self.github.live_pull_request(pr_number)
            if live.get("state") == "MERGED":
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
            if (
                record.get("pr_head_sha") != live.get("head_sha")
                or record.get("run_head_sha") != self.git.resolve(str(state["run_branch"]))
                or record.get("default_head_sha") != self.default_head_sha
                or live.get("base_sha") != self.default_head_sha
                or live.get("base_branch") != self.default_branch
            ):
                if record.get("default_head_sha") != self.default_head_sha:
                    return self._handle_default_drift(state)
                return self._invalidate_for_fresh_acceptance(state)
            if live.get("state") != "OPEN" or not live.get("mergeable"):
                return self._save(
                    self._queue_repair(
                        state,
                        repair_source="merge_conflict",
                        merge_conflict_evidence=(
                            "Final Run PR is no longer mergeable against the current "
                            "default branch."
                        ),
                    )
                )
            checks = self.github.required_checks(pr_number)
            if checks == "fail":
                return self._save(
                    self._queue_repair(
                        state,
                        repair_source="required_checks",
                        ci_evidence=self.github.required_check_evidence(pr_number),
                    )
                )
            if checks == "pending":
                publication["phase"] = "waiting_checks"
                state["status"] = "waiting_checks"
                return self._save(state)
            integrated = self.github.normal_merge(
                pr_number=pr_number, expected_head_sha=str(live["head_sha"])
            )
            merged = self.github.live_pull_request(pr_number)
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

    def recover_closeout(self, run_id: str) -> dict[str, Any]:
        with self.states.locked():
            state = self._load(run_id)
            publication = self._publication_state(state)
            if publication.get("phase") != "merged":
                raise ValueError("Run Publication is not awaiting Parent closeout")
            return self._complete_parent_closeout(state)

    def revise(self, run_id: str, feedback: str) -> dict[str, Any]:
        if not feedback.strip():
            raise ValueError("revision feedback must be non-empty")
        with self.states.locked():
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
            return self._save(state)

    def abandon(self, run_id: str) -> dict[str, Any]:
        with self.states.locked():
            state = self._load(run_id)
            if state.get("status") in {"completed", "abandoned"}:
                return state
            publication = self._publication_state(state)
            if publication["phase"] == "merged":
                raise ValueError("a merged Run cannot be abandoned")
            if publication["phase"] == "abandoned":
                return state
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
            remove_run_worktrees(self.git, self.states, run_id)
            abandonment["phase"] = "completed"
            publication["phase"] = "abandoned"
            state.update(
                {"status": "abandoned", "terminal_kind": "abandoned", "diagnostics": []}
            )
            return self._save(state)

    def _handle_default_drift(self, state: dict[str, Any]) -> dict[str, Any]:
        checkout = self._publication_checkout(state)
        try:
            self.git.prepare_expected_merge_checkout(
                default_head_sha=self.default_head_sha,
                run_head_sha=self.git.resolve(str(state["run_branch"])),
                checkout=checkout,
            )
        except GitError as error:
            return self._save(
                self._queue_repair(
                    state,
                    repair_source="merge_conflict",
                    merge_conflict_evidence=str(error),
                )
            )
        finally:
            self.git.remove_worktree(checkout)
            self._remove_empty_directories(checkout)
        return self._invalidate_for_fresh_acceptance(state)

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

    def _complete_parent_closeout(self, state: dict[str, Any]) -> dict[str, Any]:
        publication = self._publication_state(state)
        integrated_sha = publication.get("integrated_sha")
        if not isinstance(integrated_sha, str) or not integrated_sha:
            raise ValueError("merged final Run is missing its integrated SHA")
        self.github.close_parent_issue(
            parent_number=int(self._mapping(state, "parent")["number"]),
            run_id=str(state["run_id"]),
            pr_number=self._integer(publication, "pr_number"),
            integrated_sha=integrated_sha,
            delivery_type="Final Run",
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
    acceptance = state.get("run_acceptance")
    if isinstance(acceptance, dict):
        repair = acceptance.get("repair_job")
        if isinstance(repair, dict) and isinstance(repair.get("pr_number"), int):
            numbers.add(int(repair["pr_number"]))
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
