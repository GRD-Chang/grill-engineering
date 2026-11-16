"""Independent business terminal/resource obligation distinction; no SQL writes."""
from support.cleanup_scenarios import prepared_generation_two_cleanup


def test_foreign_local_ref_keeps_unallocated_remote_cleanup_after_business_complete(tmp_path, monkeypatch):
    scenario, original, replacement, original_marker, cleanups = prepared_generation_two_cleanup(tmp_path, monkeypatch, True)
    kernel, subject = scenario.kernel, scenario.delivery.key
    assert scenario.run().kind == "complete"
    cleanup = kernel.status(subject)["resource_cleanup"]
    pr = scenario.port.pull_request_by_ref(replacement.source_ref, replacement.target_ref)
    assert pr is not None and pr.merged is not None
    assert cleanup["source_sha"] == pr.head.sha
    assert scenario.port.git.commit(original.source_ref).sha == original_marker
    assert cleanup["stage"] == 1 and cleanup["outcome"] == "third"
    assert scenario.port.git.commit(replacement.source_ref).sha == pr.head.sha
    assert scenario.port.remote_refs()[replacement.source_ref] == pr.head.sha
    assert [effect.operation for effect in cleanups] == ["cleanup_worktree", "cleanup_local_ref"]
    before = kernel.status(subject)
    assert before["phase"] == "complete"
    assert (before["resource_cleanup"]["stage"], before["resource_cleanup"]["outcome"]) == (1, "third")
    with kernel.storage.transaction(write=False) as tx:
        assert tuple(tx.one("SELECT status,attempts FROM effects WHERE subject=? AND generation=2 AND operation='cleanup_local_ref'", (subject,))) == ("prepared", 0)
        assert not tx.rows("SELECT id FROM effects WHERE subject=? AND generation=2 AND operation='cleanup_remote_ref'", (subject,))
    # The runner is global. Hold the unrelated eligible sibling through public
    # control so this probe isolates the completed delivery's resource return,
    # rather than consuming cleanup()'s16-step cap on another Ticket's work.
    kernel.control("stop", "hold-unrelated-sibling-for-resource-probe", subject="ticket:4")
    writes, calls = scenario.port.writes(), list(scenario.role_calls)
    resource_boundary = scenario.cleanup()
    assert resource_boundary.kind in {"paused", "waiting"}
    assert kernel.status(subject)["resource_cleanup"] == before["resource_cleanup"]
    assert scenario.run().kind == "complete"
    assert kernel.status(subject)["resource_cleanup"] == before["resource_cleanup"]
    assert scenario.port.writes() == writes and scenario.role_calls == calls
