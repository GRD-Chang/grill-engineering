"""Interpret an open PR snapshot against exact live refs; grant no authority."""
from collections.abc import Collection

from agent_run.model import Commit, PullRequest


def open_pr_target(pr: PullRequest, *, repository: str, pr_number: int | None,
                   source_ref: str, target_ref: str, source: Commit | None,
                   target: Commit | None, ancestry: Collection[tuple[str, str]]) -> Commit | None:
    """A reported ancestor base may lag the same target, never replace a grant.

    Callers still bind this target to their own Review, intent and approval.
    The original PR base remains evidence. Merged/closed receipts are separate.
    """
    if (pr.state != "OPEN" or pr.repository != repository or pr.head_repository != repository
            or pr_number is not None and pr.number != pr_number
            or pr.source_ref != source_ref or pr.target_ref != target_ref
            or source is None or target is None or pr.head != source):
        return None
    if pr.base == target or (pr.base.sha != target.sha and (pr.base.sha, target.sha) in ancestry):
        return target
    return None
