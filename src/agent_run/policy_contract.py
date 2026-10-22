"""Fixed Policy wire fields and numeric validation, independent of configuration and delivery."""

from __future__ import annotations

import math
from typing import Any

from agent_run.messages import error_message


class DeliveryPolicyError(ValueError):
    """The Delivery Policy is missing, malformed, or outside its contract."""


POLICY_VALUE_FIELDS = {
    "parent_only_paired_rounds": "parent_rounds",
    "run_repair_rounds": "run_repairs",
    "ticket_review_rounds": "ticket_rounds",
    "ticket_development_workers": "concurrency",
    "integration_resync_limit": "resync_limit",
    "ticket_integration_development_limit": "integration_developments",
    "ticket_ci_development_limit": "ticket_ci_developments",
    "ticket_ci_review": "ticket_ci_review",
    "parent_ci_development_limit": "parent_ci_developments",
}


def positive_integer(value: Any, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise DeliveryPolicyError(error_message('policy.error.positive_integer', audit=f"{name} must be a positive integer", name=name))
    return value


def nonnegative_integer(value: Any, name: str) -> int:
    if type(value) is not int or value < 0:
        raise DeliveryPolicyError(f"{name} must be a non-negative integer")
    return value


def positive_seconds(value: Any, name: str) -> float:
    """Validate numeric seconds; unit-bearing strings belong to the config parser."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DeliveryPolicyError("invocation deadlines must be positive finite durations")
    try:
        seconds = float(value)
    except OverflowError as error:
        raise DeliveryPolicyError(error_message('policy.error.positive_duration', audit=f"{name} must be a positive duration", name=name)) from error
    if not math.isfinite(seconds) or seconds <= 0:
        raise DeliveryPolicyError(error_message('policy.error.positive_duration', audit=f"{name} must be a positive duration", name=name))
    return seconds


def canonical_number(value: float) -> int | float:
    return int(value) if value.is_integer() else value
