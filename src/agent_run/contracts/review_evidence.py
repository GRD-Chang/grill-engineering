"""Candidate equality shared by recorded Review views and current authority.

Callers own their evidence: a query supplies the recorded integration target;
Core supplies the freshly observed target. Equality never grants a write.
"""
from typing import Any, Protocol


class _CandidateFields(Protocol):
    def __getitem__(self, key: str, /) -> Any: ...


def same_review_candidate(review: _CandidateFields, candidate: _CandidateFields, *,
                          base_sha: str | None) -> bool:
    """Compare only fields with the same meaning at both observation sites.

    Review role, generation, outcome and original source observations remain
    with the caller, as do current Required Checks and explicit approval.
    """
    return bool(review["base_sha"] == base_sha
                and all(review[key] == candidate[key] for key in (
                    "revision", "candidate_sha", "candidate_tree", "expected_tree", "completion_revision")))
