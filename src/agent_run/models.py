from __future__ import annotations

from dataclasses import dataclass

# Fixture and revision consumers keep these imports, sharing the canonical values.
from agent_run_kernel.model import Blocker as Blocker, Issue as Issue, ParentIssue as ParentIssue


@dataclass(frozen=True)
class Repository:
    name_with_owner: str
    default_branch: str
    default_head_sha: str | None


def same_repository(left: object, right: object) -> bool:
    """GitHub owner/name is case-insensitive; retain its display spelling."""
    return isinstance(left, str) and isinstance(right, str) and left.lower() == right.lower()


@dataclass(frozen=True)
class DeliveryGraph:
    parent: ParentIssue
    issues: dict[int, Issue]
