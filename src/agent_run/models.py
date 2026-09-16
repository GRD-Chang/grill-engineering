from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Blocker:
    number: int
    state: str


@dataclass(frozen=True)
class Issue:
    number: int
    title: str
    body: str
    state: str
    labels: frozenset[str]
    blocked_by: tuple[Blocker, ...]


@dataclass(frozen=True)
class ParentIssue:
    number: int
    title: str
    body: str
    sub_issue_numbers: tuple[int, ...]
    sub_issue_order_reliable: bool


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
