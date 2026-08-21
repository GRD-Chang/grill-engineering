from __future__ import annotations

"""Currentness, revalidation, and strict promotion for Run Repair Candidates."""

from copy import deepcopy
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agent_run.run_currentness import invalidate_stale_run_repair
from agent_run.run_repair_cycle import escalate_repair, uses_merge_resolution
from agent_run.state_contract import require_candidate_acceptance_history

if TYPE_CHECKING:
    from agent_run.run_acceptance import RunAcceptanceEngine


class RunRepairPromotion:
    """Own default rebind and Candidate promotion authority checks."""

    def __init__(self, owner: RunAcceptanceEngine) -> None:
        self.owner = owner

    def _invalidate_stale_repair(
        self, state: dict[str, Any], job: dict[str, Any], checkout: Path
    ) -> None:
        current_default = (
            self.owner.candidate_acceptance.default_head_sha
            or str(self.owner._mapping(state, "base")["sha"])
        )
        currentness = self.owner.repair_currentness
        if (
            currentness is not None
            and current_default != job.get("default_base_sha")
            and not currentness.non_default_revision_changed(
                state, job, default_head_sha=current_default
            )
        ):
            self._preserve_stale_acceptance_repair(state, job, checkout)
            self._rebind_repair_to_default(state, job, current_default)
            return
        self.owner.git.remove_worktree(checkout)
        self.owner._remove_empty_directories(checkout)
        invalidate_stale_run_repair(state)
        job["phase"] = "stale"

    def _rebind_repair_to_default(
        self, state: dict[str, Any], job: dict[str, Any], default_head: str
    ) -> None:
        """Freeze current work and revalidate it against an advanced default head."""

        prior_phase = str(job["phase"])
        publication_was_integrated = (
            isinstance(job.get("integrated_sha"), str)
            and job.get("integrated_publication_sha") == job.get("publication_sha")
        )
        integrated_merge_resolution = publication_was_integrated and uses_merge_resolution(
            job
        )
        prior_default_head = str(job["default_base_sha"])
        prior_publication_sha = job.get("publication_sha")
        acceptance_artifact = job.get("acceptance_artifact")
        if (
            job.get("repair_source") == "acceptance"
            and isinstance(acceptance_artifact, dict)
        ):
            job["unresolved_acceptance_artifact"] = deepcopy(acceptance_artifact)
        job["default_base_sha"] = default_head
        if integrated_merge_resolution:
            # The controlled two-parent result is now the actual Run head.  A
            # later default-only drift validates that Run head as an ordinary
            # Candidate against the new default.  If the new combination
            # conflicts, the existing squash-to-merge-resolution conversion
            # prepares a fresh three-way scene without ending this Cycle.
            integrated = str(job["integrated_sha"])
            job["integrated_revalidation_merge"] = {
                "base_sha": job.get("base_sha"),
                "default_base_sha": prior_default_head,
                "candidate_sha": job.get("candidate_sha"),
                "publication_sha": job.get("publication_sha"),
            }
            job.update(
                {
                    "repair_mode": "squash",
                    "repair_source": "acceptance",
                    "base_sha": integrated,
                    "repair_base_run_head_sha": integrated,
                    "candidate_sha": integrated,
                }
            )
        self.owner.default_head_sha = default_head
        self.owner.candidate_acceptance.update_default_head(default_head)
        stale_keys = [
            "acceptance_record",
            "acceptance_artifact",
            "merge_intent",
            "ticket_write_intent",
            "review_resume_thread_id",
            "review_human_blocker_resume",
            "review_new_thread",
            "pending_review_result",
        ]
        if not publication_was_integrated:
            stale_keys.extend(("publication", "publication_sha"))
        for key in stale_keys:
            job.pop(key, None)
        candidate_sha = job.get("candidate_sha")
        squash_candidate_sha = job.get("integration_squash_candidate_sha")
        finding_snapshot_sha = job.get("integration_finding_snapshot_sha")
        pending_attempt = job.get("pending_attempt")
        reprepare_merge_resolution = (
            not publication_was_integrated
            and uses_merge_resolution(job)
            and (
                isinstance(candidate_sha, str)
                or isinstance(squash_candidate_sha, str)
                or isinstance(finding_snapshot_sha, str)
                or (
                    isinstance(pending_attempt, int)
                    and pending_attempt > int(job.get("modification_attempts", 0))
                )
            )
        )
        if reprepare_merge_resolution:
            if isinstance(candidate_sha, str):
                superseded = job.setdefault("superseded_candidate_shas", [])
                if not isinstance(superseded, list) or not all(
                    isinstance(sha, str) for sha in superseded
                ):
                    raise ValueError("superseded_candidate_shas must contain strings")
                if candidate_sha not in superseded:
                    superseded.append(candidate_sha)
                job["integration_reprepare_candidate_sha"] = candidate_sha
            job["integration_reprepare_default_sha"] = prior_default_head
            if isinstance(prior_publication_sha, str):
                publications = job.setdefault("superseded_publication_shas", [])
                if not isinstance(publications, list) or not all(
                    isinstance(sha, str) for sha in publications
                ):
                    raise ValueError("superseded_publication_shas must contain strings")
                if prior_publication_sha not in publications:
                    publications.append(prior_publication_sha)
                job["integration_reprepare_publication_sha"] = prior_publication_sha
            job["integration_reprepare_required"] = True
            if (
                prior_phase == "committing_candidate"
                and isinstance(pending_attempt, int)
                and pending_attempt > int(job.get("modification_attempts", 0))
            ):
                job["integration_reprepare_discard_invocation_changes"] = True
            if job.get("integration_reprepare_preserved_changes") is True:
                job["integration_reprepare_discard_invocation_changes"] = True
            job["repair_source"] = "merge_conflict"
            job.pop("candidate_sha", None)
            job["phase"] = "repairing"
        elif prior_phase != "committing_candidate" and isinstance(candidate_sha, str):
            job["phase"] = "candidate"
        run = self.owner._run_state(state)
        run["phase"] = "repairing"
        self.owner._sync_repair_cycle_counters(run, job)
        publication = state.get("run_publication")
        if isinstance(publication, dict) and publication.get("phase") not in {
            "merged",
            "abandoned",
        }:
            publication["phase"] = "stale"
            for key in ("artifact", "write_intent", "approval_grant"):
                publication.pop(key, None)
        state.update(
            {
                "status": "run_acceptance_pending",
                "terminal_kind": "run_repair_pending",
                "diagnostics": [],
            }
        )

    def _preserve_stale_acceptance_repair(
        self, state: dict[str, Any], job: dict[str, Any], checkout: Path
    ) -> None:
        """Save a completed finding-repair invocation before rebasing its merge scene."""

        pending_attempt = job.get("pending_attempt")
        unresolved_artifact = job.get("unresolved_acceptance_artifact")
        candidate_sha = job.get("candidate_sha")
        if not (
            uses_merge_resolution(job)
            and job.get("phase") == "committing_candidate"
            and job.get("repair_source") == "acceptance"
            and isinstance(unresolved_artifact, dict)
            and isinstance(candidate_sha, str)
            and isinstance(pending_attempt, int)
            and pending_attempt > int(job.get("modification_attempts", 0))
        ):
            return
        snapshot = self.owner.git.create_integration_repair_snapshot(
            checkout,
            candidate_sha=candidate_sha,
            attempt=pending_attempt,
        )
        if snapshot is None:
            return
        job["integration_finding_snapshot_sha"] = snapshot
        job["integration_reprepare_preserved_changes"] = True
        job["modification_attempts"] = pending_attempt
        job["code_modification_attempts"] = pending_attempt
        job.pop("pending_attempt", None)
        self.owner._sync_repair_cycle_counters(self.owner._run_state(state), job)

    def _complete_integration_reprepare(
        self, state: dict[str, Any], job: dict[str, Any], checkout: Path
    ) -> None:
        superseded_candidate = job.get("integration_reprepare_candidate_sha")
        superseded_default = job.get("integration_reprepare_default_sha")
        superseded_publication = job.get("integration_reprepare_publication_sha")
        if superseded_candidate is not None and not isinstance(
            superseded_candidate, str
        ):
            raise ValueError("integration reprepare Candidate must be a string")
        if superseded_candidate is None:
            pending_attempt = job.get("pending_attempt")
            if not (
                isinstance(job.get("integration_squash_candidate_sha"), str)
                or isinstance(job.get("integration_finding_snapshot_sha"), str)
                or (
                    isinstance(pending_attempt, int)
                    and pending_attempt > int(job.get("modification_attempts", 0))
                )
            ):
                raise ValueError("integration reprepare requires its pending attempt")
        if not isinstance(superseded_default, str):
            raise ValueError("integration reprepare requires its superseded default head")
        evidence = self.owner.git.reprepare_integration_repair_checkout(
            checkout,
            run_head_sha=str(job["base_sha"]),
            default_head_sha=str(job["default_base_sha"]),
            superseded_candidate_sha=superseded_candidate,
            superseded_default_head_sha=superseded_default,
            superseded_publication_sha=(
                superseded_publication
                if isinstance(superseded_publication, str)
                else None
            ),
            squash_candidate_sha=(
                str(job["integration_squash_candidate_sha"])
                if isinstance(job.get("integration_squash_candidate_sha"), str)
                else None
            ),
            finding_snapshot_sha=(
                str(job["integration_finding_snapshot_sha"])
                if isinstance(job.get("integration_finding_snapshot_sha"), str)
                else None
            ),
            discard_invocation_changes=(
                job.get("integration_reprepare_discard_invocation_changes") is True
            ),
        )
        if evidence is None:
            pending_attempt = job.get("pending_attempt")
            if not (
                isinstance(pending_attempt, int)
                and pending_attempt > int(job["modification_attempts"])
            ):
                job["pending_attempt"] = int(job["modification_attempts"])
            job["phase"] = "committing_candidate"
            job.pop("merge_conflict_evidence", None)
            job.pop("integration_conflict_paths", None)
        else:
            job.update(
                {
                    "repair_source": "merge_conflict",
                    "merge_conflict_evidence": evidence,
                    "integration_conflict_paths": list(
                        self.owner.git.integration_conflict_paths(checkout)
                    ),
                    "phase": "repairing",
                }
            )
        job.pop("integration_reprepare_candidate_sha", None)
        job.pop("integration_reprepare_default_sha", None)
        job.pop("integration_reprepare_publication_sha", None)
        job.pop("integration_reprepare_discard_invocation_changes", None)
        job.pop("integration_reprepare_preserved_changes", None)
        job.pop("integration_reprepare_required", None)
        self.owner._save(state)

    def _repair_trigger_is_current(
        self, state: dict[str, Any], job: dict[str, Any]
    ) -> bool:
        currentness = self.owner.repair_currentness
        if currentness is None:
            raise ValueError("Run Repair requires the Publisher")
        return currentness.trigger_is_current(
            state, job, default_head_sha=self.owner._default_head(state)
        )

    def _after_repair_merge(
        self,
        state: dict[str, Any],
        job: dict[str, Any],
        _live_before_merge: dict[str, Any],
    ) -> bool:
        if self.owner.github is None:
            raise ValueError("Run Repair requires the Publisher")
        candidate_history = require_candidate_acceptance_history(
            job.get("candidate_acceptance_history", []),
            "run_acceptance.repair_job.candidate_acceptance",
        )
        raw_run = state.get("run_acceptance")
        if not isinstance(raw_run, dict):
            raise ValueError("run_acceptance must be an object")
        run_history = require_candidate_acceptance_history(
            raw_run.get("candidate_acceptance_history", []),
            "run_acceptance.candidate_acceptance",
        )
        currentness = self.owner.repair_currentness
        if currentness is None:
            raise ValueError("Run Repair requires the Publisher")
        live = currentness.live_pull_request(
            state,
            int(job["pr_number"]),
            waiting_for=f"merged Run Repair PR #{int(job['pr_number'])} promotion",
        )
        integrated = job.get("integrated_sha")
        publication = self.owner._mapping(job, "publication")
        merge_resolution = uses_merge_resolution(job)
        revalidation_merge = job.get("integrated_revalidation_merge")
        published_as_merge_resolution = merge_resolution or isinstance(
            revalidation_merge, dict
        )
        published_base = (
            revalidation_merge.get("base_sha")
            if isinstance(revalidation_merge, dict)
            else job.get("base_sha")
        )
        published_default = (
            revalidation_merge.get("default_base_sha")
            if isinstance(revalidation_merge, dict)
            else job.get("default_base_sha")
        )
        published_candidate = (
            revalidation_merge.get("candidate_sha")
            if isinstance(revalidation_merge, dict)
            else job.get("candidate_sha")
        )
        published_publication = (
            revalidation_merge.get("publication_sha")
            if isinstance(revalidation_merge, dict)
            else job.get("publication_sha")
        )
        expected_parents = (
            [published_base, published_publication]
            if published_as_merge_resolution
            else [published_base]
        )
        if (
            live.get("state") != "MERGED"
            or not isinstance(integrated, str)
            or live.get("integrated_sha") != integrated
            or live.get("head_sha") != job.get("publication_sha")
            or live.get("base_branch") != state.get("run_branch")
            or live.get("head_tree") != live.get("integrated_tree")
            or (
                not published_as_merge_resolution
                and live.get("integrated_message") != publication.get("commit_message")
            )
            or live.get("integrated_parents") != expected_parents
            or (
                published_as_merge_resolution
                and (
                    self.owner.git.commit_parents(str(published_candidate))
                    != [published_base, published_default]
                    or self.owner.git.commit_parents(str(published_publication))
                    != [published_base, published_default]
                    or not self.owner.git.is_ancestor(str(published_default), integrated)
                )
            )
        ):
            self._invalidate_stale_repair(state, job, self.owner._repair_checkout(state))
            return False
        if not self.owner._refresh_run_currentness(state):
            return False
        if not self._repair_trigger_is_current(state, job):
            self._invalidate_stale_repair(state, job, self.owner._repair_checkout(state))
            return False
        promoted = self.owner._candidate_promotion_record(state, job, integrated)
        if promoted is None:
            # A merged Candidate is not automatically an accepted Run.  Any
            # mismatch at this seam discards the Candidate and starts a fresh
            # Run Acceptance generation against the live authorities.
            self._invalidate_stale_repair(state, job, self.owner._repair_checkout(state))
            return False
        run = self.owner._run_state(state)
        prior = set(self.owner._string_list(job, "prior_reviewer_thread_ids"))
        reviewers = self.owner._string_list(run, "reviewer_thread_ids")
        reviewers.extend(
            thread_id
            for thread_id in self.owner._string_list(job, "reviewer_thread_ids")
            if thread_id not in prior and thread_id not in reviewers
        )
        run["reviewer_thread_ids"] = reviewers
        history = self.owner._string_list(run, "development_thread_history")
        development_ids = [
            *self.owner._string_list(job, "development_thread_history"),
            str(job.get("development_thread_id", "")),
        ]
        for thread_id in development_ids:
            if thread_id and thread_id not in history:
                history.append(thread_id)
        run["development_thread_history"] = history
        run.update(
            {
                "modification_attempts": int(job["modification_attempts"]),
                "code_modification_attempts": int(
                    job.get("code_modification_attempts", job["modification_attempts"])
                ),
                "candidate_sha": job["candidate_sha"],
                "publication_sha": job["publication_sha"],
                "repair_pr_number": job["pr_number"],
                "integrated_sha": integrated,
                "reviewed_head_sha": integrated,
                "acceptance_record": promoted,
                "acceptance_artifact": promoted["artifact"],
                "phase": "accepted",
            }
        )
        cycle = run.get("repair_cycle")
        if isinstance(cycle, dict):
            cycle.update(
                {
                    "status": "promoted",
                    "promoted_candidate_sha": str(job["candidate_sha"]),
                    "integrated_sha": integrated,
                }
            )
        completed_repairs = run.setdefault("completed_repair_jobs", [])
        if not isinstance(completed_repairs, list):
            raise ValueError("completed_repair_jobs must be a list")
        completed = {
            "phase": "completed",
            "repair_branch": job["repair_branch"],
            "integrated_sha": integrated,
            "candidate_sha": job["candidate_sha"],
            "acceptance_state": "promoted",
        }
        run["candidate_acceptance_history"] = [
            *run_history,
            *deepcopy(candidate_history),
        ]
        display = job.get("linked_branch_display")
        if isinstance(display, dict):
            completed["linked_branch_display"] = dict(display)
        completed_repairs.append(completed)
        del completed_repairs[:-32]
        run.pop("repair_job", None)
        publication_state = state.get("run_publication")
        if isinstance(publication_state, dict) and publication_state.get("phase") not in {
            "merged",
            "abandoned",
        }:
            # The existing Final Run PR, if any, must receive a refreshed
            # narrative after the promoted boundary is durable.
            publication_state["phase"] = "stale"
            for key in ("artifact", "write_intent", "approval_grant"):
                publication_state.pop(key, None)
        state["status"] = "run_publication_pending"
        state["terminal_kind"] = "run_acceptance_passed"
        state["diagnostics"] = []
        return True

    def _escalate_repair(
        self, state: dict[str, Any], job: dict[str, Any], code: str
    ) -> None:
        escalate_repair(state, self.owner._run_state(state), job, code)
