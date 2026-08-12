from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol


class GitHubPublisher(Protocol):
    def delete_managed_branch(self, branch: str) -> None: ...

    def ensure_parent_branch(
        self, *, parent_number: int, branch: str, base_branch: str
    ) -> None: ...

    def ensure_run_pr(
        self, *, branch: str, base_branch: str, title: str, body: str
    ) -> int: ...

    def record_run_publication(
        self, pr_number: int, record: dict[str, Any]
    ) -> None: ...

    def normal_merge(
        self, *, pr_number: int, expected_head_sha: str
    ) -> str: ...

    def close_parent_issue(
        self,
        *,
        parent_number: int,
        run_id: str,
        pr_number: int,
        integrated_sha: str,
        delivery_type: str,
    ) -> None: ...

    def abandon_run_pr(self, pr_number: int) -> None: ...

    def abandon_change_pr(self, pr_number: int) -> bool: ...

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

    def ensure_parent_pr(
        self, *, branch: str, base_branch: str, title: str, body: str
    ) -> int: ...

    def abandon_parent_pr(self, pr_number: int) -> bool: ...

    def has_supersession_close_receipt(
        self, pr_number: int, generation: object, close_nonce: object
    ) -> bool: ...

    def has_supersession_close_record(
        self, pr_number: int, generation: object, close_nonce: object
    ) -> bool: ...

    def has_supersession_close_intent(
        self, pr_number: int, generation: object, close_nonce: object
    ) -> bool: ...

    def publication_context(self, pr_number: int) -> dict[str, object]: ...

    def required_checks(self, pr_number: int) -> str: ...

    def required_check_evidence(self, pr_number: int) -> dict[str, Any]: ...

    def live_pull_request(self, pr_number: int) -> dict[str, Any]: ...

    def record_acceptance(
        self, pr_number: int, record: dict[str, Any]
    ) -> None: ...

    def record_agent_run_status(
        self, pr_number: int, status: dict[str, Any]
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

    def prepare_primary_ticket_close(
        self,
        *,
        ticket_number: int,
        run_id: str,
        pr_number: int,
        integrated_sha: str,
    ) -> dict[str, Any] | None: ...

    def close_primary_ticket(
        self,
        *,
        ticket_number: int,
        run_id: str,
        pr_number: int,
        integrated_sha: str,
        close_intent: dict[str, Any] | None = None,
        before_dispatch: Callable[[], None] | None = None,
    ) -> dict[str, Any] | None: ...

    def ticket_closed_by_run(
        self,
        *,
        ticket_number: int,
        run_id: str,
        recorded_ownership: dict[str, Any] | None,
    ) -> bool: ...

    def ticket_close_ownership(
        self,
        *,
        ticket_number: int,
        run_id: str,
        recorded_ownership: dict[str, Any] | None,
    ) -> dict[str, Any] | None: ...

    def recover_abandoned_ticket(
        self,
        *,
        ticket_number: int,
        run_id: str,
        pr_number: int,
        integrated_sha: str,
        expected_ownership: dict[str, Any],
    ) -> bool: ...

    def mark_ready_for_human(self, ticket_number: int) -> None: ...

    def current_effective_revision(
        self,
        *,
        parent_number: int,
        ticket_number: int,
        expected_revision: str,
    ) -> str: ...
