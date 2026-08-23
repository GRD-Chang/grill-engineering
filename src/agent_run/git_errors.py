from __future__ import annotations


class GitError(RuntimeError):
    """A managed Git operation could not preserve its required boundary."""


class GitIntegrityError(GitError):
    """A Worker changed the managed checkout outside the Candidate seam."""

    def __init__(self, message: str, *, evidence: dict[str, str]) -> None:
        super().__init__(message)
        self.evidence = evidence


class MergeConflictError(GitError):
    """The exact default/Run merge needs semantic conflict resolution."""
