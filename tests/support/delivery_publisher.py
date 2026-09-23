"""共享 GitHub Publisher fake，按实例保存发布状态。"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any
from agent_run.git import GitRepository
from agent_run.branch_publication import BranchPublicationResult, RemoteBranchObservation


class ScriptedPublisher:
    def __init__(self, repo: Path) -> None:
        self.repo = repo
        self.pr_number = 11
        self.created_prs = 0
        self.pr_bodies: list[str] = []
        self.pr_titles: list[str] = []
        self.closed_issues: list[int] = []
        self.agent_run_statuses: list[dict[str, Any]] = []
        self.live_head: str | None = None
        self.branch: str | None = None
        self.base_branch: str | None = None
        self.revision_override: str | None = None
        self.checks = ["pass"]
        self.failed_check_evidence = {
            "pr_number": self.pr_number,
            "checks": [
                {
                    "name": "test",
                    "workflow": "ci",
                    "bucket": "fail",
                    "state": "FAILURE",
                    "repairability": "code_failure",
                    "description": "The test job failed.",
                    "link": "https://example.invalid/checks/test",
                }
            ],
        }
        self.check_position = 0
        self.merged_sha: str | None = None
        self.merged_head: str | None = None
        self.publication_context_calls: list[int] = []
        self.prepared_ticket_closes: list[dict[str, Any]] = []

    def ensure_parent_branch(
        self, *, parent_number: int, branch: str, base_branch: str
    ) -> None:
        del parent_number, branch, base_branch

    def ensure_ticket_branch(
        self,
        *,
        ticket_number: int,
        branch: str,
        base_branch: str,
        expected_base_sha: str,
        expected_remote_sha: str,
        recovery_remote_sha: str,
    ) -> None:
        del (
            ticket_number, branch, base_branch, expected_base_sha,
            expected_remote_sha, recovery_remote_sha,
        )

    def publish_branch(
        self,
        branch: str,
        head_sha: str,
        *,
        expected_remote_sha: str,
    ) -> BranchPublicationResult:
        observation = RemoteBranchObservation(
            branch, expected_remote_sha, self.live_head or expected_remote_sha
        )
        if self.live_head == head_sha:
            return BranchPublicationResult(observation, head_sha, "already_published")
        if self.live_head not in {None, expected_remote_sha}:
            raise ValueError("scripted remote ticket branch drifted")
        self.live_head = head_sha
        return BranchPublicationResult(observation, head_sha, "published")

    def verify_ticket_pr_before_publish(
        self,
        *,
        branch: str,
        base_branch: str,
        expected_head_sha: str,
        expected_base_sha: str,
    ) -> None:
        del branch, base_branch, expected_head_sha, expected_base_sha

    def ensure_ticket_pr(
        self,
        *,
        branch: str,
        base_branch: str,
        title: str,
        body: str,
        primary_ticket: int,
        expected_head_sha: str,
        expected_base_sha: str,
    ) -> int:
        del expected_head_sha, expected_base_sha
        self.created_prs += 1
        self.branch = branch
        self.base_branch = base_branch
        self.pr_bodies.append(body)
        self.pr_titles.append(title)
        return self.pr_number

    def publication_context(self, pr_number: int) -> dict[str, object]:
        assert pr_number == self.pr_number
        self.publication_context_calls.append(pr_number)
        return {
            "number": pr_number,
            "url": f"https://example.invalid/pull/{pr_number}",
            "title": self.pr_titles[-1],
        }

    def required_checks(self, pr_number: int) -> str:
        value = self.checks[min(self.check_position, len(self.checks) - 1)]
        self.check_position += 1
        return value

    def required_checks_snapshot(
        self, pr_number: int, *, expected_head_sha: str
    ) -> dict[str, Any]:
        assert pr_number == self.pr_number
        assert self.live_pull_request(pr_number)["head_sha"] == expected_head_sha
        result = self.checks[min(self.check_position, len(self.checks) - 1)]
        self.check_position += 1
        configured = self.failed_check_evidence.get("checks")
        if result == "fail" and isinstance(configured, list):
            checks = [
                {
                    key: check[key]
                    for key in (
                        "name",
                        "workflow",
                        "bucket",
                        "state",
                        "description",
                        "link",
                    )
                    if key in check
                }
                for check in configured
                if isinstance(check, dict)
            ]
        elif result == "none":
            checks = []
        else:
            checks = [
                {
                    "name": "test",
                    "workflow": "ci",
                    "bucket": result,
                    "link": "https://example.invalid/checks/test",
                }
            ]
        return {
            "pr_number": pr_number,
            "head_sha": expected_head_sha,
            "result": result,
            "checks": checks,
        }

    def required_check_evidence(
        self, pr_number: int, *, expected_head_sha: str | None = None
    ) -> dict[str, Any]:
        del expected_head_sha
        assert pr_number == self.pr_number
        return self.failed_check_evidence

    def live_pull_request(self, pr_number: int) -> dict[str, Any]:
        result = {
            "head_branch": self.branch,
            "head_sha": self.live_head,
            "head_repository": "example/project",
            "base_branch": self.base_branch,
            "base_sha": (
                subprocess.run(
                    ["git", "rev-parse", str(self.base_branch)],
                    cwd=self.repo,
                    text=True,
                    capture_output=True,
                    check=True,
                ).stdout.strip()
                if self.base_branch
                else None
            ),
            "base_repository": "example/project",
            "mergeable": True,
            "state": "OPEN",
        }
        if self.merged_sha is not None and self.merged_head is not None:
            result.update(
                {
                    "head_sha": self.merged_head,
                    "base_sha": GitRepository(self.repo).commit_parents(
                        self.merged_sha
                    )[0],
                    "mergeable": False,
                    "state": "MERGED",
                    "integrated_sha": self.merged_sha,
                    "head_tree": _tree(self.repo, self.merged_head),
                    "integrated_tree": _tree(self.repo, self.merged_sha),
                    "integrated_message": GitRepository(self.repo).commit_subject(
                        self.merged_sha
                    ),
                    "integrated_parents": GitRepository(self.repo).commit_parents(
                        self.merged_sha
                    ),
                }
            )
        return result

    def record_agent_run_status(self, pr_number: int, status: dict[str, Any]) -> None:
        self.agent_run_statuses[:] = [
            existing
            for existing in self.agent_run_statuses
            if existing["pr_number"] != pr_number
        ]
        self.agent_run_statuses.append({"pr_number": pr_number, **status})

    def squash_merge(
        self,
        *,
        pr_number: int,
        expected_head_sha: str,
        run_branch: str,
        commit_message: str,
    ) -> str:
        del pr_number
        tree = subprocess.run(
            ["git", "rev-parse", f"{expected_head_sha}^{{tree}}"],
            cwd=self.repo,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        parent = subprocess.run(
            ["git", "rev-parse", run_branch],
            cwd=self.repo,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        integrated = subprocess.run(
            ["git", "commit-tree", tree, "-p", parent, "-m", commit_message],
            cwd=self.repo,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        subprocess.run(
            [
                "git",
                "update-ref",
                f"refs/heads/{run_branch}",
                integrated,
                parent,
            ],
            cwd=self.repo,
            check=True,
        )
        self.merged_sha = integrated
        self.merged_head = expected_head_sha
        return integrated

    def sync_run_branch(self, *, run_branch: str, integrated_sha: str) -> None:
        subprocess.run(
            ["git", "update-ref", f"refs/heads/{run_branch}", integrated_sha],
            cwd=self.repo,
            check=True,
        )

    def prepare_primary_ticket_close(
        self,
        *,
        ticket_number: int,
        run_id: str,
        pr_number: int,
        integrated_sha: str,
    ) -> dict[str, Any]:
        self.prepared_ticket_closes.append(
            {"pr_number": pr_number, "integrated_sha": integrated_sha}
        )
        return {
            "actor": "scripted-publisher",
            "event_id": None,
            "intent_created_at": f"{run_id}:ticket-{ticket_number}:intent",
            "baseline_event_id": 0,
            "intent_binding": f"pr-{pr_number}:sha-{integrated_sha}",
        }

    def close_primary_ticket(
        self,
        *,
        ticket_number: int,
        run_id: str,
        pr_number: int,
        integrated_sha: str,
        close_intent: dict[str, Any] | None = None,
        before_dispatch: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        del run_id, pr_number, integrated_sha, close_intent
        if before_dispatch is not None:
            before_dispatch()
        self.closed_issues.append(ticket_number)
        return {"actor": "scripted-publisher", "event_id": ticket_number}

    def current_effective_revision(
        self,
        *,
        parent_number: int,
        ticket_number: int,
        expected_revision: str,
    ) -> str:
        return self.revision_override or expected_revision


def _tree(repository: Path, sha: str) -> str:
    return subprocess.run(
        ["git", "rev-parse", f"{sha}^{{tree}}"],
        cwd=repository,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
