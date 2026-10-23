"""Public settings resolution and native frozen Policy values."""
import pytest

from agent_run.errors import DeliveryPolicyError
from agent_run.contracts.policy_values import resolve_delivery_policy
from agent_run.model import Policy
from support.user_defaults import settings


def test_policy_uses_role_specific_builtin_invocation_deadlines() -> None:
    policy = resolve_delivery_policy()

    assert policy.invocation_deadlines == {
        "development": 5 * 60 * 60,
        "review": 2 * 60 * 60,
        "publication": 60 * 60,
    }


def test_ci_recovery_limits_are_independent_and_frozen() -> None:
    policy = resolve_delivery_policy(
        user_defaults={"parent_ci_development_limit": 2},
        command_overrides={"ticket_ci_development_limit": 5},
    )

    assert policy.parent_ci_developments == 2
    assert policy.ticket_ci_developments == 5
    assert resolve_delivery_policy(user_defaults=policy) == policy


def test_ticket_development_worker_limit_is_positive_and_frozen() -> None:
    policy = resolve_delivery_policy(
        user_defaults={"ticket_development_workers": 5},
        command_overrides={"ticket_development_workers": 1},
    )
    assert Policy().concurrency == 3
    assert policy.concurrency == 1
    assert resolve_delivery_policy(user_defaults=policy) == policy

    code, configured = settings("configure", "--ticket-development-workers", "2")
    assert code == 0
    assert configured["policy"]["ticket_development_workers"] == 2


def test_cli_accepts_both_ci_recovery_policy_overrides() -> None:
    code, configured = settings("configure", "--parent-ci-development-limit", "2",
                                "--ticket-ci-development-limit", "4")
    assert code == 0
    assert configured["policy"]["parent_ci_development_limit"] == 2
    assert configured["policy"]["ticket_ci_development_limit"] == 4


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


def test_direct_policy_constructor_rejects_invalid_values() -> None:
    with pytest.raises(DeliveryPolicyError):
        Policy(publication_deadline_seconds=0)


def test_development_thread_policy_resolution() -> None:
    assert Policy().snapshot()["development_thread_policy"] == "reuse"
    policy = resolve_delivery_policy(
        user_defaults={"development_thread_policy": "new-per-attempt"},
        command_overrides={"development_thread_policy": "reuse"},
    )
    assert policy.snapshot()["development_thread_policy"] == "reuse"


def test_budget_window_override_preserves_frozen_thread_policy() -> None:
    frozen = Policy(new_thread_per_development=True)
    renewed = resolve_delivery_policy(
        user_defaults=frozen, command_overrides={"ticket_review_rounds": 5},
    )
    assert renewed.snapshot()["development_thread_policy"] == "new-per-attempt"
    assert renewed.ticket_rounds == 5


def test_ticket_ci_policy_defaults_overrides_and_frozen_snapshot() -> None:
    policy = resolve_delivery_policy()
    assert policy.ticket_ci_developments == 3
    assert policy.ticket_ci_review is False
    configured = resolve_delivery_policy(
        user_defaults={"ticket_ci_development_limit": 7, "ticket_ci_review": True},
        command_overrides={"ticket_ci_review": False},
    )
    assert configured.ticket_ci_review is False
    assert configured.ticket_ci_developments == 7


@pytest.mark.parametrize("invalid", [0, -1, True, 1.2, "3"])
def test_ticket_ci_development_limit_rejects_nonpositive_or_noninteger(invalid: object) -> None:
    with pytest.raises(DeliveryPolicyError):
        resolve_delivery_policy(command_overrides={"ticket_ci_development_limit": invalid})


@pytest.mark.parametrize("invalid", [None, 0, 1, "true"])
def test_ticket_ci_review_requires_boolean(invalid: object) -> None:
    with pytest.raises(DeliveryPolicyError):
        resolve_delivery_policy(user_defaults={"ticket_ci_review": invalid})


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
def test_configured_policy_distinguishes_equal_scalar_types(
    field: str, valid: object, invalid: object,
) -> None:
    snapshot = Policy().snapshot()
    snapshot[field] = valid
    accepted = resolve_delivery_policy(user_defaults=snapshot)
    snapshot[field] = invalid
    for _ in range(2):
        with pytest.raises(DeliveryPolicyError):
            resolve_delivery_policy(user_defaults=snapshot)
    snapshot[field] = valid
    assert resolve_delivery_policy(user_defaults=snapshot) == accepted


def test_configured_deadline_rejects_bool_after_equal_numeric_values() -> None:
    snapshot = Policy().snapshot()
    for duration in (1, 1.0, "1s"):
        snapshot["invocation_deadlines"]["review"] = duration
        assert resolve_delivery_policy(user_defaults=snapshot).review_deadline_seconds == 1
    snapshot["invocation_deadlines"]["review"] = True
    with pytest.raises(DeliveryPolicyError):
        resolve_delivery_policy(user_defaults=snapshot)


def test_configured_duration_conversion_and_unhashable_string_keep_validation() -> None:
    class ChangingDuration(float):
        calls = 0

        def __float__(self) -> float:
            self.calls += 1
            return 10.0 + self.calls

    class UnhashableString(str):
        __hash__ = None  # type: ignore[assignment]

    snapshot = Policy().snapshot()
    snapshot["invocation_deadlines"]["review"] = ChangingDuration(10)
    assert resolve_delivery_policy(user_defaults=snapshot).review_deadline_seconds == 11
    assert resolve_delivery_policy(user_defaults=snapshot).review_deadline_seconds == 12
    snapshot["invocation_deadlines"]["review"] = UnhashableString("13s")
    assert resolve_delivery_policy(user_defaults=snapshot).review_deadline_seconds == 13
    assert resolve_delivery_policy(user_defaults=snapshot).review_deadline_seconds == 13


def test_large_configured_scalar_values_keep_validation() -> None:
    snapshot = Policy().snapshot()
    snapshot["ticket_review_rounds"] = 10**400
    snapshot["invocation_deadlines"]["review"] = " " * 1000 + "1.5m"
    assert resolve_delivery_policy(user_defaults=snapshot).ticket_rounds == 10**400
    assert resolve_delivery_policy(user_defaults=snapshot).review_deadline_seconds == 90
    snapshot["invocation_deadlines"]["review"] = 10**400
    with pytest.raises(DeliveryPolicyError):
        resolve_delivery_policy(user_defaults=snapshot)
