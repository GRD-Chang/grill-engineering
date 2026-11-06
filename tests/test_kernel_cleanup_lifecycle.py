"""Final ownership and physical cleanup races after committed delivery."""
from __future__ import annotations
from support.effect_boundary import _local_marker

from agent_run.contracts.write_authority import WriteAuthority

from pathlib import Path
import json

import pytest

from agent_run.model import DeliveryKind, Effect, EffectResult
from agent_run.core.kernel import Kernel
from support.offline import OfflinePort
from support.core_operations import CoreOperations
from support.cleanup_scenarios import _native_scenario, _local_head


CLEANUP_OPERATIONS = ("cleanup_worktree", "cleanup_local_ref", "cleanup_remote_ref")


def test_completed_final_retires_only_its_recorded_resources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = _native_scenario(tmp_path / "scenario", DeliveryKind.FINAL_RUN)
    main = "refs/heads/main"
    main_before = scenario.port.git.commit(main)
    sibling = "refs/heads/other-active-ticket"
    scenario.port.git.run("update-ref", sibling, main_before.sha)
    checkout = Path(scenario.delivery.checkout)
    actual_execute = scenario.port.execute
    retired: list[Effect] = []

    def execute(effect: Effect, authorize: WriteAuthority) -> EffectResult | None:
        if effect.operation in CLEANUP_OPERATIONS:
            # Cleanup cannot be used as a substitute for durable business closeout.
            fresh = Kernel(scenario.port.root / 'kernel.sqlite3', run_id=scenario.spec.run_id)
            assert fresh.status(scenario.delivery.key)["phase"] == "complete"
            retired.append(effect)
            if effect.operation == "cleanup_worktree":
                assert Path(effect.payload["checkout"]) == checkout
                assert effect.payload["expected_head"] == scenario.port.git.run(
                    "rev-parse", "HEAD", cwd=checkout,
                ).stdout.strip()
            else:
                pr = scenario.port.pull_request_by_ref(scenario.delivery.source_ref, scenario.delivery.target_ref)
                assert pr is not None and pr.merged is not None
                assert effect.payload["branch_ref"] == scenario.delivery.source_ref
                assert effect.payload["expected_head"] == pr.head.sha
                assert pr.head.sha != pr.merged.sha
        return actual_execute(effect, authorize)

    monkeypatch.setattr(scenario.port, "execute", execute)
    assert scenario.run(approve=True).kind == "complete"
    assert [effect.operation for effect in retired] == ["cleanup_worktree", "cleanup_remote_ref"]
    assert not checkout.exists()
    registry = scenario.port.git.run("worktree", "list", "--porcelain", "-z").stdout.split("\0")
    assert f"worktree {checkout}" not in registry
    # The offline remote mirror exists locally, but Final never created an
    # owned local marker. Its publication cannot authorize deleting this ref.
    assert _local_head(scenario, scenario.delivery.source_ref) is not None
    with scenario.kernel.storage.transaction(write=False) as tx:
        omitted = tx.one("SELECT material_id FROM history WHERE subject=? AND kind='resource_not_owned' AND identity='cleanup_local_ref'", (scenario.delivery.key,))
        assert json.loads(tx.get(omitted[0], scenario.delivery.key))["create_ref"] is False
    assert _local_head(scenario, sibling) == main_before.sha
    pr = scenario.port.pull_request_by_ref(scenario.delivery.source_ref, scenario.delivery.target_ref)
    assert pr is not None and pr.merged is not None
    assert scenario.kernel.status(scenario.delivery.key)["issue_link"]["state"] == "linked"
    assert scenario.port.remote_commit(scenario.delivery.target_ref) == pr.merged
    for effect in retired:
        assert scenario.port.reconcile(effect).disposition == "target"
    calls_before = list(scenario.role_calls)
    writes_before = scenario.port.writes()
    scenario.kernel = Kernel(scenario.port.root / 'kernel.sqlite3', run_id=scenario.spec.run_id)
    assert scenario.run(approve=True).kind == "complete"
    assert scenario.role_calls == calls_before
    assert scenario.port.writes() == writes_before


def test_foreign_remote_head_keeps_completed_delivery_and_original_cleanup_intent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """02 remote-delete dispatch race: completed business, foreign remote preserved."""
    scenario = _native_scenario(tmp_path / "scenario", DeliveryKind.TICKET)
    actual_execute = scenario.port.execute
    attempts: list[Effect] = []
    foreign_heads: list[str] = []

    def execute(effect: Effect, authorize: WriteAuthority) -> EffectResult | None:
        if effect.operation == "cleanup_remote_ref":
            assert authorize is not None
            authorize.prepare()()  # Preserve the original race after durable dispatch.
            assert not attempts
            attempts.append(effect)
            assert scenario.kernel.status(scenario.delivery.key)["phase"] == "complete"
            # Earlier stages have removed the local resource. This is a separate
            # SQLite remote fact; advancing it must not create/adopt a local ref.
            assert _local_head(scenario, effect.source_ref) is None
            assert scenario.port.remote_refs()[effect.source_ref] == effect.payload["expected_head"]
            later = scenario.port.external_remote_append(
                effect.source_ref, {"foreign.txt": "another actor's retained work\n"}, "cleanup-race",
            )
            foreign_heads.append(later.sha)
            assert later.sha != effect.payload["expected_head"]
            assert _local_head(scenario, effect.source_ref) is None
        return actual_execute(effect, authorize)

    monkeypatch.setattr(scenario.port, "execute", execute)
    # Observe resource retirement, independently of the business-complete
    # return. Keep run()'s original limit of200 operations for this fresh scenario.
    boundary = scenario.cleanup(max_steps=200)
    assert boundary.kind == "waiting" and boundary.reason == "cleanup_pending", boundary
    assert len(attempts) == len(foreign_heads) == 1
    original = attempts[0]
    durable = Kernel(scenario.port.root / 'kernel.sqlite3', run_id=scenario.spec.run_id).status(scenario.delivery.key)
    assert durable["phase"] == "complete"
    cleanup = durable["resource_cleanup"]
    assert cleanup["state"] == "pending" and cleanup["stage"] == 2
    assert cleanup["outcome"] == "third"
    assert cleanup["source_sha"] == original.payload["expected_head"]
    assert cleanup["effect_id"] == original.effect_id
    with scenario.kernel.storage.transaction(write=False) as tx:
        assert tuple(tx.one("SELECT status,attempts FROM effects WHERE id=?", (original.effect_id,))) == ("sent", 1)
    pr = scenario.port.pull_request_by_ref(scenario.delivery.source_ref, scenario.delivery.target_ref)
    assert pr is not None and pr.merged is not None
    assert scenario.port.remote_commit(scenario.delivery.target_ref) == pr.merged
    assert scenario.port.git.run("show", f"{foreign_heads[0]}:foreign.txt").stdout == "another actor's retained work\n"
    calls_before, writes_before = list(scenario.role_calls), scenario.port.writes()
    scenario.kernel = Kernel(scenario.port.root / 'kernel.sqlite3', run_id=scenario.spec.run_id)
    scenario.port = OfflinePort(scenario.port.root)
    assert scenario.port.remote_refs()[original.source_ref] == foreign_heads[0]
    # Re-entry may admit Final, but must neither replay the original cleanup
    # nor run another Agent. Its completed business and pending cleanup persist.
    reentry = CoreOperations(scenario.kernel, scenario.execution, scenario.make_ports).once(agent_capacity=0)
    assert reentry.call is None and reentry.reason == "final_activated"
    assert scenario.kernel.status(scenario.delivery.key)["phase"] == "complete"
    assert scenario.port.remote_refs()[original.source_ref] == foreign_heads[0]
    assert _local_head(scenario, original.source_ref) is None
    assert scenario.kernel.status(scenario.delivery.key)["resource_cleanup"] == cleanup
    assert scenario.kernel.status(scenario.delivery.key)["budgets"] == durable["budgets"]
    assert scenario.role_calls == calls_before
    assert scenario.port.writes() == writes_before


def test_dirty_checkout_keeps_completed_delivery_and_blocks_later_resource_deletions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """02 dirty completed Ticket: preserve checkout, local ref and remote ref."""
    scenario = _native_scenario(tmp_path / "scenario", DeliveryKind.TICKET)
    checkout = Path(scenario.delivery.checkout)
    actual_execute = scenario.port.execute
    attempts: list[Effect] = []

    def execute(effect: Effect, authorize: WriteAuthority) -> EffectResult | None:
        if effect.operation in CLEANUP_OPERATIONS:
            assert authorize is not None
            authorize.prepare()()  # The original completed-delivery cut is already sent.
            attempts.append(effect)
            assert effect.operation == "cleanup_worktree"
            assert scenario.kernel.status(scenario.delivery.key)["phase"] == "complete"
            (checkout / "unsaved.txt").write_text("retain work after delivery\n")
        return actual_execute(effect, authorize)

    monkeypatch.setattr(scenario.port, "execute", execute)
    # This assertion concerns the cleanup boundary, after business closeout.
    boundary = scenario.cleanup(max_steps=200)
    assert boundary.kind == "waiting" and boundary.reason == "cleanup_pending", boundary
    assert len(attempts) == 1
    durable = Kernel(scenario.port.root / 'kernel.sqlite3', run_id=scenario.spec.run_id).status(scenario.delivery.key)
    assert durable["phase"] == "complete"
    cleanup = durable["resource_cleanup"]
    assert cleanup["state"] == "pending" and cleanup["stage"] == 0
    assert cleanup["outcome"] == "third"
    with scenario.kernel.storage.transaction(write=False) as tx:
        assert tuple(tx.one("SELECT status,attempts FROM effects WHERE id=?", (attempts[0].effect_id,))) == ("sent", 1)
        assert not tx.rows("SELECT id FROM effects WHERE subject=? AND operation IN ('cleanup_local_ref','cleanup_remote_ref')", (scenario.delivery.key,))
    pr = scenario.port.pull_request_by_ref(scenario.delivery.source_ref, scenario.delivery.target_ref)
    assert pr is not None and pr.merged is not None
    assert _local_head(scenario, scenario.delivery.source_ref) == _local_marker(scenario)
    assert scenario.port.remote_refs()[scenario.delivery.source_ref] == pr.head.sha
    assert (checkout / "unsaved.txt").read_text() == "retain work after delivery\n"
    assert scenario.port.git.run("rev-parse", "HEAD", cwd=checkout).stdout.strip() == attempts[0].payload["expected_head"]
    assert f"worktree {checkout}" in scenario.port.git.run("worktree", "list", "--porcelain", "-z").stdout.split("\0")
    calls_before, writes_before = list(scenario.role_calls), scenario.port.writes()
    scenario.kernel = Kernel(scenario.port.root / 'kernel.sqlite3', run_id=scenario.spec.run_id)
    scenario.port = OfflinePort(scenario.port.root)
    reentry = CoreOperations(scenario.kernel, scenario.execution, scenario.make_ports).once(agent_capacity=0)
    assert reentry.call is None and reentry.reason == "final_activated"
    assert scenario.kernel.status(scenario.delivery.key)["phase"] == "complete"
    assert scenario.kernel.status(scenario.delivery.key)["resource_cleanup"] == cleanup
    assert scenario.kernel.status(scenario.delivery.key)["budgets"] == durable["budgets"]
    assert (checkout / "unsaved.txt").read_text() == "retain work after delivery\n"
    assert _local_head(scenario, scenario.delivery.source_ref) == _local_marker(scenario)
    assert scenario.port.remote_refs()[scenario.delivery.source_ref] == pr.head.sha
    assert scenario.role_calls == calls_before
    assert scenario.port.writes() == writes_before
