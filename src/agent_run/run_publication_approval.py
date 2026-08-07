from __future__ import annotations

import shutil
from typing import Any

from agent_run.delivery_cleanup import DeliveryCleanupEngine
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
            publication = self._publication_state(state)
            if publication["phase"] == "merged":
                raise ValueError("a merged Run cannot be abandoned")
            if publication["phase"] == "abandoned":
                return state
            pr_number = publication.get("pr_number")
            if isinstance(pr_number, int):
                self.github.abandon_run_pr(pr_number)
            shutil.rmtree(
                self.states.root / "worktrees" / str(state["run_id"]), ignore_errors=True
            )
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
