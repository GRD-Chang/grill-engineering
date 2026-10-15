from __future__ import annotations

from typing import Any


PUBLICATION_PENDING_MESSAGE = (
    "Publication Operation Retry was exhausted; inspect the persisted failure and "
    "explicitly resume to renew its retry window and reconcile the original write intent"
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
