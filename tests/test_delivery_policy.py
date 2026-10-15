from __future__ import annotations

from collections import UserDict
from typing import Any

import pytest

from agent_run.delivery_policy import (
    DeliveryPolicy,
    DeliveryPolicyError,
    parent_only_budget_policy,
    parent_only_budget_policy_for_job,
    parse_policy_snapshot,
    resolve_delivery_policy,
    run_repair_budget_policy,
    run_repair_budget_policy_for_job,
    ticket_review_budget_policy,
)


def test_policy_resolution_uses_builtin_user_then_command_precedence() -> None:
    policy = resolve_delivery_policy(
        user_defaults={
            "ticket_review_rounds": 2,
            "review_deadline": "3m",
        },
        command_overrides={
            "ticket_review_rounds": 1,
            "invocation_deadlines": {"review": "30s"},
        },
    )

    assert policy.ticket_review_rounds == 1
    assert policy.integration_resync_limit == 3
    assert policy.parent_ci_development_limit == 3
    assert policy.ticket_ci_development_limit == 3
    assert policy.review_deadline_seconds == 30
    assert policy.development_deadline_seconds == 5 * 60 * 60
    assert policy.publication_deadline_seconds == 60 * 60
    assert ticket_review_budget_policy(policy).development_limit == 2
    assert ticket_review_budget_policy(policy).review_limit == 1


def test_policy_uses_role_specific_builtin_invocation_deadlines() -> None:
    policy = resolve_delivery_policy()

    assert policy.invocation_deadlines == {
        "development": 5 * 60 * 60,
        "review": 2 * 60 * 60,
        "publication": 60 * 60,
    }


def test_parent_only_policy_uses_an_independent_paired_round_limit() -> None:
    policy = resolve_delivery_policy(
        user_defaults={"parent_only_paired_rounds": 4},
        command_overrides={"parent_only_paired_rounds": 2},
    )

    assert policy.parent_only_paired_rounds == 2
    budget_policy = parent_only_budget_policy(policy)
    assert budget_policy.development_limit == 2
    assert budget_policy.review_limit == 2
    assert budget_policy.final_ci_fix_limit == 0
    assert budget_policy.fallback is False


def test_parent_only_job_policy_reads_its_frozen_snapshot() -> None:
    policy = DeliveryPolicy(parent_only_paired_rounds=6)
    job = {"policy_snapshot": policy.snapshot()}

    assert parent_only_budget_policy_for_job(job).review_limit == 6
    assert parent_only_budget_policy_for_job(job).development_limit == 6


def test_run_repair_policy_derives_review_first_rounds() -> None:
    policy = resolve_delivery_policy(command_overrides={"run_repair_rounds": 4})

    budget_policy = run_repair_budget_policy(policy)

    assert policy.run_repair_rounds == 4
    assert budget_policy.development_limit == 4
    assert budget_policy.review_limit == 5
    assert budget_policy.final_ci_fix_limit == 0
    assert budget_policy.fallback is False
    assert run_repair_budget_policy_for_job(
        {"policy_snapshot": policy.snapshot()}
    ) == budget_policy


def test_ci_recovery_limits_are_independent_and_frozen() -> None:
    policy = resolve_delivery_policy(
        user_defaults={"parent_ci_development_limit": 2},
        command_overrides={"ticket_ci_development_limit": 5},
    )

    assert policy.parent_ci_development_limit == 2
    assert policy.ticket_ci_development_limit == 5
    assert parse_policy_snapshot(policy.snapshot()) == policy


def test_ticket_development_worker_limit_is_positive_and_frozen() -> None:
    policy = resolve_delivery_policy(
        user_defaults={"ticket_development_workers": 5},
        command_overrides={"ticket_development_workers": 1},
    )
    assert DeliveryPolicy().ticket_development_workers == 3
    assert policy.ticket_development_workers == 1
    assert parse_policy_snapshot(policy.snapshot()) == policy

    from agent_run.cli import _policy_overrides, build_parser

    parsed = build_parser().parse_args([
        "settings", "configure", "--ticket-development-workers", "2",
    ])
    assert _policy_overrides(parsed) == {"ticket_development_workers": 2}


def test_cli_accepts_both_ci_recovery_policy_overrides() -> None:
    from agent_run.cli import _policy_overrides, build_parser

    parsed = build_parser().parse_args([
        "settings", "configure", "--parent-ci-development-limit", "2",
        "--ticket-ci-development-limit", "4",
    ])

    assert _policy_overrides(parsed) == {
        "parent_ci_development_limit": 2, "ticket_ci_development_limit": 4
    }


def test_invocation_deadlines_are_read_from_the_frozen_snapshot() -> None:
    from agent_run.delivery_policy import invocation_deadline_for_state

    policy = DeliveryPolicy(
        development_deadline_seconds=11,
        review_deadline_seconds=13,
        publication_deadline_seconds=17,
    )
    state = {"policy_snapshot": policy.snapshot()}

    assert invocation_deadline_for_state(state, "development") == 11
    assert invocation_deadline_for_state(state, "fresh_acceptance") == 13
    assert invocation_deadline_for_state(state, "final_publication") == 17


@pytest.mark.parametrize(
    "overrides",
    [
        {"ticket_review_rounds": 0},
        {"ticket_review_rounds": True},
        {"parent_only_paired_rounds": 0},
        {"parent_only_paired_rounds": True},
        {"run_repair_rounds": 0},
        {"run_repair_rounds": True},
        {"integration_resync_limit": -1},
        {"integration_resync_limit": True},
        {"parent_ci_development_limit": 0},
        {"ticket_ci_development_limit": True},
        {"ticket_development_workers": 0},
        {"ticket_development_workers": True},
        {"invocation_deadlines": {"review": "0s"}},
        {"invocation_deadlines": {"review": "not-a-duration"}},
        {"invocation_deadlines": {"review": 10**400}},
        {"unknown": 1},
    ],
)
def test_invalid_policy_is_rejected(overrides: dict[str, object]) -> None:
    with pytest.raises(DeliveryPolicyError):
        resolve_delivery_policy(command_overrides=overrides)


def test_policy_snapshot_is_complete_and_does_not_reapply_defaults() -> None:
    policy = DeliveryPolicy(ticket_review_rounds=4, review_deadline_seconds=1.5)
    snapshot = policy.snapshot()

    assert parse_policy_snapshot(snapshot).snapshot() == snapshot
    missing_resync = dict(snapshot)
    missing_resync.pop("integration_resync_limit")
    with pytest.raises(DeliveryPolicyError):
        parse_policy_snapshot(missing_resync)
    missing_workers = dict(snapshot)
    missing_workers.pop("ticket_development_workers")
    with pytest.raises(DeliveryPolicyError):
        parse_policy_snapshot(missing_workers)
    with pytest.raises(DeliveryPolicyError):
        parse_policy_snapshot({"ticket_review_rounds": 4})


def test_direct_policy_constructor_rejects_invalid_values() -> None:
    with pytest.raises(DeliveryPolicyError):
        DeliveryPolicy(publication_deadline_seconds=0)


def test_development_thread_policy_resolution_and_legacy_snapshot() -> None:
    assert DeliveryPolicy().development_thread_policy == "reuse"
    policy = resolve_delivery_policy(
        user_defaults={"development_thread_policy": "new-per-attempt"},
        command_overrides={"development_thread_policy": "reuse"},
    )
    assert policy.development_thread_policy == "reuse"
    snapshot = policy.snapshot()
    snapshot.pop("development_thread_policy")
    assert parse_policy_snapshot(snapshot).development_thread_policy == "reuse"
    snapshot["development_thread_policy"] = None
    with pytest.raises(DeliveryPolicyError, match="development_thread_policy"):
        parse_policy_snapshot(snapshot)


def test_budget_window_override_preserves_frozen_thread_policy() -> None:
    frozen = DeliveryPolicy(development_thread_policy="new-per-attempt")
    renewed = resolve_delivery_policy(
        user_defaults=frozen, command_overrides={"ticket_review_rounds": 5},
    )
    assert renewed.development_thread_policy == "new-per-attempt"
    assert renewed.ticket_review_rounds == 5


def test_ticket_ci_policy_defaults_overrides_and_frozen_snapshot() -> None:
    policy = resolve_delivery_policy()
    assert policy.ticket_ci_development_limit == 3
    assert policy.ticket_ci_review is False
    configured = resolve_delivery_policy(
        user_defaults={"ticket_ci_development_limit": 7, "ticket_ci_review": True},
        command_overrides={"ticket_ci_review": False},
    )
    assert configured.ticket_ci_review is False
    assert parse_policy_snapshot(configured.snapshot()).ticket_ci_development_limit == 7


@pytest.mark.parametrize("invalid", [0, -1, True, 1.2, "3"])
def test_ticket_ci_development_limit_rejects_nonpositive_or_noninteger(invalid: object) -> None:
    with pytest.raises(DeliveryPolicyError):
        resolve_delivery_policy(command_overrides={"ticket_ci_development_limit": invalid})


@pytest.mark.parametrize("invalid", [None, 0, 1, "true"])
def test_ticket_ci_review_requires_boolean(invalid: object) -> None:
    with pytest.raises(DeliveryPolicyError):
        resolve_delivery_policy(user_defaults={"ticket_ci_review": invalid})


def test_repeated_snapshot_parse_observes_mutations_and_returns_independent_snapshots() -> None:
    snapshot = DeliveryPolicy(ticket_review_rounds=2).snapshot()
    original = parse_policy_snapshot(snapshot)
    assert parse_policy_snapshot(snapshot) == original

    snapshot["ticket_review_rounds"] = 4
    snapshot["invocation_deadlines"]["review"] = "1.5m"
    updated = parse_policy_snapshot(snapshot)
    assert updated.ticket_review_rounds == 4
    assert updated.review_deadline_seconds == 90
    assert original.ticket_review_rounds == 2
    assert original.review_deadline_seconds == 7200

    first, second = updated.snapshot(), updated.snapshot()
    first["ticket_review_rounds"] = 99
    first["invocation_deadlines"]["review"] = 99
    assert second == parse_policy_snapshot(snapshot).snapshot()
    assert second["ticket_review_rounds"] == 4
    assert second["invocation_deadlines"]["review"] == 90


@pytest.mark.parametrize(
    "field, valid, invalid",
    [
        ("ticket_review_rounds", 1, True),
        ("ticket_review_rounds", 1, 1.0),
        ("integration_resync_limit", 0, False),
        ("integration_resync_limit", 0, 0.0),
        ("ticket_ci_review", True, 1),
        ("ticket_ci_review", True, 1.0),
        ("ticket_ci_review", False, 0),
        ("ticket_ci_review", False, 0.0),
    ],
)
def test_snapshot_validation_distinguishes_equal_scalar_types(
    field: str, valid: object, invalid: object,
) -> None:
    snapshot = DeliveryPolicy().snapshot()
    snapshot[field] = valid
    accepted = parse_policy_snapshot(snapshot)
    snapshot[field] = invalid
    for _ in range(2):
        with pytest.raises(DeliveryPolicyError):
            parse_policy_snapshot(snapshot)
    snapshot[field] = valid
    assert parse_policy_snapshot(snapshot) == accepted


def test_snapshot_deadline_validation_rejects_bool_after_equal_numeric_values() -> None:
    snapshot = DeliveryPolicy().snapshot()
    for duration in (1, 1.0, "1s"):
        snapshot["invocation_deadlines"]["review"] = duration
        assert parse_policy_snapshot(snapshot).review_deadline_seconds == 1
    snapshot["invocation_deadlines"]["review"] = True
    with pytest.raises(DeliveryPolicyError):
        parse_policy_snapshot(snapshot)


@pytest.mark.parametrize("level", ["policy", "deadlines"])
@pytest.mark.parametrize("change", ["missing", "extra"])
def test_repeated_snapshot_parse_still_checks_both_field_sets(level: str, change: str) -> None:
    snapshot = DeliveryPolicy().snapshot()
    parse_policy_snapshot(snapshot)
    changed = snapshot if level == "policy" else snapshot["invocation_deadlines"]
    if change == "missing":
        changed.pop("ticket_review_rounds" if level == "policy" else "review")
    else:
        changed["unknown"] = 1
    with pytest.raises(DeliveryPolicyError, match="invalid field set"):
        parse_policy_snapshot(snapshot)


@pytest.mark.parametrize("level", ["policy", "deadlines"])
def test_custom_mapping_access_is_not_reused(level: str) -> None:
    class ChangingMapping(UserDict[str, Any]):
        def __getitem__(self, key: str) -> Any:
            value = super().__getitem__(key)
            if key == ("ticket_review_rounds" if level == "policy" else "review"):
                self.data[key] += 1
            return value

    snapshot = DeliveryPolicy(ticket_review_rounds=2, review_deadline_seconds=10).snapshot()
    if level == "policy":
        raw = ChangingMapping(snapshot)
    else:
        snapshot["invocation_deadlines"] = ChangingMapping(snapshot["invocation_deadlines"])
        raw = snapshot
    first = parse_policy_snapshot(raw)
    second = parse_policy_snapshot(raw)
    assert first.ticket_review_rounds == 2
    assert second.ticket_review_rounds == (3 if level == "policy" else 2)
    assert first.review_deadline_seconds == 10
    assert second.review_deadline_seconds == (11 if level == "deadlines" else 10)


def test_dictionary_subclass_retains_custom_get_behavior() -> None:
    class ChangingSnapshot(dict[str, Any]):
        def get(self, key: str, default: Any = None) -> Any:
            value = super().get(key, default)
            if key == "ticket_review_rounds":
                self[key] += 1
            return value

    snapshot = ChangingSnapshot(DeliveryPolicy(ticket_review_rounds=2).snapshot())
    assert parse_policy_snapshot(snapshot).ticket_review_rounds == 2
    assert parse_policy_snapshot(snapshot).ticket_review_rounds == 3


def test_custom_scalar_conversion_and_unhashable_string_keep_original_validation() -> None:
    class ChangingDuration(float):
        calls = 0

        def __float__(self) -> float:
            self.calls += 1
            return 10.0 + self.calls

    class UnhashableString(str):
        __hash__ = None  # type: ignore[assignment]

    snapshot = DeliveryPolicy().snapshot()
    snapshot["invocation_deadlines"]["review"] = ChangingDuration(10)
    assert parse_policy_snapshot(snapshot).review_deadline_seconds == 11
    assert parse_policy_snapshot(snapshot).review_deadline_seconds == 12
    snapshot["invocation_deadlines"]["review"] = UnhashableString("13s")
    assert parse_policy_snapshot(snapshot).review_deadline_seconds == 13
    assert parse_policy_snapshot(snapshot).review_deadline_seconds == 13


def test_large_scalar_values_keep_original_validation() -> None:
    snapshot = DeliveryPolicy().snapshot()
    snapshot["ticket_review_rounds"] = 10**400
    snapshot["invocation_deadlines"]["review"] = " " * 1000 + "1.5m"
    assert parse_policy_snapshot(snapshot).ticket_review_rounds == 10**400
    assert parse_policy_snapshot(snapshot).review_deadline_seconds == 90
    snapshot["invocation_deadlines"]["review"] = 10**400
    with pytest.raises(DeliveryPolicyError):
        parse_policy_snapshot(snapshot)
