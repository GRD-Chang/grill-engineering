from __future__ import annotations


class IncompatibleRunStateError(ValueError):
    """A persisted Run predates the one supported Invocation/Generation shape."""


class MissingRunMaterialsError(IncompatibleRunStateError):
    """A current Run needs trusted materials before its facts can be verified.

    Like incompatible state, this must never be recorded over residual state.
    Unlike legacy state, the same public resume can retry after manual repair.
    """
