"""A late physical read remains auditable without retiring its original wait."""
import json

from agent_run.core.operation_contract import ReadUnavailable
from test_kernel_host_observation import sent_workspaces


def test_late_sent_target_is_audited_without_settling_or_renewing(tmp_path):
    scenario, runner, sent = sent_workspaces(tmp_path, count=1)
    now = [100.25]
    scenario.kernel.clock = lambda: now[0]
    reconcile = runner.ports.reconcile
    reads = []

    def physical(effect):
        reads.append(effect.effect_id)
        if len(reads) == 1:
            raise ReadUnavailable("github_read_failed", "Original read outage")
        result = reconcile(effect)
        assert result.disposition == "target"
        now[0] = 701.25
        return result

    runner.ports.reconcile = physical
    runner.activate_observation()
    assert runner.run_once().retry_at == 105.25
    now[0] = 105.25
    late = runner.run_once()
    assert (late.kind, late.reason) == ("paused", "supervision_timeout")
    with scenario.kernel.storage.transaction(write=False) as tx:
        assert tuple(tx.one("SELECT status,attempts FROM effects WHERE id=?", (sent[0].effect_id,))) == ("sent", 1)
        assert tuple(tx.one("SELECT started_at,deadline FROM waits")) == (100.25, 700.25)
        evidence = [json.loads(row[0]) for row in tx.rows(
            "SELECT m.body FROM history h JOIN material m ON m.id=h.material_id "
            "WHERE h.kind='effect_readback' AND h.identity=?", (sent[0].effect_id,))]
        assert evidence[-1]["result"]["disposition"] == "target"
        assert evidence[-1]["dispatch_serial"] == sent[0].dispatch_serial
    assert runner.state().disposition == "quiescent"
    runner.activate_observation()
    assert runner.state().disposition == "quiescent"
    assert reads == [sent[0].effect_id, sent[0].effect_id]
