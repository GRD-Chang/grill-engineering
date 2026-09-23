from __future__ import annotations

import pytest

from agent_run.delivery_policy import DeliveryPolicy
from agent_run.integration_resync_budget import (
    ResyncBudgetExhausted,
    complete_integration_window,
    observe_integration_snapshot,
    renew_resync_budget,
    require_resync_budget,
)
from agent_run.state_contract import _require_integration_resync_budgets
from agent_run.state_errors import IncompatibleRunStateError


def test_initial_integration_is_free_and_open_window_coalesces_external_changes() -> None:
    policy = DeliveryPolicy(integration_resync_limit=2).snapshot()
    job: dict = {"policy_snapshot": policy}

    assert observe_integration_snapshot(job, source_sha="s1", target_sha="t1", policy_snapshot=policy) is False
    assert job["integration_resync"]["used"] == 0
    assert observe_integration_snapshot(job, source_sha="s2", target_sha="t1", policy_snapshot=policy) is False
    complete_integration_window(job)
    assert observe_integration_snapshot(job, source_sha="s2", target_sha="t1", policy_snapshot=policy) is False
    assert observe_integration_snapshot(job, source_sha="s2", target_sha="t2", policy_snapshot=policy) is True
    assert observe_integration_snapshot(job, source_sha="s3", target_sha="t2", policy_snapshot=policy) is False
    assert job["integration_resync"]["used"] == 1
    assert job["integration_resync"]["window"] == 2
    require_resync_budget(job["integration_resync"], policy_snapshot=policy)


def test_own_write_readback_does_not_consume_allowance() -> None:
    policy = DeliveryPolicy(integration_resync_limit=1).snapshot()
    job: dict = {"policy_snapshot": policy}
    observe_integration_snapshot(job, source_sha="s1", target_sha="t1", policy_snapshot=policy)
    complete_integration_window(job)
    assert observe_integration_snapshot(job, source_sha="ours", target_sha="t1", policy_snapshot=policy, own_source_write=True) is False
    assert job["integration_resync"]["used"] == 0
    assert observe_integration_snapshot(job, source_sha="ours", target_sha="t2", policy_snapshot=policy) is True
    assert job["integration_resync"]["used"] == 1


def test_exhaustion_preserves_snapshot_and_explicit_renewal_only_refreshes_resync() -> None:
    policy = DeliveryPolicy(integration_resync_limit=1).snapshot()
    job: dict = {"policy_snapshot": policy, "review_budget": {"window": 4}}
    observe_integration_snapshot(job, source_sha="s1", target_sha="t1", policy_snapshot=policy)
    complete_integration_window(job)
    observe_integration_snapshot(job, source_sha="s2", target_sha="t1", policy_snapshot=policy)
    complete_integration_window(job)

    with pytest.raises(ResyncBudgetExhausted, match="integration_resync_exhausted"):
        observe_integration_snapshot(job, source_sha="s3", target_sha="t1", policy_snapshot=policy)
    assert job["integration_resync"]["source_sha"] == "s2"
    assert job["review_budget"] == {"window": 4}
    renew_resync_budget(job, policy_snapshot=policy)
    assert observe_integration_snapshot(job, source_sha="s3", target_sha="t1", policy_snapshot=policy) is True
    assert job["integration_resync"]["generation"] == 2
    assert job["integration_resync"]["used"] == 1
    assert job["review_budget"] == {"window": 4}


def test_zero_resync_limit_still_allows_initial_integration() -> None:
    policy = DeliveryPolicy(integration_resync_limit=0).snapshot()
    job: dict = {"policy_snapshot": policy}
    observe_integration_snapshot(job, source_sha="s1", target_sha="t1", policy_snapshot=policy)
    complete_integration_window(job)
    with pytest.raises(ResyncBudgetExhausted):
        observe_integration_snapshot(job, source_sha="s2", target_sha="t1", policy_snapshot=policy)


def test_persisted_budget_rejects_corruption_without_creating_missing_state() -> None:
    policy = DeliveryPolicy().snapshot()
    state = {"policy_snapshot": policy, "ticket_jobs": {"12": {"policy_snapshot": policy}}}
    _require_integration_resync_budgets(state)
    job = state["ticket_jobs"]["12"]
    observe_integration_snapshot(job, source_sha="s1", target_sha="t1", policy_snapshot=policy)
    job["integration_resync"]["used"] = 4
    with pytest.raises(IncompatibleRunStateError, match="integration_resync"):
        _require_integration_resync_budgets(state)
