"""Configuration and the native domain share one frozen policy value contract."""
from dataclasses import replace

import pytest

import agent_run.contracts.policy_values as delivery_policy
from agent_run.errors import DeliveryPolicyError
from agent_run.contracts.policy_values import resolve_delivery_policy
from agent_run.model import Policy, Profile
from agent_run.contracts.policy_values import checkpoint_policy


def test_settings_sparse_overrides_freeze_native_policy_and_renew_from_frozen(tmp_path, capsys, monkeypatch) -> None:
    import json

    from agent_run.config.defaults import UserDefaultsStore
    from agent_run.model import Commit, Delivery, DeliveryKind, RunSpec
    from agent_run.core.kernel import Kernel
    from agent_run.config.creation import freeze_creation
    from agent_run.cli.main import main
    from support.core_operations import CoreOperations, native_agent_fixture

    defaults = UserDefaultsStore()
    legacy = defaults.path.with_name("delivery-policy.json")
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_bytes(b'{"ticket_review_rounds":99}')
    old_bytes = legacy.read_bytes()
    assert main(["settings", "configure", "--ticket-review-round", "2",
                 "--development-timeout", "7s", "--integration-resync-limit", "0", "--json"]) == 0
    capsys.readouterr()
    assert main(["settings", "configure", "--review-deadline", "1.5s", "--json"]) == 0
    saved = json.loads(capsys.readouterr().out)
    assert saved["defaults"]["policy"] == {"ticket_review_rounds": 2, "integration_resync_limit": 0,
        "invocation_deadlines": {"development": "7s", "review": "1.5s"}}
    assert saved["source"] == "user-defaults" and "legacy_ignored" not in saved

    base = Commit("base", "tree")
    delivery = Delivery("ticket", DeliveryKind.TICKET, 2, "ticket-v1", base,
                        "refs/heads/source", "refs/heads/target")
    spec = freeze_creation(RunSpec("run", "example/project", 1, "parent-v1", "graph-v1", (delivery,)),
                           defaults, policy_overrides={"ticket_review_rounds": 3})
    assert spec.policy.ticket_rounds == 3 and spec.policy.resync_limit == 0
    assert spec.policy.development_deadline_seconds == 7 and spec.policy.review_deadline_seconds == 1.5
    kernel, ports = native_agent_fixture(tmp_path / "kernel.sqlite3", spec)
    assert main(["settings", "configure", "--ticket-review-rounds", "9",
                 "--development-deadline", "2h", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["policy"]["ticket_review_rounds"] == 9
    reopened = Kernel(kernel.storage.path, run_id=kernel.storage.run_id)
    execution = reopened.claim_executor("settings-owner")
    call = CoreOperations(reopened, execution, lambda binding: ports).once().call
    assert call is not None and call.deadline_seconds == 7
    assert reopened.status(delivery.key)["budgets"]["requirements"]["review_limit"] == 3

    def no_future_defaults():
        raise AssertionError("A later window must use the original frozen Policy")

    monkeypatch.setattr(delivery_policy, "default_delivery_policy", no_future_defaults)
    assert checkpoint_policy(spec.policy, {"ticket_review_rounds": 4}) == replace(spec.policy, ticket_rounds=4)
    assert checkpoint_policy(spec.policy, {}) == spec.policy
    assert legacy.read_bytes() == old_bytes


def test_policy_resolution_returns_the_domain_values_without_a_second_conversion() -> None:
    policy = resolve_delivery_policy(command_overrides={
        "ticket_review_rounds": 2,
        "parent_only_paired_rounds": 5,
        "run_repair_rounds": 4,
        "ticket_development_workers": 1,
        "integration_resync_limit": 0,
        "ticket_integration_development_limit": 6,
        "ticket_ci_development_limit": 7,
        "parent_ci_development_limit": 8,
        "ticket_ci_review": True,
        "development_thread_policy": "new-per-attempt",
        "invocation_deadlines": {"development": "7s", "review": "1.5s", "publication": "2m"},
    })
    assert isinstance(policy, Policy)
    assert (policy.ticket_rounds, policy.parent_rounds, policy.run_repairs) == (2, 5, 4)
    assert (policy.concurrency, policy.resync_limit, policy.integration_developments) == (1, 0, 6)
    assert (policy.ticket_ci_developments, policy.parent_ci_developments) == (7, 8)
    assert policy.ticket_ci_review is True and policy.new_thread_per_development is True
    assert policy.invocation_deadlines == {"development": 7.0, "review": 1.5, "publication": 120.0}


def test_frozen_values_and_checkpoint_do_not_inherit_future_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    frozen = Policy(ticket_rounds=2, parent_rounds=5, run_repairs=4, resync_limit=0,
                    integration_developments=6, ticket_ci_developments=7, parent_ci_developments=8,
                    concurrency=1, ticket_ci_review=True, new_thread_per_development=True,
                    development_deadline_seconds=7, review_deadline_seconds=1.5,
                    publication_deadline_seconds=120)
    snapshot = frozen.snapshot()
    monkeypatch.setattr(delivery_policy, "default_delivery_policy", lambda: Policy(
        ticket_rounds=19, parent_rounds=29, run_repairs=39, resync_limit=9, concurrency=9,
        development_deadline_seconds=999,
    ))
    assert checkpoint_policy(frozen, {"ticket_ci_development_limit": 4}) == replace(frozen, ticket_ci_developments=4)
    assert frozen.snapshot() == snapshot
    profiles = replace(frozen, development_profile=Profile("fixed-development", "high"),
                       review_profile=Profile("fixed-review", "medium"),
                       publication_profile=Profile("fixed-publication", "high"))
    assert checkpoint_policy(profiles, {"review_deadline": "2s"}) == replace(profiles, review_deadline_seconds=2)
    assert checkpoint_policy(profiles, {}) == profiles

    def future_defaults_are_unavailable() -> Policy:
        raise AssertionError("Frozen policy renewal must not consult future defaults")

    monkeypatch.setattr(delivery_policy, "default_delivery_policy", future_defaults_are_unavailable)
    assert checkpoint_policy(profiles, {"ticket_ci_development_limit": 4}) == replace(profiles, ticket_ci_developments=4)


@pytest.mark.parametrize(("native", "wire", "invalid"), [
    ("ticket_rounds", "ticket_review_rounds", True),
    ("parent_rounds", "parent_only_paired_rounds", 0),
    ("run_repairs", "run_repair_rounds", -1),
    ("concurrency", "ticket_development_workers", 1.5),
    ("resync_limit", "integration_resync_limit", -1),
    ("ticket_ci_review", "ticket_ci_review", 1),
    ("development_deadline_seconds", "development_deadline_seconds", float("inf")),
])
def test_direct_and_configured_values_share_policy_validation(native: str, wire: str, invalid: object) -> None:
    with pytest.raises(DeliveryPolicyError):
        Policy(**{native: invalid})  # type: ignore[arg-type]
    with pytest.raises(DeliveryPolicyError):
        resolve_delivery_policy(command_overrides={wire: invalid})


def test_checkpoint_applies_only_explicit_overrides_without_exporting_frozen_values(monkeypatch) -> None:
    frozen = Policy(ticket_rounds=2, parent_rounds=5, run_repairs=4, resync_limit=0,
                    ticket_ci_review=True, new_thread_per_development=True,
                    development_profile=Profile("frozen-development", "high"),
                    review_profile=Profile("frozen-review", "medium"),
                    publication_profile=Profile("frozen-publication", "high"))

    def no_snapshot(_policy):
        raise AssertionError("Applying an override must not export the frozen Policy")

    monkeypatch.setattr(Policy, "snapshot", no_snapshot)
    overrides = {"ticket_review_rounds": 7, "review_deadline": "1.5s"}
    expected = replace(frozen, ticket_rounds=7, review_deadline_seconds=1.5)
    assert checkpoint_policy(frozen, overrides) == expected
    assert resolve_delivery_policy(user_defaults=frozen, command_overrides=overrides) == expected
    assert checkpoint_policy(frozen, {}) == frozen
    assert checkpoint_policy(frozen, {"development_deadline": None}) == frozen


@pytest.mark.parametrize("value", [0, 1, 1.0, "true", None, [], {}])
@pytest.mark.parametrize("field", ["ticket_ci_review", "new_thread_per_development"])
def test_native_policy_flags_require_actual_booleans(field, value) -> None:
    with pytest.raises(DeliveryPolicyError, match="must be boolean"):
        Policy(**{field: value})
