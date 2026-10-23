"""Confirmed retirement evidence survives a later external PR state cycle."""
from dataclasses import replace

import pytest

from agent_run.model import DeliveryKind
from support.offline import Scenario
from support.offline_api import SyntheticGitHub


@pytest.mark.parametrize("read_gap", [None, "original_event", "actor", "time", "duplicate_id"])
def test_confirmed_retirement_recheck_requires_complete_ordered_foreign_history(tmp_path, monkeypatch, read_gap):
    scenario = Scenario(tmp_path / "parent", DeliveryKind.PARENT_ONLY)
    kernel, subject = scenario.kernel, scenario.delivery.key
    kernel.clock = lambda: 100.25
    assert scenario.run().kind == "approval"
    delivery = kernel.delivery(subject)
    scenario.requirements = replace(scenario.requirements,
        parent=replace(scenario.requirements.parent, body="First updated requirement"))
    world = scenario.world()
    replacement = replace(delivery, source_ref=delivery.source_ref + "-g2",
        checkout=delivery.checkout + "-g2", base=world.target)
    assert kernel.control("requeue", "first", subject=subject, world=world, replacement=replacement) == "pending"
    assert scenario.runner().run_once().kind == "progress"
    scenario.requirements = replace(scenario.requirements,
        parent=replace(scenario.requirements.parent, body="Second updated requirement"))
    assert scenario.step().kind == "paused"
    with kernel.storage.transaction(write=False) as tx:
        original = dict(tx.one("SELECT * FROM effects WHERE subject=? AND operation='retire_pr'", (subject,)))
        payload = tx.get(original["payload_id"], subject)
        receipt = tx.get(original["receipt_id"], subject)
    assert original["status"] == "confirmed" and original["attempts"] == 1
    api = SyntheticGitHub(scenario.port.git, scenario.port.database, scenario.spec.repository,
        physical=scenario.port.physical, actor="another-maintainer", written=lambda *args: None, read_faults=set())
    endpoint = f"repos/{scenario.spec.repository}/pulls/{original['pr_number']}"
    assert api.write(endpoint, "PATCH", {"state": "open"}).returncode == 0
    assert api.write(endpoint, "PATCH", {"state": "closed"}).returncode == 0
    assert [event["event"] for event in scenario.port.objects.events(original["pr_number"])] == ["closed", "reopened", "closed"]
    raw_before = scenario.port.objects.snapshot()
    writes_before = scenario.port.writes()
    read = SyntheticGitHub.list

    def observed_events(self, path):
        rows = read(self, path)
        if self.database != scenario.port.database or not path.endswith("/events?per_page=100"):
            return rows
        if read_gap == "original_event":
            return rows[1:]
        if read_gap == "actor":
            return [*rows[:-1], {**rows[-1], "actor": None}]
        if read_gap == "time":
            return [*rows[:-1], {**rows[-1], "created_at": "2000-01-01T00:00:00Z"}]
        if read_gap == "duplicate_id":
            return [*rows[:-1], {**rows[-1], "id": rows[0]["id"]}]
        return rows

    monkeypatch.setattr(SyntheticGitHub, "list", observed_events)
    latest = scenario.world()
    assert kernel.control("requeue", "second", subject=subject, world=latest,
        replacement=replace(replacement, base=latest.target)) == "pending"
    step = scenario.runner().run_once()
    assert (step.kind, step.reason) == (("progress", "requeue_retirement_changed") if read_gap is None
                                      else ("paused", "write_result_unknown"))
    status = kernel.status(subject)
    assert status["generation"] == 1 and status["pending_write"] is None and status["retired_generations"] == []
    with kernel.storage.transaction(write=False) as tx:
        assert dict(tx.one("SELECT * FROM effects WHERE id=?", (original["id"],))) == original
        assert tx.get(original["payload_id"], subject) == payload
        assert tx.get(original["receipt_id"], subject) == receipt
        assert tx.one("SELECT status FROM actions WHERE id='second'")[0] == ("superseded" if read_gap is None else "pending")
    assert scenario.port.objects.snapshot() == raw_before
    assert scenario.port.writes() == writes_before
