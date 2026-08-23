from __future__ import annotations


class IncompatibleRunStateError(ValueError):
    """A persisted Run predates the one supported Invocation/Generation shape."""
