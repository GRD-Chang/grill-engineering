"""Lifecycle resource observations grounded in the unchanged 02 cleanup callers.

Sources: delivery_cleanup.complete_ticket/complete_parent/complete_final_run;
test_cleanup_head_protection.test_every_completed_cleanup_uses_source_head_instead_of_merge_result;
test_ticket_closeout_does_not_attempt_another_ticket_cleanup. The independent
75c neutral observation additionally records absent Ticket local/published refs
and D/R/P checkouts. These tests are candidate regression probes, not a claim
that three new Scenario runs are an old/new same-input differential execution.
"""
from __future__ import annotations

from pathlib import Path
import json
from dataclasses import replace
from typing import Callable

import pytest

from agent_run_kernel import DeliveryKind, Effect, EffectResult, Kernel
from agent_run_kernel.offline import GraphScenario, OfflinePort, Scenario
from test_kernel_requirements import independent_source
from support.core_operations import CoreOperations, final_scenario


CLEANUP_OPERATIONS = ("cleanup_worktree", "cleanup_local_ref", "cleanup_remote_ref")


def _native_scenario(root: Path, kind: DeliveryKind) -> GraphScenario:
    if kind == DeliveryKind.FINAL_RUN:
        return final_scenario(root)
    source = independent_source()
    if kind == DeliveryKind.PARENT_ONLY:
        source = replace(source, parent=replace(source.parent, sub_issue_numbers=()), issues=())
    else:
        # Isolate one delivery's cleanup. A global runner may otherwise start
        # the independent sibling when the completed subject's cleanup blocks.
        first = source.issues[0]
        source = replace(source, parent=replace(source.parent, sub_issue_numbers=(first.number,)), issues=(first,))
    return GraphScenario(root, source)


def _local_head(scenario: Scenario, ref: str) -> str | None:
    output = scenario.port.git.run("for-each-ref", "--format=%(refname)%09%(objectname)", ref).stdout
    return next((line.partition("\t")[2] for line in output.splitlines()
                 if line.partition("\t")[0] == ref), None)


def _local_marker(scenario: Scenario) -> str:
    with scenario.kernel.storage.transaction(write=False) as tx:
        return str(tx.one("SELECT e.before_sha FROM effects e JOIN subjects s ON s.id=e.subject AND s.generation=e.generation WHERE e.subject=? AND e.operation='workspace' AND e.status='confirmed'",
                          (scenario.delivery.key,))[0])


@pytest.mark.parametrize("kind", list(DeliveryKind))
def test_completed_delivery_retires_only_its_recorded_resources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: DeliveryKind,
) -> None:
    scenario = _native_scenario(tmp_path / "scenario", kind)
    main = "refs/heads/main"
    main_before = scenario.port.git.commit(main)
    if kind == DeliveryKind.PARENT_ONLY:
        assert scenario.port.git.run("for-each-ref", "--format=%(refname)").stdout.splitlines() == [main]
        assert scenario.port.remote_refs() == {main: main_before.sha}
    sibling = "refs/heads/other-active-ticket"
    scenario.port.git.run("update-ref", sibling, main_before.sha)
    checkout = Path(scenario.delivery.checkout)
    actual_execute = scenario.port.execute
    retired: list[Effect] = []

    def execute(effect: Effect, authorize: Callable[[], None] | None = None) -> EffectResult | None:
        if effect.operation in CLEANUP_OPERATIONS:
            # Cleanup cannot be used as a substitute for durable business closeout.
            fresh = Kernel(scenario.port.root / "kernel.sqlite3")
            assert fresh.status(scenario.delivery.key)["phase"] == "complete"
            retired.append(effect)
            if effect.operation == "cleanup_worktree":
                assert Path(effect.payload["checkout"]) == checkout
                assert effect.payload["expected_head"] == scenario.port.git.run(
                    "rev-parse", "HEAD", cwd=checkout,
                ).stdout.strip()
            else:
                pr = scenario.port.pull_request(scenario.delivery.key)
                assert pr is not None and pr.merged is not None
                assert effect.payload["branch_ref"] == scenario.delivery.source_ref
                assert effect.payload["expected_head"] == (_local_marker(scenario) if effect.operation == "cleanup_local_ref" else pr.head.sha)
                if kind != DeliveryKind.TICKET:
                    assert pr.head.sha != pr.merged.sha
        return actual_execute(effect, authorize)

    monkeypatch.setattr(scenario.port, "execute", execute)
    assert scenario.run(approve=True).kind == "complete"
    expected_operations = [op for op in CLEANUP_OPERATIONS if kind != DeliveryKind.FINAL_RUN or op != "cleanup_local_ref"]
    assert [effect.operation for effect in retired] == expected_operations
    assert not checkout.exists()
    registry = scenario.port.git.run("worktree", "list", "--porcelain", "-z").stdout.split("\0")
    assert f"worktree {checkout}" not in registry
    if kind == DeliveryKind.FINAL_RUN:
        # The offline remote mirror exists locally, but Final never created an
        # owned local marker. Its publication cannot authorize deleting this ref.
        assert _local_head(scenario, scenario.delivery.source_ref) is not None
        with scenario.kernel.storage.transaction(write=False) as tx:
            omitted = tx.one("SELECT material_id FROM history WHERE subject=? AND kind='resource_not_owned' AND identity='cleanup_local_ref'", (scenario.delivery.key,))
            assert json.loads(tx.get(omitted[0], scenario.delivery.key))["create_ref"] is False
    else:
        assert _local_head(scenario, scenario.delivery.source_ref) is None
    assert _local_head(scenario, sibling) == main_before.sha
    if kind == DeliveryKind.PARENT_ONLY:
        assert scenario.port.git.run("for-each-ref", "--format=%(refname)").stdout.splitlines() == [main, sibling]
        assert set(scenario.port.remote_refs()) == {main}
    pr = scenario.port.pull_request(scenario.delivery.key)
    assert pr is not None and pr.merged is not None
    assert scenario.kernel.status(scenario.delivery.key)["issue_link"]["state"] == "linked"
    assert scenario.port.git.commit(scenario.delivery.target_ref) == pr.merged
    if kind == DeliveryKind.TICKET:
        assert scenario.port.git.commit(main) == main_before
    for effect in retired:
        assert scenario.port.reconcile(effect).disposition == "target"
    calls_before = list(scenario.role_calls)
    writes_before = scenario.port.writes()
    scenario.kernel = Kernel(scenario.port.root / "kernel.sqlite3")
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

    def execute(effect: Effect, authorize: Callable[[], None] | None = None) -> EffectResult | None:
        if effect.operation == "cleanup_remote_ref":
            assert authorize is not None
            authorize()  # Preserve the original race after durable dispatch.
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
    durable = Kernel(scenario.port.root / "kernel.sqlite3").status(scenario.delivery.key)
    assert durable["phase"] == "complete"
    cleanup = durable["resource_cleanup"]
    assert cleanup["state"] == "pending" and cleanup["stage"] == 2
    assert cleanup["outcome"] == "third"
    assert cleanup["source_sha"] == original.payload["expected_head"]
    assert cleanup["effect_id"] == original.effect_id
    with scenario.kernel.storage.transaction(write=False) as tx:
        assert tuple(tx.one("SELECT status,attempts FROM effects WHERE id=?", (original.effect_id,))) == ("sent", 1)
    pr = scenario.port.pull_request(scenario.delivery.key)
    assert pr is not None and pr.merged is not None
    assert scenario.port.git.commit(scenario.delivery.target_ref) == pr.merged
    assert scenario.port.git.run("show", f"{foreign_heads[0]}:foreign.txt").stdout == "another actor's retained work\n"
    calls_before, writes_before = list(scenario.role_calls), scenario.port.writes()
    scenario.kernel = Kernel(scenario.port.root / "kernel.sqlite3")
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

    def execute(effect: Effect, authorize: Callable[[], None] | None = None) -> EffectResult | None:
        if effect.operation in CLEANUP_OPERATIONS:
            assert authorize is not None
            authorize()  # The original completed-delivery cut is already sent.
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
    durable = Kernel(scenario.port.root / "kernel.sqlite3").status(scenario.delivery.key)
    assert durable["phase"] == "complete"
    cleanup = durable["resource_cleanup"]
    assert cleanup["state"] == "pending" and cleanup["stage"] == 0
    assert cleanup["outcome"] == "third"
    with scenario.kernel.storage.transaction(write=False) as tx:
        assert tuple(tx.one("SELECT status,attempts FROM effects WHERE id=?", (attempts[0].effect_id,))) == ("sent", 1)
        assert not tx.rows("SELECT id FROM effects WHERE subject=? AND operation IN ('cleanup_local_ref','cleanup_remote_ref')", (scenario.delivery.key,))
    pr = scenario.port.pull_request(scenario.delivery.key)
    assert pr is not None and pr.merged is not None
    assert _local_head(scenario, scenario.delivery.source_ref) == _local_marker(scenario)
    assert scenario.port.remote_refs()[scenario.delivery.source_ref] == pr.head.sha
    assert (checkout / "unsaved.txt").read_text() == "retain work after delivery\n"
    assert scenario.port.git.run("rev-parse", "HEAD", cwd=checkout).stdout.strip() == attempts[0].payload["expected_head"]
    assert f"worktree {checkout}" in scenario.port.git.run("worktree", "list", "--porcelain", "-z").stdout.split("\0")
    calls_before, writes_before = list(scenario.role_calls), scenario.port.writes()
    scenario.kernel = Kernel(scenario.port.root / "kernel.sqlite3")
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


def test_optional_issue_link_lost_ack_is_not_replayed_or_promoted_by_readback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """75c old actual: external linked once, durable indeterminate, replay zero."""
    scenario = Scenario(tmp_path / "scenario", DeliveryKind.TICKET)
    scenario.port.lose_response.add("issue_link")
    now = [1000.25]
    scenario.kernel.clock = lambda: now[0]
    actual_execute = scenario.port.execute
    attempts: list[Effect] = []

    def execute(effect: Effect, authorize: Callable[[], None] | None = None) -> EffectResult | None:
        if effect.operation == "issue_link":
            attempts.append(effect)
            assert effect.payload["issue"] == scenario.delivery.issue
            assert effect.payload["ref"] == scenario.delivery.source_ref
            original_authorize = authorize

            def dispatched() -> None:
                assert original_authorize is not None
                original_authorize()
                before = Kernel(scenario.port.root / "kernel.sqlite3").status(scenario.delivery.key)
                assert before["issue_link"]["state"] == "indeterminate"

            authorize = dispatched
        return actual_execute(effect, authorize)

    monkeypatch.setattr(scenario.port, "execute", execute)
    for _ in range(80):
        interrupted = scenario.step()
        if attempts:
            break
    else:
        raise AssertionError("The optional link write was never reached")
    # Like RemoteEffects._link_issue, a lost optional acknowledgement is an
    # indeterminate write result, not a source read or required-checks wait.
    assert interrupted.kind == "progress" and interrupted.retry_at is None
    assert scenario.runner().state().wake_at is None
    with scenario.kernel.storage.transaction(write=False) as tx:
        assert not tx.rows("SELECT evidence FROM waits WHERE subject=?", (scenario.delivery.key,))
    world = scenario.world()
    assert world.checks is not None and world.checks.result == "none"
    assert world.pr is not None and world.checks.head_sha == world.pr.head.sha
    budgets_at_loss = scenario.kernel.status(scenario.delivery.key)["budgets"]
    calls_at_loss = list(scenario.role_calls)
    # A separate operation continues business after the optional response loss;
    # the original indeterminate display mutation must never be replayed.
    boundary = scenario.run()
    assert boundary.kind == "complete", boundary
    assert len(attempts) == 1
    original = attempts[0]
    durable = Kernel(scenario.port.root / "kernel.sqlite3").status(scenario.delivery.key)
    assert durable["phase"] == "complete"
    assert durable["issue_link"]["state"] == "indeterminate"
    assert durable["issue_link"]["source_sha"] == original.payload["source_head"]
    assert durable["budgets"] == budgets_at_loss
    assert scenario.role_calls == calls_at_loss
    calls_before, writes_before = list(scenario.role_calls), scenario.port.writes()
    assert sum(write["operation"] == "issue_link" for write in writes_before) == 1
    scenario.kernel = Kernel(scenario.port.root / "kernel.sqlite3")
    scenario.port = OfflinePort(scenario.port.root)
    # Real durable external link exists; observing it cannot manufacture the
    # missing original acknowledgement or authorize a second optional mutation.
    assert scenario.port.reconcile(original).disposition == "target"
    boundary = scenario.run()
    assert boundary.kind == "complete", boundary
    assert scenario.kernel.status(scenario.delivery.key)["issue_link"] == durable["issue_link"]
    assert scenario.kernel.status(scenario.delivery.key)["phase"] == "complete"
    assert scenario.kernel.status(scenario.delivery.key)["budgets"] == durable["budgets"]
    assert scenario.role_calls == calls_before
    assert scenario.port.writes() == writes_before
