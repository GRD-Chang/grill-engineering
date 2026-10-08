from __future__ import annotations

"""User-level Delivery Policy resolution and immutable Run snapshots."""

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass

from agent_run.messages import error_message
from typing import Any

from agent_run.review_budget import (
    PARENT_ONLY_POLICY,
    RUN_POLICY,
    TICKET_POLICY,
    ReviewBudgetPolicy,
)


DELIVERY_POLICY_PROTOCOL = 7
_POLICY_KEYS = frozenset(
    {
        "ticket_development_workers",
        "parent_only_paired_rounds",
        "run_repair_rounds",
        "ticket_review_rounds",
        "integration_resync_limit",
        "ticket_integration_development_limit",
        "ci_recovery_rounds",
        "ci_repairs_per_round",
        "development_thread_policy",
        "invocation_deadlines",
    }
)
_DEADLINE_KEYS = frozenset({"development", "review", "publication"})
_DURATION_PATTERN = re.compile(
    r"^\s*([0-9]+(?:\.[0-9]+)?)\s*([smhd]?)\s*$", re.IGNORECASE
)
_DURATION_MULTIPLIERS = {"": 1.0, "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}


class DeliveryPolicyError(ValueError):
    """The Delivery Policy is missing, malformed, or outside its contract."""


@dataclass(frozen=True)
class DeliveryPolicy:
    """Complete policy values captured by one Delivery Run/window."""

    ticket_review_rounds: int = 3
    ticket_development_workers: int = 3
    integration_resync_limit: int = 3
    ticket_integration_development_limit: int = 3
    ci_recovery_rounds: int = 3
    ci_repairs_per_round: int = 3
    parent_only_paired_rounds: int = 10
    run_repair_rounds: int = 3
    development_deadline_seconds: float = 5 * 60 * 60
    review_deadline_seconds: float = 2 * 60 * 60
    publication_deadline_seconds: float = 60 * 60
    development_thread_policy: str = "reuse"

    def __post_init__(self) -> None:
        if self.development_thread_policy not in ("reuse", "new-per-attempt"):
            raise DeliveryPolicyError(error_message('policy.error.thread_policy', audit="development_thread_policy must be reuse or new-per-attempt"))
        object.__setattr__(
            self,
            "ticket_review_rounds",
            _positive_integer(self.ticket_review_rounds, "ticket_review_rounds"),
        )
        object.__setattr__(
            self,
            "ticket_development_workers",
            _positive_integer(self.ticket_development_workers, "ticket_development_workers"),
        )
        object.__setattr__(
            self,
            "integration_resync_limit",
            _nonnegative_integer(self.integration_resync_limit, "integration_resync_limit"),
        )
        for field in ("ci_recovery_rounds", "ci_repairs_per_round", "ticket_integration_development_limit"):
            object.__setattr__(self, field, _positive_integer(getattr(self, field), field))
        object.__setattr__(
            self,
            "parent_only_paired_rounds",
            _positive_integer(
                self.parent_only_paired_rounds, "parent_only_paired_rounds"
            ),
        )
        object.__setattr__(
            self,
            "run_repair_rounds",
            _positive_integer(self.run_repair_rounds, "run_repair_rounds"),
        )
        for field, label in (
            ("development_deadline_seconds", "development deadline"),
            ("review_deadline_seconds", "review deadline"),
            ("publication_deadline_seconds", "publication deadline"),
        ):
            object.__setattr__(
                self,
                field,
                _positive_duration(getattr(self, field), label),
            )

    def snapshot(self) -> dict[str, Any]:
        return {
            "parent_only_paired_rounds": self.parent_only_paired_rounds,
            "run_repair_rounds": self.run_repair_rounds,
            "ticket_review_rounds": self.ticket_review_rounds,
            "ticket_development_workers": self.ticket_development_workers,
            "integration_resync_limit": self.integration_resync_limit,
            "ticket_integration_development_limit": self.ticket_integration_development_limit,
            "ci_recovery_rounds": self.ci_recovery_rounds,
            "ci_repairs_per_round": self.ci_repairs_per_round,
            "development_thread_policy": self.development_thread_policy,
            "invocation_deadlines": {
                "development": _canonical_number(self.development_deadline_seconds),
                "review": _canonical_number(self.review_deadline_seconds),
                "publication": _canonical_number(self.publication_deadline_seconds),
            },
        }

    @property
    def invocation_deadlines(self) -> dict[str, float]:
        return {
            "development": self.development_deadline_seconds,
            "review": self.review_deadline_seconds,
            "publication": self.publication_deadline_seconds,
        }


def default_delivery_policy() -> DeliveryPolicy:
    return DeliveryPolicy()


def resolve_delivery_policy(
    *,
    user_defaults: Mapping[str, Any] | DeliveryPolicy | None = None,
    command_overrides: Mapping[str, Any] | None = None,
) -> DeliveryPolicy:
    """Resolve builtin values, user defaults, then one-shot overrides."""

    values: dict[str, Any] = default_delivery_policy().snapshot()
    values["invocation_deadlines"] = dict(values["invocation_deadlines"])
    for source, raw, ignore_none in (
        ("user defaults", user_defaults, False),
        ("command overrides", command_overrides, True),
    ):
        if raw is None:
            continue
        normalized = (
            raw.snapshot()
            if isinstance(raw, DeliveryPolicy)
            else normalize_policy_overrides(raw, ignore_none=ignore_none)
        )
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
        "ci_recovery_rounds": "ci_recovery_rounds",
        "ci_repairs_per_round": "ci_repairs_per_round",
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


def parse_policy_snapshot(value: object) -> DeliveryPolicy:
    """Parse the complete canonical state snapshot without applying defaults."""

    if not isinstance(value, Mapping) or set(value) not in (
        _POLICY_KEYS, _POLICY_KEYS - {"development_thread_policy"}
    ):
        raise DeliveryPolicyError("Policy Snapshot has an invalid field set")
    deadlines = value.get("invocation_deadlines")
    if not isinstance(deadlines, Mapping) or set(deadlines) != _DEADLINE_KEYS:
        raise DeliveryPolicyError("Policy Snapshot deadlines have an invalid field set")
    return _policy_from_values(
        {
            "parent_only_paired_rounds": value.get("parent_only_paired_rounds"),
            "run_repair_rounds": value.get("run_repair_rounds"),
            "ticket_review_rounds": value.get("ticket_review_rounds"),
            "ticket_development_workers": value.get("ticket_development_workers"),
            "integration_resync_limit": value.get("integration_resync_limit"),
            "ticket_integration_development_limit": value.get("ticket_integration_development_limit"),
            "ci_recovery_rounds": value.get("ci_recovery_rounds"),
            "ci_repairs_per_round": value.get("ci_repairs_per_round"),
            "development_thread_policy": value.get("development_thread_policy", "reuse"),
            "invocation_deadlines": dict(deadlines),
        }
    )


def policy_snapshot_for_state(state: Mapping[str, Any]) -> dict[str, Any]:
    """Return a validated snapshot for a newly materialized Change Job."""

    raw = state.get("policy_snapshot")
    if raw is None:
        return default_delivery_policy().snapshot()
    return parse_policy_snapshot(raw).snapshot()


def invocation_deadline_for_state(
    state: Mapping[str, Any], role: str
) -> float:
    """Resolve the frozen role deadline used by one Agent Invocation."""

    role_name = {
        "development": "development",
        "review": "review",
        "reviewer": "review",
        "fresh_acceptance": "review",
        "publication": "publication",
        "final_publication": "publication",
    }.get(role)
    if role_name is None:
        raise DeliveryPolicyError(f"unknown Agent Invocation role: {role}")
    policy = (
        default_delivery_policy()
        if state.get("policy_snapshot") is None
        else parse_policy_snapshot(state.get("policy_snapshot"))
    )
    return policy.invocation_deadlines[role_name]


def ticket_review_budget_policy(policy: DeliveryPolicy) -> ReviewBudgetPolicy:
    """Derive the Ticket D(N+1)/R(N) topology from semantic Review rounds."""

    return ReviewBudgetPolicy(
        development_limit=policy.ticket_review_rounds + 1,
        review_limit=policy.ticket_review_rounds,
        final_ci_fix_limit=1,
        fallback=True,
        budget_scope="ticket",
    )


def parent_only_budget_policy(policy: DeliveryPolicy) -> ReviewBudgetPolicy:
    """Derive the Parent-only Development N/Review N paired topology."""

    return ReviewBudgetPolicy(
        development_limit=policy.parent_only_paired_rounds,
        review_limit=policy.parent_only_paired_rounds,
        final_ci_fix_limit=0,
        fallback=False,
        budget_scope="parent-only",
    )


def parent_integration_budget_policy(policy: DeliveryPolicy) -> ReviewBudgetPolicy:
    """Review-first Parent delivery window, independent of demand development."""

    return ReviewBudgetPolicy(
        development_limit=policy.run_repair_rounds,
        review_limit=policy.run_repair_rounds + 1,
        fallback=False,
        budget_scope="parent-only",
    )


def run_repair_budget_policy(policy: DeliveryPolicy) -> ReviewBudgetPolicy:
    """Derive the Run D(N)/R(N+1) review-first topology."""

    return ReviewBudgetPolicy(
        development_limit=policy.run_repair_rounds,
        review_limit=policy.run_repair_rounds + 1,
        final_ci_fix_limit=0,
        fallback=False,
        budget_scope="run",
    )


def ticket_budget_policy_for_job(
    job: Mapping[str, Any], *, state_snapshot: object | None = None
) -> ReviewBudgetPolicy:
    """Resolve a Ticket Job policy, retaining a small legacy unit-test seam."""

    raw = job.get("policy_snapshot")
    if raw is None:
        raw = state_snapshot
    if raw is None:
        return TICKET_POLICY
    try:
        return ticket_review_budget_policy(parse_policy_snapshot(raw))
    except DeliveryPolicyError as error:
        raise ValueError(f"invalid Ticket Policy Snapshot: {error}") from error


def parent_only_budget_policy_for_job(
    job: Mapping[str, Any], *, state_snapshot: object | None = None
) -> ReviewBudgetPolicy:
    """Resolve the frozen Parent-only paired-round policy for one Job."""

    raw = job.get("policy_snapshot")
    if raw is None:
        raw = state_snapshot
    if raw is None:
        # Keep the direct engine seam usable for legacy unit callers; the
        # persisted State contract rejects a missing Run Policy Snapshot.
        return (
            ReviewBudgetPolicy(3, 4, budget_scope="parent-only")
            if job.get("parent_integration_stage") is True
            else PARENT_ONLY_POLICY
        )
    try:
        policy = parse_policy_snapshot(raw)
        return (
            parent_integration_budget_policy(policy)
            if job.get("parent_integration_stage") is True
            else parent_only_budget_policy(policy)
        )
    except DeliveryPolicyError as error:
        raise ValueError(f"invalid Parent-only Policy Snapshot: {error}") from error


def run_repair_budget_policy_for_job(
    job: Mapping[str, Any], *, state_snapshot: object | None = None
) -> ReviewBudgetPolicy:
    """Resolve the frozen Run policy shared by Run Acceptance and Run Repair."""

    raw = job.get("policy_snapshot")
    if raw is None:
        raw = state_snapshot
    if raw is None:
        # Keep the direct engine seam usable for legacy unit callers; the
        # persisted State contract rejects a missing Policy Snapshot.
        return RUN_POLICY
    try:
        return run_repair_budget_policy(parse_policy_snapshot(raw))
    except DeliveryPolicyError as error:
        raise ValueError(f"invalid Run Policy Snapshot: {error}") from error


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


def _policy_from_values(values: Mapping[str, Any]) -> DeliveryPolicy:
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
    return DeliveryPolicy(
        development_thread_policy=values.get("development_thread_policy", "reuse"),
        parent_only_paired_rounds=_positive_integer(
            values.get("parent_only_paired_rounds"), "parent_only_paired_rounds"
        ),
        run_repair_rounds=_positive_integer(
            values.get("run_repair_rounds"), "run_repair_rounds"
        ),
        ticket_review_rounds=_positive_integer(
            values.get("ticket_review_rounds"), "ticket_review_rounds"
        ),
        ticket_development_workers=_positive_integer(
            values.get("ticket_development_workers"), "ticket_development_workers"
        ),
        ticket_integration_development_limit=_positive_integer(
            values.get("ticket_integration_development_limit"), "ticket_integration_development_limit"
        ),
        integration_resync_limit=_nonnegative_integer(
            values.get("integration_resync_limit"), "integration_resync_limit"
        ),
        ci_recovery_rounds=_positive_integer(
            values.get("ci_recovery_rounds"), "ci_recovery_rounds"
        ),
        ci_repairs_per_round=_positive_integer(
            values.get("ci_repairs_per_round"), "ci_repairs_per_round"
        ),
        development_deadline_seconds=_positive_duration(
            deadlines["development"], "development deadline"
        ),
        review_deadline_seconds=_positive_duration(
            deadlines["review"], "review deadline"
        ),
        publication_deadline_seconds=_positive_duration(
            deadlines["publication"], "publication deadline"
        ),
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
