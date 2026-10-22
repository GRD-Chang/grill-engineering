from __future__ import annotations

"""User-level Delivery Policy resolution and immutable Run snapshots."""

import math
import re
from collections.abc import Mapping
from functools import lru_cache

from agent_run.messages import error_message
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agent_run_kernel.model import Policy


DELIVERY_POLICY_PROTOCOL = 9
_POLICY_KEYS = frozenset(
    {
        "ticket_development_workers",
        "parent_only_paired_rounds",
        "run_repair_rounds",
        "ticket_review_rounds",
        "integration_resync_limit",
        "ticket_integration_development_limit",
        "ticket_ci_development_limit",
        "ticket_ci_review",
        "parent_ci_development_limit",
        "development_thread_policy",
        "invocation_deadlines",
    }
)
_DEADLINE_KEYS = frozenset({"development", "review", "publication"})
_LEGACY_POLICY_KEYS = _POLICY_KEYS - {"development_thread_policy"}
_POLICY_SCALAR_FIELDS = (
    "parent_only_paired_rounds", "run_repair_rounds", "ticket_review_rounds",
    "ticket_development_workers", "integration_resync_limit",
    "ticket_integration_development_limit", "ticket_ci_development_limit",
    "ticket_ci_review", "parent_ci_development_limit", "development_thread_policy",
)
_DEADLINE_FIELDS = ("development", "review", "publication")
_PolicyScalar = bool | int | float | str
_PolicyCacheKey = tuple[tuple[type[object], _PolicyScalar], ...]
_DURATION_PATTERN = re.compile(
    r"^\s*([0-9]+(?:\.[0-9]+)?)\s*([smhd]?)\s*$", re.IGNORECASE
)
_DURATION_MULTIPLIERS = {"": 1.0, "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}


class DeliveryPolicyError(ValueError):
    """The Delivery Policy is missing, malformed, or outside its contract."""


# Canonical wire keys map once to the native frozen value's stable fields.
# This is a fixed contract, not a configurable field/schema interpreter.
_POLICY_VALUE_FIELDS = {
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


def default_delivery_policy() -> Policy:
    from agent_run_kernel.model import Policy
    return Policy()


def resolve_delivery_policy(
    *,
    user_defaults: Mapping[str, Any] | Policy | None = None,
    command_overrides: Mapping[str, Any] | None = None,
) -> Policy:
    """Resolve builtin values, user defaults, then one-shot overrides."""

    from agent_run_kernel.model import Policy

    if isinstance(user_defaults, Policy):
        values = user_defaults.snapshot()
        user_defaults = None  # A complete frozen owner is the baseline, not sparse defaults.
    else:
        values = default_delivery_policy().snapshot()
    values["invocation_deadlines"] = dict(values["invocation_deadlines"])
    for source, raw, ignore_none in (
        ("user defaults", user_defaults, False),
        ("command overrides", command_overrides, True),
    ):
        if raw is None:
            continue
        normalized = normalize_policy_overrides(raw, ignore_none=ignore_none)
        _merge_policy_values(values, normalized, source)
    return _policy_from_values(values)


def normalize_policy_overrides(
    raw: Mapping[str, Any], *, ignore_none: bool = False
) -> dict[str, Any]:
    """Normalize sparse config/CLI fields to the canonical policy keys."""

    if not isinstance(raw, Mapping):
        raise DeliveryPolicyError(error_message('policy.error.object', audit="Delivery Policy must be an object"))
    normalized: dict[str, Any] = {}
    deadlines: dict[str, Any] = {}

    def put(key: str, value: Any) -> None:
        if value is None and ignore_none:
            return
        if key in normalized and normalized[key] != value:
            raise DeliveryPolicyError(error_message('policy.error.duplicate_option', audit=f"Delivery Policy option {key!r} is duplicated", field=key))
        normalized[key] = value

    def put_deadline(role: str, value: Any) -> None:
        if value is None and ignore_none:
            return
        if role in deadlines and deadlines[role] != value:
            raise DeliveryPolicyError(
                error_message('policy.error.duplicate_deadline', audit=f"Delivery Policy deadline {role!r} is duplicated", role=role)
            )
        deadlines[role] = value

    round_aliases = {
        "parent_only_paired_round": "parent_only_paired_rounds",
        "parent_only_paired_rounds": "parent_only_paired_rounds",
        "run_repair_round": "run_repair_rounds",
        "run_repair_rounds": "run_repair_rounds",
        "ticket_review_rounds": "ticket_review_rounds",
        "ticket_review_round": "ticket_review_rounds",
        "ticket_development_workers": "ticket_development_workers",
        "integration_resync_limit": "integration_resync_limit",
        "ticket_integration_development_limit": "ticket_integration_development_limit",
        "ticket_ci_development_limit": "ticket_ci_development_limit",
        "ticket_ci_review": "ticket_ci_review",
        "parent_ci_development_limit": "parent_ci_development_limit",
    }
    flat_deadline_aliases = {
        role: {
            f"{role}_deadline",
            f"{role}_duration",
            f"{role}_timeout",
            f"{role}_deadline_seconds",
            f"{role}_timeout_seconds",
        }
        for role in _DEADLINE_KEYS
    }
    for key, value in raw.items():
        if key == "development_thread_policy":
            put(key, value)
            continue
        if key in round_aliases:
            put(round_aliases[key], value)
            continue
        if key in {"invocation_deadlines", "deadlines"}:
            if value is None and ignore_none:
                continue
            if not isinstance(value, Mapping):
                raise DeliveryPolicyError(error_message('policy.error.option_object', audit=f"{key} must be an object", field=key))
            for role, deadline in value.items():
                if role not in _DEADLINE_KEYS:
                    raise DeliveryPolicyError(
                        error_message('policy.error.deadline_role', audit=f"unknown Delivery Policy deadline role: {role!r}", role=role)
                    )
                put_deadline(role, deadline)
            continue
        matched_role = next(
            (role for role, aliases in flat_deadline_aliases.items() if key in aliases),
            None,
        )
        if matched_role is not None:
            put_deadline(matched_role, value)
            continue
        raise DeliveryPolicyError(error_message('policy.error.unknown_option', audit=f"unknown Delivery Policy option: {key}", field=key))
    if deadlines:
        normalized["invocation_deadlines"] = deadlines
    return normalized


def parse_policy_snapshot(value: object) -> Policy:
    """Parse the complete canonical state snapshot without applying defaults."""

    if not isinstance(value, Mapping) or set(value) not in (
        _POLICY_KEYS, _LEGACY_POLICY_KEYS
    ):
        raise DeliveryPolicyError("Policy Snapshot has an invalid field set")
    deadlines = value.get("invocation_deadlines")
    if not isinstance(deadlines, Mapping) or set(deadlines) != _DEADLINE_KEYS:
        raise DeliveryPolicyError("Policy Snapshot deadlines have an invalid field set")
    key = _policy_cache_key(value, deadlines)
    if key is not None:
        return _cached_policy(key)
    return _policy_with_deadlines(value, deadlines)


def _policy_cache_key(
    values: Mapping[str, Any], deadlines: Mapping[str, Any],
) -> _PolicyCacheKey | None:
    # Custom mappings/values retain their original access and validation path.
    # Bound retained scalar sizes as well as the number of successful entries.
    if type(values) is not dict or type(deadlines) is not dict:
        return None
    if any(type(field) is not str for field in values) or any(
        type(field) is not str for field in deadlines
    ):
        return None
    scalars = tuple(values.get(field, "reuse") for field in _POLICY_SCALAR_FIELDS) + tuple(
        deadlines[field] for field in _DEADLINE_FIELDS
    )
    key: list[tuple[type[object], _PolicyScalar]] = []
    for scalar in scalars:
        scalar_type = type(scalar)
        if scalar_type is str:
            if len(scalar) > 128:
                return None
        elif scalar_type is int:
            if scalar.bit_length() > 64:
                return None
        elif scalar_type is float:
            if not math.isfinite(scalar):
                return None
        elif scalar_type is not bool:
            return None
        key.append((scalar_type, scalar))
    # Python compares bool/int/float values equal; retain each exact type.
    return tuple(key)


@lru_cache(maxsize=128)
def _cached_policy(key: _PolicyCacheKey) -> Policy:
    scalar_count = len(_POLICY_SCALAR_FIELDS)
    values = dict(zip(_POLICY_SCALAR_FIELDS, (scalar for _, scalar in key[:scalar_count])))
    deadlines = dict(zip(_DEADLINE_FIELDS, (scalar for _, scalar in key[scalar_count:])))
    # lru_cache retains only successful immutable results, never exceptions or
    # the caller's mutable snapshot. Shape checks still run before every hit.
    return _policy_with_deadlines(values, deadlines)


def _merge_policy_values(
    target: dict[str, Any], source: Mapping[str, Any], source_name: str
) -> None:
    if set(source) - _POLICY_KEYS:
        raise DeliveryPolicyError(error_message('policy.error.unknown_fields', audit=f"{source_name} contains unknown policy fields", source_name=source_name))
    for key, value in source.items():
        if key == "invocation_deadlines":
            if not isinstance(value, Mapping):
                raise DeliveryPolicyError(error_message('policy.error.deadlines_object', audit=f"{source_name} deadlines must be an object", source_name=source_name))
            target["invocation_deadlines"].update(value)
        else:
            target[key] = value


def _policy_from_values(values: Mapping[str, Any]) -> Policy:
    deadlines = values.get("invocation_deadlines")
    if not isinstance(deadlines, Mapping):
        raise DeliveryPolicyError(error_message('policy.error.invocation_object', audit="invocation_deadlines must be an object"))
    if set(deadlines) - _DEADLINE_KEYS:
        raise DeliveryPolicyError(error_message('policy.error.invocation_role', audit="invocation_deadlines contains an unknown role"))
    required = _DEADLINE_KEYS - set(deadlines)
    if required:
        raise DeliveryPolicyError(
            "invocation_deadlines is missing: " + ", ".join(sorted(required))
        )
    return _policy_with_deadlines(values, deadlines)


def _policy_with_deadlines(
    values: Mapping[str, Any], deadlines: Mapping[str, Any],
) -> Policy:
    """Construct after the caller has checked its required deadline fields."""
    from agent_run_kernel.model import Policy

    thread_policy = values.get("development_thread_policy", "reuse")
    if thread_policy not in ("reuse", "new-per-attempt"):
        raise DeliveryPolicyError(error_message('policy.error.thread_policy', audit="development_thread_policy must be reuse or new-per-attempt"))
    native_values: dict[str, Any] = {field: values.get(key) for key, field in _POLICY_VALUE_FIELDS.items()}
    return Policy(
        **native_values,
        new_thread_per_development=thread_policy == "new-per-attempt",
        development_deadline_seconds=_positive_duration(deadlines["development"], "development deadline"),
        review_deadline_seconds=_positive_duration(deadlines["review"], "review deadline"),
        publication_deadline_seconds=_positive_duration(deadlines["publication"], "publication deadline"),
    )


def _positive_integer(value: Any, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise DeliveryPolicyError(error_message('policy.error.positive_integer', audit=f"{name} must be a positive integer", name=name))
    return value


def _nonnegative_integer(value: Any, name: str) -> int:
    if type(value) is not int or value < 0:
        raise DeliveryPolicyError(f"{name} must be a non-negative integer")
    return value


def _positive_duration(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise DeliveryPolicyError(error_message('policy.error.positive_duration', audit=f"{name} must be a positive duration", name=name))
    try:
        if isinstance(value, (int, float)):
            seconds = float(value)
        elif isinstance(value, str):
            match = _DURATION_PATTERN.fullmatch(value)
            if match is None:
                raise DeliveryPolicyError(error_message('policy.error.positive_duration', audit=f"{name} must be a positive duration", name=name))
            seconds = float(match.group(1)) * _DURATION_MULTIPLIERS[
                match.group(2).lower()
            ]
        else:
            raise DeliveryPolicyError(error_message('policy.error.positive_duration', audit=f"{name} must be a positive duration", name=name))
    except OverflowError as error:
        raise DeliveryPolicyError(error_message('policy.error.positive_duration', audit=f"{name} must be a positive duration", name=name)) from error
    if not isinstance(seconds, float):
        raise DeliveryPolicyError(error_message('policy.error.positive_duration', audit=f"{name} must be a positive duration", name=name))
    if not math.isfinite(seconds) or seconds <= 0:
        raise DeliveryPolicyError(error_message('policy.error.positive_duration', audit=f"{name} must be a positive duration", name=name))
    return seconds


def _canonical_number(value: float) -> int | float:
    return int(value) if value.is_integer() else value
