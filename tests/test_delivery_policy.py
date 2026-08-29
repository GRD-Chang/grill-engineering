from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_run.delivery_policy import (
    DeliveryPolicy,
    DeliveryPolicyError,
    DeliveryPolicyStore,
    parse_policy_snapshot,
    resolve_delivery_policy,
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
    assert policy.review_deadline_seconds == 30
    assert policy.development_deadline_seconds == 5 * 60 * 60
    assert ticket_review_budget_policy(policy).development_limit == 2
    assert ticket_review_budget_policy(policy).review_limit == 1


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
        {"invocation_deadlines": {"review": "0s"}},
        {"invocation_deadlines": {"review": "not-a-duration"}},
        {"invocation_deadlines": {"review": 10**400}},
        {"unknown": 1},
    ],
)
def test_invalid_policy_is_rejected(overrides: dict[str, object]) -> None:
    with pytest.raises(DeliveryPolicyError):
        resolve_delivery_policy(command_overrides=overrides)


def test_delivery_policy_store_merges_sparse_user_defaults_atomically(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config" / "agent-run" / "delivery-policy.json"
    store = DeliveryPolicyStore(path)

    first = store.configure(
        {"ticket_review_rounds": 2, "review_deadline": "90m"}
    )
    second = store.configure({"ticket_review_rounds": 1})

    assert first.review_deadline_seconds == 90 * 60
    assert second.ticket_review_rounds == 1
    assert second.review_deadline_seconds == 90 * 60
    assert store.path.stat().st_mode & 0o777 == 0o600
    assert json.loads(store.path.read_text(encoding="utf-8")) == {
        "invocation_deadlines": {"review": "90m"},
        "ticket_review_rounds": 1,
    }


def test_policy_snapshot_is_complete_and_does_not_reapply_defaults() -> None:
    policy = DeliveryPolicy(ticket_review_rounds=4, review_deadline_seconds=1.5)
    snapshot = policy.snapshot()

    assert parse_policy_snapshot(snapshot).snapshot() == snapshot
    with pytest.raises(DeliveryPolicyError):
        parse_policy_snapshot({"ticket_review_rounds": 4})


def test_direct_policy_constructor_rejects_invalid_values() -> None:
    with pytest.raises(DeliveryPolicyError):
        DeliveryPolicy(publication_deadline_seconds=0)
