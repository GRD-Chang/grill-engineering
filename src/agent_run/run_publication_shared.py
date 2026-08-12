from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_run.agents import AgentBackend
from agent_run.delivery_protocol import GitHubPublisher
from agent_run.git import GitRepository
from agent_run.human_responses import current_human_response_history
from agent_run.revisions import effective_revision
from agent_run.state import StateStore


class RunPublicationShared:
    """Shared state and evidence operations for the Final Run publication flows."""

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

    def _acceptance_is_current(
        self, state: dict[str, Any], run: dict[str, Any]
    ) -> bool:
        record = run.get("acceptance_record")
        if not isinstance(record, dict) or run.get("phase") != "accepted":
            return False
        run_head = self.git.resolve(str(state["run_branch"]))
        return (
            record.get("reviewed_head_sha") == run_head
            and record.get("reviewed_default_base_sha") == self.default_head_sha
            and record.get("expected_merge_tree")
            == self.git.expected_merge_tree(
                default_head_sha=self.default_head_sha, run_head_sha=run_head
            )
            and record.get("parent_revision") == self._mapping(state, "parent").get("revision")
            and record.get("ticket_graph_revision")
            == self._mapping(state, "ticket_graph").get("revision")
            and record.get("ticket_completion_records")
            == ticket_completion_records(state)
        )

    def _invalidate_for_fresh_acceptance(self, state: dict[str, Any]) -> dict[str, Any]:
        run = self._mapping(state, "run_acceptance")
        for key in ("acceptance_record", "acceptance_artifact", "reviewed_head_sha"):
            run.pop(key, None)
        for key in (
            "human_response_history",
            "human_response_generation",
            "prior_human_blockers",
        ):
            run.pop(key, None)
        run["acceptance_generation"] = int(run.get("acceptance_generation", 1)) + 1
        run["phase"] = "pending"
        publication = self._publication_state(state)
        if publication["phase"] not in {"merged", "abandoned"}:
            publication["phase"] = "stale"
        state.update(
            {
                "status": "run_acceptance_pending",
                "terminal_kind": "run_acceptance_stale",
                "diagnostics": [],
            }
        )
        return self._save(state)

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
        state.update(
            {
                "status": "run_acceptance_pending",
                "terminal_kind": "run_repair_pending",
                "diagnostics": [],
            }
        )
        return state

    def _publication_request(self, state: dict[str, Any], checkout: Path) -> dict[str, Any]:
        parent = self._mapping(state, "parent")
        publication = self._publication_state(state)
        run = self._mapping(state, "run_acceptance")
        request: dict[str, Any] = {
            "parent_issue_url": (
                f"https://github.com/{state['repository']}/issues/{int(parent['number'])}"
            ),
            "checkout": str(checkout),
            "acceptance_artifact": self._mapping(run, "acceptance_artifact"),
        }
        if publication.get("prior_human_blockers"):
            request["prior_human_blockers"] = publication["prior_human_blockers"]
        if history := current_human_response_history(
            publication,
            generation=int(publication.get("human_response_generation", 1)),
        ):
            request["human_response_history"] = history
        if publication.get("thread_id"):
            request["thread_id"] = publication.get("thread_id")
        return request

    def _render_final_run_pr_body(self, state: dict[str, Any], narrative: str) -> str:
        parent = self._mapping(state, "parent")
        completed_tickets = "\n".join(self._completed_ticket_lines(state))
        return (
            f"Parent Issue: #{int(parent['number'])}\n"
            "Delivery Type: Final Run\n\n"
            f"## Completed Tickets\n\n{completed_tickets}\n\n"
            f"{narrative.strip()}"
        )

    def _completed_ticket_lines(self, state: dict[str, Any]) -> list[str]:
        tickets = self._mapping(self._mapping(state, "ticket_graph"), "tickets")
        lines: list[str] = []
        for key, job in sorted(self._mapping(state, "ticket_jobs").items()):
            if not isinstance(job, dict) or job.get("phase") != "completed":
                continue
            ticket = self._mapping(tickets, key)
            title = str(ticket.get("title", "")).strip()
            if not title:
                raise ValueError("completed Ticket is missing its title")
            number = int(key)
            pr_number = job.get("pr_number")
            if isinstance(pr_number, int):
                lines.append(
                    f"- [#{number}: {title}](https://github.com/{state['repository']}/pull/{pr_number})"
                )
            else:
                lines.append(f"- #{number}: {title}")
        if not lines:
            raise ValueError("Final Run requires completed Tickets")
        return lines

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
        run = self._mapping(state, "run_acceptance")
        return {
            "parent_revision": self._mapping(state, "parent")["revision"],
            "ticket_graph_revision": self._mapping(state, "ticket_graph")["revision"],
            "default_head_sha": self.default_head_sha,
            "run_head_sha": run_head,
            "pr_head_sha": pr_head,
            "expected_merge_tree": self._mapping(run, "acceptance_record")[
                "expected_merge_tree"
            ],
        }

    def _publication_state(self, state: dict[str, Any]) -> dict[str, Any]:
        existing = state.get("run_publication")
        if isinstance(existing, dict):
            return existing
        publication = {"phase": "pending"}
        state["run_publication"] = publication
        return publication

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
