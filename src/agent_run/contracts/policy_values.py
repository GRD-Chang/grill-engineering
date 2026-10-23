from __future__ import annotations


"""User-level Delivery Policy resolution and immutable Run snapshots."""

import re
from collections.abc import Mapping
from dataclasses import replace

from agent_run.errors import error_message
from agent_run.errors import DeliveryPolicyError
from agent_run.contracts.policy import POLICY_VALUE_FIELDS, positive_seconds
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agent_run.model import Policy


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
_DEADLINE_FIELDS = ("development", "review", "publication")
_DURATION_PATTERN = re.compile(
    r"^\s*([0-9]+(?:\.[0-9]+)?)\s*([smhd]?)\s*$", re.IGNORECASE
)
_DURATION_MULTIPLIERS = {"": 1.0, "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}


def default_delivery_policy() -> Policy:
    from agent_run.model import Policy
    return Policy()


def resolve_delivery_policy(
    *,
    user_defaults: Mapping[str, Any] | Policy | None = None,
    command_overrides: Mapping[str, Any] | None = None,
) -> Policy:
    """Resolve sparse defaults, or apply explicit overrides to a frozen owner."""

    from agent_run.model import Policy

    if isinstance(user_defaults, Policy):
        overrides = normalize_policy_overrides(
            {} if command_overrides is None else command_overrides, ignore_none=True,
        )
        return replace(user_defaults, **_policy_changes(overrides))
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
    from agent_run.model import Policy

    new_thread = _new_thread_per_development(values.get("development_thread_policy", "reuse"))
    native_values: dict[str, Any] = {field: values.get(key) for key, field in POLICY_VALUE_FIELDS.items()}
    return Policy(
        **native_values,
        new_thread_per_development=new_thread,
        development_deadline_seconds=_positive_duration(deadlines["development"], "development deadline"),
        review_deadline_seconds=_positive_duration(deadlines["review"], "review deadline"),
        publication_deadline_seconds=_positive_duration(deadlines["publication"], "publication deadline"),
    )


def _policy_changes(values: Mapping[str, Any]) -> dict[str, Any]:
    """Convert only explicit, normalized options for an existing frozen owner."""
    changes = {field: values[key] for key, field in POLICY_VALUE_FIELDS.items() if key in values}
    if "development_thread_policy" in values:
        changes["new_thread_per_development"] = _new_thread_per_development(values["development_thread_policy"])
    deadlines = values.get("invocation_deadlines", {})
    for role in _DEADLINE_FIELDS:
        if role in deadlines:
            changes[role + "_deadline_seconds"] = _positive_duration(deadlines[role], role + " deadline")
    return changes


def _new_thread_per_development(value: object) -> bool:
    if value not in ("reuse", "new-per-attempt"):
        raise DeliveryPolicyError(error_message('policy.error.thread_policy', audit="development_thread_policy must be reuse or new-per-attempt"))
    return value == "new-per-attempt"


def _positive_duration(value: Any, name: str) -> float:
    if isinstance(value, str):
        match = _DURATION_PATTERN.fullmatch(value)
        if match is None:
            raise DeliveryPolicyError(error_message('policy.error.positive_duration', audit=f"{name} must be a positive duration", name=name))
        value = float(match.group(1)) * _DURATION_MULTIPLIERS[match.group(2).lower()]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DeliveryPolicyError(error_message('policy.error.positive_duration', audit=f"{name} must be a positive duration", name=name))
    return positive_seconds(value, name)


from typing import Any, Mapping
from agent_run.model import Policy

def checkpoint_policy(current: Policy, overrides: Mapping[str, Any]) -> Policy:
    """Resolve an explicit new window against this owner's frozen policy."""
    return resolve_delivery_policy(user_defaults=current, command_overrides=overrides)
