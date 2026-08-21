from __future__ import annotations


class GitError(RuntimeError):
    """A managed Git operation could not preserve its required boundary."""


class MergeConflictError(GitError):
    """The exact default/Run merge needs semantic conflict resolution."""
