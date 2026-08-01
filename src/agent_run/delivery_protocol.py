from __future__ import annotations

from typing import Any, Protocol


class GitHubPublisher(Protocol):
    def ensure_run_repair_branch(
        self, *, branch: str, base_branch: str
    ) -> None: ...

    def ensure_run_repair_pr(
        self, *, branch: str, base_branch: str, title: str, body: str
    ) -> int: ...

    def ensure_ticket_branch(
        self,
        *,
        ticket_number: int,
        branch: str,
        base_branch: str,
    ) -> None: ...

    def publish_branch(
        self,
        branch: str,
        head_sha: str,
        *,
        expected_remote_sha: str,
    ) -> None: ...

    def ensure_ticket_pr(
        self,
        *,
        branch: str,
        base_branch: str,
        title: str,
        body: str,
        primary_ticket: int,
    ) -> int: ...

    def required_checks(self, pr_number: int) -> str: ...

    def required_check_evidence(self, pr_number: int) -> dict[str, Any]: ...

    def live_pull_request(self, pr_number: int) -> dict[str, Any]: ...

    def record_acceptance(
        self, pr_number: int, record: dict[str, Any]
    ) -> None: ...

    def squash_merge(
        self,
        *,
        pr_number: int,
        expected_head_sha: str,
        run_branch: str,
        commit_message: str,
    ) -> str: ...

    def sync_run_branch(
        self, *, run_branch: str, integrated_sha: str
    ) -> None: ...

    def close_primary_ticket(
        self,
        *,
        ticket_number: int,
        run_id: str,
        pr_number: int,
        integrated_sha: str,
    ) -> None: ...

    def mark_ready_for_human(self, ticket_number: int) -> None: ...

    def current_effective_revision(
        self,
        *,
        parent_number: int,
        ticket_number: int,
        expected_revision: str,
    ) -> str: ...
