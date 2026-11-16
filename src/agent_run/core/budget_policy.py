"""Pure allowance calculations; admission and window commits belong to Core."""
from collections.abc import Mapping
from typing import Any

from agent_run.model import DeliveryKind, Policy, Purpose


def budget_limits(policy: Policy, kind: DeliveryKind) -> dict[Purpose, tuple[int, int | None]]:
    return {
        Purpose.REQUIREMENTS: (policy.ticket_rounds + 1, policy.ticket_rounds)
        if kind == DeliveryKind.TICKET else (policy.parent_rounds, policy.parent_rounds),
        Purpose.INTEGRATION: (policy.run_repairs, policy.run_repairs + 1)
        if kind == DeliveryKind.FINAL_RUN else (policy.run_repairs if kind == DeliveryKind.PARENT_ONLY
                                               else policy.integration_developments,
                                               0 if kind == DeliveryKind.TICKET else None),
        Purpose.CI: (policy.ticket_ci_developments if kind == DeliveryKind.TICKET else policy.parent_ci_developments, None),
    }


def requires_review(policy: Policy, kind: DeliveryKind, purpose: Purpose) -> bool:
    return (kind != DeliveryKind.TICKET or purpose == Purpose.REQUIREMENTS
            or purpose == Purpose.CI and policy.ticket_ci_review)


def renewed_budget(policy: Policy, kind: DeliveryKind, purpose: Purpose, previous: Mapping[str, Any],
                   *, new_cycle: bool = False) -> tuple[int, int, int, int | None, bool]:
    """Return usage, limits and review requirement for the next budget window."""
    limit, review_limit = budget_limits(policy, kind)[purpose]
    developments, reviews = 0, 0
    if purpose == Purpose.CI:
        reviews = previous["reviews_used"]  # A cumulative count, not a renewable quota.
    if purpose == Purpose.INTEGRATION and kind == DeliveryKind.FINAL_RUN and not new_cycle:
        developments = (0 if previous["developments_used"] >= previous["development_limit"]
                        else previous["developments_used"])
        reviews = (0 if previous["review_limit"] is not None and previous["reviews_used"] >= previous["review_limit"]
                   else previous["reviews_used"])
    if purpose == Purpose.INTEGRATION and kind != DeliveryKind.FINAL_RUN:
        developments, reviews = previous["developments_used"], previous["reviews_used"]
        limit += previous["development_limit"]  # Extend allowance without erasing use.
    return developments, reviews, limit, review_limit, requires_review(policy, kind, purpose)
