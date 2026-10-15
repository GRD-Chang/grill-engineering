"""Resolve fixed delivery budget ownership without mutating work subjects."""
from __future__ import annotations

from typing import Any


def delivery_budget_owner(
    state: dict[str, Any], subject: dict[str, Any],
) -> dict[str, Any]:
    """Keep Parent repair allowances on the Parent delivery object."""
    acceptance = state.get("run_acceptance")
    if isinstance(acceptance, dict) and subject is acceptance.get("repair_job"):
        return acceptance
    return subject


def repair_integration_budget_owner(
    state: dict[str, Any], subject: dict[str, Any],
) -> dict[str, Any]:
    """Read the object's resync window for this Repair's integration scope.

    Repair refs have their own existing refresh windows; sharing the mainline
    source/target window would change which external writes are charged.
    """
    owner = delivery_budget_owner(state, subject)
    if owner is subject:
        return subject
    windows = owner.get("repair_integration_budgets")
    window = windows.get(str(subject["repair_generation"])) if isinstance(windows, dict) else None
    if not isinstance(window, dict):
        raise ValueError("missing Parent Repair integration budget window")
    return window
