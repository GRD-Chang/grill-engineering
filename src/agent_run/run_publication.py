from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from agent_run.agents import AgentBackend
from agent_run.artifacts import PublicationArtifact
from agent_run.delivery_protocol import GitHubPublisher
from agent_run.git import GitError, GitRepository
from agent_run.state import StateStore


class RunPublicationEngine:
    """Publish and explicitly merge one already accepted Delivery Run."""

    def __init__(
        self,
        *,
        git: GitRepository,
        states: StateStore,
        agents: AgentBackend,
        github: GitHubPublisher,
        default_branch: str,
        default_head_sha: str,
    ) -> None:
        self.git = git
        self.states = states
        self.agents = agents
        self.github = github
        self.default_branch = default_branch
        self.default_head_sha = default_head_sha

    def publish(self, run_id: str) -> dict[str, Any]:
        with self.states.locked():
            state = self._load(run_id)
            run = self._mapping(state, "run_acceptance")
            publication = self._publication_state(state)
            if publication["phase"] in {"merged", "abandoned"}:
                return state
            if publication["phase"] == "stale":
                publication.clear()
                publication["phase"] = "pending"
            if publication["phase"] == "publishing" and not isinstance(
                publication.get("artifact"), dict
            ):
                # A worker can die after its durable in-flight marker but
                # before it has produced an artifact. Retrying starts a fresh
                # one-shot publisher rather than treating a missing artifact
                # as a state corruption.
                publication["phase"] = "pending"
            if not self._acceptance_is_current(state, run):
                return self._invalidate_for_fresh_acceptance(state)

            if publication["phase"] == "pending":
                publication["phase"] = "publishing"
                self._save(state)
                checkout = self._publication_checkout(state)
                try:
                    self.git.prepare_validation_checkout(
                        head_sha=self.git.resolve(str(state["run_branch"])),
                        checkout=checkout,
                    )
                    artifact = PublicationArtifact.parse(
                        self.agents.run_publication(
                            self._publication_request(state, run, checkout)
                        ),
                        delivery_run=str(state["run_id"]),
                        final_run=True,
                    )
                finally:
                    self.git.remove_worktree(checkout)
                    self._remove_empty_directories(checkout)
                publication["artifact"] = {
                    "commit_message": artifact.commit_message,
                    "pr_title": artifact.pr_title,
                    "pr_body_markdown": artifact.pr_body_markdown,
                }

            artifact = PublicationArtifact.parse(
                self._mapping(publication, "artifact"),
                delivery_run=str(state["run_id"]),
                final_run=True,
            )
            run_head = self.git.resolve(str(state["run_branch"]))
            pr_number = self.github.ensure_run_pr(
                branch=str(state["run_branch"]),
                base_branch=self.default_branch,
                title=artifact.pr_title,
                body=self._render_final_run_pr_body(state, artifact.pr_body_markdown),
            )
            live = self.github.live_pull_request(pr_number)
            if (
                live.get("state") != "OPEN"
                or live.get("head_sha") != run_head
                or live.get("base_branch") != self.default_branch
                or live.get("base_sha") != self.default_head_sha
            ):
                raise ValueError("final Run PR does not match the accepted publication")
            record = self._record(state, run_head, str(live["head_sha"]))
            self.github.record_run_publication(pr_number, record)
            publication.update({"pr_number": pr_number, "record": record})
            checks = self.github.required_checks(pr_number)
            self._record_agent_run_status(pr_number, run, run_head, checks)
            if checks == "fail":
                self._queue_repair(
                    state,
                    repair_source="required_checks",
                    ci_evidence=self.github.required_check_evidence(pr_number),
                )
            elif checks == "pending":
                publication["phase"] = "waiting_checks"
                state["status"] = "waiting_checks"
                state["diagnostics"] = []
            else:
                publication["phase"] = "ready_for_approval"
                state["status"] = "run_approval_pending"
                state["terminal_kind"] = "waiting_human"
                state["diagnostics"] = []
            return self._save(state)

    def approve(self, run_id: str) -> dict[str, Any]:
        with self.states.locked():
            state = self._load(run_id)
            publication = self._publication_state(state)
            if publication["phase"] == "merged":
                return state
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
                    and live.get("integrated_parents") == [
                        record.get("default_head_sha"),
                        record.get("run_head_sha"),
                    ]
                ):
                    publication.update({"phase": "merged", "integrated_sha": integrated})
                    state.update({"status": "completed", "terminal_kind": "merged", "diagnostics": []})
                    return self._save(state)
                raise ValueError("merged final Run PR does not match its publication record")
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
                        merge_conflict_evidence="Final Run PR is no longer mergeable against the current default branch.",
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
                or merged.get("integrated_parents") != [
                    self.default_head_sha,
                    live.get("head_sha"),
                ]
            ):
                raise ValueError("final Run merge result does not preserve the expected boundary")
            publication.update({"phase": "merged", "integrated_sha": integrated})
            state.update({"status": "completed", "terminal_kind": "merged", "diagnostics": []})
            return self._save(state)

    def revise(self, run_id: str, feedback: str) -> dict[str, Any]:
        if not feedback.strip():
            raise ValueError("revision feedback must be non-empty")
        with self.states.locked():
            state = self._load(run_id)
            publication = self._publication_state(state)
            if publication["phase"] in {"merged", "abandoned"}:
                raise ValueError("a completed or abandoned Run cannot be revised")
            if state.get("status") not in {
                "ready_for_human",
                "run_approval_pending",
            }:
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
            worktree_root = self.states.root / "worktrees" / str(state["run_id"])
            shutil.rmtree(worktree_root, ignore_errors=True)
            publication["phase"] = "abandoned"
            state.update({"status": "abandoned", "terminal_kind": "abandoned", "diagnostics": []})
            return self._save(state)

    def _acceptance_is_current(
        self, state: dict[str, Any], run: dict[str, Any]
    ) -> bool:
        record = run.get("acceptance_record")
        if not isinstance(record, dict) or run.get("phase") != "accepted":
            return False
        return (
            record.get("reviewed_head_sha") == self.git.resolve(str(state["run_branch"]))
            and record.get("reviewed_default_base_sha") == self.default_head_sha
            and record.get("parent_revision") == self._mapping(state, "parent").get("revision")
            and record.get("ticket_graph_revision") == self._mapping(state, "ticket_graph").get("revision")
            and record.get("ticket_completion_records") == self._ticket_completion_records(state)
        )

    def _invalidate_for_fresh_acceptance(self, state: dict[str, Any]) -> dict[str, Any]:
        run = self._mapping(state, "run_acceptance")
        for key in ("acceptance_record", "acceptance_artifact", "reviewed_head_sha"):
            run.pop(key, None)
        run["phase"] = "pending"
        publication = self._publication_state(state)
        if publication["phase"] not in {"merged", "abandoned"}:
            publication["phase"] = "stale"
        state.update({"status": "run_acceptance_pending", "terminal_kind": "run_acceptance_stale", "diagnostics": []})
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

    def _queue_repair(
        self,
        state: dict[str, Any],
        *,
        repair_source: str,
        human_feedback: str | None = None,
        ci_evidence: dict[str, Any] | None = None,
        merge_conflict_evidence: str | None = None,
    ) -> dict[str, Any]:
        run = self._mapping(state, "run_acceptance")
        request: dict[str, Any] = {"repair_source": repair_source}
        if human_feedback is not None:
            request["human_feedback"] = human_feedback
            run["modification_attempts"] = 0
        if ci_evidence is not None:
            request["ci_evidence"] = ci_evidence
        if merge_conflict_evidence is not None:
            request["merge_conflict_evidence"] = merge_conflict_evidence
        run.update({"phase": "repairing", "repair_request": request})
        run.pop("repair_job", None)
        publication = state.get("run_publication")
        if isinstance(publication, dict) and publication.get("phase") not in {
            "merged",
            "abandoned",
        }:
            publication["phase"] = "stale"
        state.update({"status": "run_acceptance_pending", "terminal_kind": "run_repair_pending", "diagnostics": []})
        return state

    def _publication_request(
        self, state: dict[str, Any], run: dict[str, Any], checkout: Path
    ) -> dict[str, Any]:
        return {
            "run_id": state["run_id"],
            "parent": self._mapping(state, "parent"),
            "ticket_graph": self._mapping(state, "ticket_graph"),
            "ticket_completion_records": self._ticket_completion_records(state),
            "run_head_sha": self.git.resolve(str(state["run_branch"])),
            "default_head_sha": self.default_head_sha,
            "acceptance_record": self._mapping(run, "acceptance_record"),
            "checkout": str(checkout),
        }

    def _render_final_run_pr_body(self, state: dict[str, Any], narrative: str) -> str:
        parent = self._mapping(state, "parent")
        return (
            f"Parent Issue: #{int(parent['number'])}\n"
            "Delivery Type: Final Run\n\n"
            f"{narrative.strip()}"
        )

    def _record_agent_run_status(
        self, pr_number: int, run: dict[str, Any], run_head: str, checks: str
    ) -> None:
        artifact = self._mapping(run, "acceptance_artifact")
        raw_checks = self._mapping(artifact, "checks")
        lane_statuses = {
            lane: str(self._mapping(raw_checks, lane)["status"])
            for lane in ("e2e", "standards", "spec")
        }
        next_action = {
            "fail": "repair failed Required Checks",
            "pending": "wait for Required Checks",
        }.get(checks, "await explicit maintainer approval")
        self.github.record_agent_run_status(
            pr_number,
            {
                "scope": "final-run",
                "base_sha": self.default_head_sha,
                "candidate_sha": run_head,
                "validation_verdict": str(artifact["verdict"]),
                "lane_statuses": lane_statuses,
                "required_checks": checks,
                "next_action": next_action,
            },
        )

    def _record(self, state: dict[str, Any], run_head: str, pr_head: str) -> dict[str, Any]:
        return {
            "parent_revision": self._mapping(state, "parent")["revision"],
            "ticket_graph_revision": self._mapping(state, "ticket_graph")["revision"],
            "default_head_sha": self.default_head_sha,
            "run_head_sha": run_head,
            "pr_head_sha": pr_head,
        }

    def _publication_state(self, state: dict[str, Any]) -> dict[str, Any]:
        existing = state.get("run_publication")
        if isinstance(existing, dict):
            return existing
        publication = {"phase": "pending"}
        state["run_publication"] = publication
        return publication

    def _ticket_completion_records(self, state: dict[str, Any]) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for key, job in sorted(self._mapping(state, "ticket_jobs").items()):
            if isinstance(job, dict) and job.get("phase") == "completed":
                records.append({
                    "ticket_number": int(key),
                    "integrated_sha": job.get("integrated_sha"),
                    "acceptance_record": job.get("acceptance_record"),
                })
        return records

    def _publication_checkout(self, state: dict[str, Any]) -> Path:
        return self.states.root / "worktrees" / str(state["run_id"]) / "run-publication"

    @staticmethod
    def _remove_empty_directories(checkout: Path) -> None:
        for directory in (checkout.parent, checkout.parent.parent):
            try:
                directory.rmdir()
            except OSError:
                pass

    def _load(self, run_id: str) -> dict[str, Any]:
        state = self.states.load_run(run_id)
        if state is None:
            raise ValueError(f"unknown Delivery Run: {run_id}")
        return state

    @staticmethod
    def _mapping(data: dict[str, Any], key: str) -> dict[str, Any]:
        value = data.get(key)
        if not isinstance(value, dict):
            raise ValueError(f"{key} must be an object")
        return value

    @staticmethod
    def _integer(data: dict[str, Any], key: str) -> int:
        value = data.get(key)
        if not isinstance(value, int):
            raise ValueError(f"{key} must be an integer")
        return value

    def _save(self, state: dict[str, Any]) -> dict[str, Any]:
        self.states.save_run(str(state["run_id"]), state)
        return state
