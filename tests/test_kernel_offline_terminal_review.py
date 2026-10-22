"""Independent business terminal/resource obligation distinction; no SQL writes."""
import test_kernel_cleanup_local_witness as witness


def test_foreign_local_ref_keeps_unallocated_remote_cleanup_after_business_complete(tmp_path, monkeypatch):
    scenarios = []
    factory = witness.GraphScenario

    def capture(*args, **kwargs):
        scenario = factory(*args, **kwargs)
        scenarios.append(scenario)
        return scenario

    monkeypatch.setattr(witness, "GraphScenario", capture)
    # Run every original foreign-protection assertion unchanged, including
    # retained local+remote refs and only worktree/local cleanup operations.
    witness.test_generation_two_cleanup_uses_the_original_local_marker_and_published_remote_head(
        tmp_path, monkeypatch, True)
    scenario, = scenarios
    kernel, subject = scenario.kernel, scenario.delivery.key
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
