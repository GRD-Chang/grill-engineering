"""Remote heads and original-intent readback, independent of local Git refs."""

from pathlib import Path

import pytest

from agent_run.model import DeliveryKind, Effect
from agent_run.core.kernel import Kernel
from support.offline import Scenario
from support.offline_git import OfflineGit
from agent_run.core.operation_contract import ReadUnavailable
from test_kernel_remote_effects import EffectBoundary, effect_boundary


def merge_intent(scenario: Scenario, monkeypatch: pytest.MonkeyPatch):
    with effect_boundary(scenario, monkeypatch, "merge"):
        with pytest.raises(EffectBoundary) as boundary:
            scenario.run()
    return boundary.value


def test_remote_source_advance_after_approval_prevents_merge_and_close(tmp_path: Path) -> None:
    scenario = Scenario(tmp_path / "scenario", DeliveryKind.PARENT_ONLY)
    assert scenario.run().kind == "approval"
    target = scenario.port.remote_commit(scenario.delivery.target_ref)
    source = scenario.port.ref_commit(scenario.delivery.source_ref)
    original_pr = scenario.port.pull_request_by_ref(scenario.delivery.source_ref, scenario.delivery.target_ref)
    original_counts = (scenario.development_count, scenario.review_count, scenario.publication_count)
    scenario.kernel.control("approve", "approve-exact-source", subject=scenario.delivery.key, world=scenario.world())
    third = scenario.port.external_remote_append(scenario.delivery.source_ref, {"foreign.txt": "New remote-only work\n"}, "remote-source")
    assert scenario.port.ref_commit(scenario.delivery.source_ref) == source
    assert scenario.world().pr.head == third
    assert scenario.run().kind == "approval"
    updated = scenario.port.remote_commit(scenario.delivery.source_ref)
    assert updated is not None and scenario.port.git.is_ancestor(third.sha, updated.sha)
    assert scenario.port.git.run("show", updated.sha + ":foreign.txt").stdout == "New remote-only work\n"
    assert scenario.development_count == original_counts[0]
    assert scenario.review_count == original_counts[1] + 1
    assert scenario.publication_count == original_counts[2] + 1
    assert scenario.kernel.status(scenario.delivery.key)["resync"]["used"] == 1
    assert scenario.port.pull_request_by_ref(scenario.delivery.source_ref, scenario.delivery.target_ref).number == original_pr.number
    assert scenario.port.remote_commit(scenario.delivery.target_ref) == target
    assert scenario.port.pull_request_by_ref(scenario.delivery.source_ref, scenario.delivery.target_ref).state == "OPEN"
    assert not [write for write in scenario.port.writes() if write["operation"] in {"merge", "close"}]


@pytest.mark.parametrize("server_committed", [False, True])
def test_original_merge_readback_distinguishes_server_commit_from_partial_local_mirror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, server_committed: bool,
) -> None:
    scenario = Scenario(tmp_path / "scenario", DeliveryKind.TICKET)
    now = [100.25]
    scenario.kernel.clock = lambda: now[0]
    boundary = merge_intent(scenario, monkeypatch)
    effect = boundary.effect
    before = scenario.port.remote_commit(effect.target_ref)
    if server_committed:
        scenario.port.lose_response.add("merge")
        with pytest.raises(ReadUnavailable, match="merge took effect; response lost") as lost:
            scenario.port.execute(effect, boundary.authorize)
        assert lost.value.code == "write_response_unavailable" and lost.value.write_returncode is None
    else:
        def fail_before_remote_commit(*args: object, **kwargs: object) -> None:
            raise OSError("Synthetic server transaction failed before remote commit")
        # A local branch advance is not a server merge receipt. The only remote
        # authority remains the actual bare repository; no second merge engine.
        scenario.port.git.run("update-ref", effect.target_ref, effect.after_sha)
        with monkeypatch.context() as physical_fault:
            physical_fault.setattr(OfflineGit, "merge", fail_before_remote_commit)
            with pytest.raises(ReadUnavailable, match="before remote commit") as failed:
                scenario.port.execute(effect, boundary.authorize)
            assert failed.value.code == "operation_io_unavailable" and failed.value.write_returncode is None
        assert scenario.port.remote_commit(effect.target_ref) == before
        assert scenario.port.ref_commit(effect.target_ref) != before
        assert scenario.port.pull_request_by_ref(effect.source_ref, effect.target_ref).state == "OPEN"
        readback = scenario.port.reconcile

        def unavailable_readback(original):
            # Preserve the unknown-readback story through a real raw API read
            # failure, rather than the retired synthetic unknown EffectResult.
            with monkeypatch.context() as read_fault:
                read_fault.setattr(scenario.port, "read_faults", {"*"})
                return readback(original)

        monkeypatch.setattr(scenario.port, "reconcile", unavailable_readback)
    if server_committed:
        observed = scenario.port.reconcile(effect)
        assert observed.disposition == "target"
    else:
        with pytest.raises(ReadUnavailable) as unavailable:
            scenario.port.reconcile(effect)
        assert unavailable.value.code == "remote_read_failed" and unavailable.value.write_returncode is None
    scenario.kernel = Kernel(scenario.kernel.storage.path)
    scenario.kernel.clock = lambda: now[0]
    waiting = scenario.runner().run_once()
    if server_committed:
        assert scenario.run().kind == "complete"
        assert len([write for write in scenario.port.writes() if write["operation"] == "merge"]) == 1
    else:
        assert scenario.kernel.status(effect.subject)["pending_write"]["id"] == effect.effect_id
        assert scenario.kernel.status(effect.subject)["holds_integration"] is True
        assert waiting.retry_at is not None
        now[0] = waiting.retry_at
        with effect_boundary(scenario, monkeypatch, "merge"):
            with pytest.raises(EffectBoundary) as pending:
                scenario.runner().run_once()
        assert pending.value.phase == "reconcile" and pending.value.effect == effect
