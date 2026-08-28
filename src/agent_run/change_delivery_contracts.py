from __future__ import annotations

"""Typed contracts and mutation ports for shared Change Delivery."""

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Protocol


class StaleDisposition(str, Enum):
    """The shared engine's deterministic recovery for a stale change job."""

    BLOCK = "block"
    FRESH_RUN_ACCEPTANCE = "fresh_run_acceptance"


@dataclass(frozen=True)
class ChangeJobContract:
    """Typed, deterministic descriptor for a Change Delivery consumer."""

    label: str
    branch: str
    base_branch: str


class ChangeDeliveryAdapter(Protocol):
    """Consumer semantics only; concrete adapters live beside each consumer."""

    stale_disposition: StaleDisposition
    classify_required_check_failures: bool

    def development_thread_is_allowed(
        self, state: dict[str, Any], thread_id: str
    ) -> bool: ...

    def development_request(
        self, state: dict[str, Any], job: dict[str, Any], checkout: Path
    ) -> dict[str, Any]: ...

    def publication_request(
        self, state: dict[str, Any], job: dict[str, Any], checkout: Path
    ) -> dict[str, Any]: ...

    def review_request(
        self, state: dict[str, Any], job: dict[str, Any], checkout: Path
    ) -> dict[str, Any]: ...

    def acceptance_record(
        self,
        state: dict[str, Any],
        job: dict[str, Any],
        reviewer_thread_id: str,
        artifact: dict[str, Any],
    ) -> dict[str, Any]: ...

    def acceptance_is_current(
        self, state: dict[str, Any], job: dict[str, Any], acceptance: dict[str, Any]
    ) -> bool: ...

    def revision_changed(self, state: dict[str, Any], job: dict[str, Any]) -> bool: ...

    def base_is_current(self, current_base: str, job: dict[str, Any]) -> bool: ...

    def requires_explicit_approval(
        self, state: dict[str, Any], job: dict[str, Any]
    ) -> bool: ...

    def resume_after_required_checks_failure(
        self, state: dict[str, Any], job: dict[str, Any]
    ) -> bool:
        """Restore consumer state and return whether this invocation must yield."""
        ...

    def linked_issue_number(
        self, state: dict[str, Any], job: dict[str, Any]
    ) -> int | None: ...

    def invocation_identity(
        self, state: dict[str, Any], job: dict[str, Any]
    ) -> tuple[str, int]: ...


class ChangeDeliveryPublisher(Protocol):
    """Consumer-specific mutations executed through the Publisher seam."""

    def commit_candidate(
        self, checkout: Path, job: dict[str, Any], attempt: int
    ) -> str | None: ...

    def create_publication_commit(
        self, checkout: Path, job: dict[str, Any], message: str
    ) -> str: ...

    def prepare_validation(
        self, checkout: Path, job: dict[str, Any], validation: Path
    ) -> None: ...

    def ensure_pr(
        self,
        state: dict[str, Any],
        job: dict[str, Any],
        publication: dict[str, Any],
    ) -> int: ...

    def live_pull_request(
        self, state: dict[str, Any], job: dict[str, Any], pr_number: int
    ) -> dict[str, Any]: ...

    def invalidate_stale(
        self, state: dict[str, Any], job: dict[str, Any], checkout: Path
    ) -> None: ...

    def after_merge(
        self, state: dict[str, Any], job: dict[str, Any], live: dict[str, Any]
    ) -> bool: ...

    def merge(
        self, state: dict[str, Any], job: dict[str, Any], publication: dict[str, Any]
    ) -> str: ...

    def merge_description(self, job: dict[str, Any]) -> str: ...

    def escalate(
        self, state: dict[str, Any], job: dict[str, Any], code: str
    ) -> None: ...

    def sync_attempts(self, state: dict[str, Any], job: dict[str, Any]) -> None: ...
