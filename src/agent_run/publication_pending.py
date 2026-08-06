from __future__ import annotations

from typing import Any


PUBLICATION_PENDING_MESSAGE = (
    "Publication retries were exhausted; resume retries publication without "
    "rerunning Development or Fresh Validation"
)


def publication_pending_diagnostic(
    *, subject_key: str, subject: str | int
) -> dict[str, Any]:
    """Return the durable diagnostic for an exhausted publication retry loop."""
    return {
        "code": "publication_pending",
        "message": PUBLICATION_PENDING_MESSAGE,
        subject_key: subject,
    }
