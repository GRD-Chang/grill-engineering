"""Ticket label qualification at explicit work-entry boundaries."""

from __future__ import annotations

from collections.abc import Set


DISQUALIFYING_LABELS = frozenset({"needs-triage", "needs-info", "ready-for-human"})


class TicketEligibilityError(ValueError):
    """A requested entry is refused without consuming the paused work."""


def label_blocker(labels: Set[str]) -> str | None:
    disqualifying = sorted(labels & DISQUALIFYING_LABELS)
    if disqualifying:
        return f"disqualifying_label:{disqualifying[0]}"
    if "ready-for-agent" not in labels:
        return "missing_ready_for_agent"
    return None
