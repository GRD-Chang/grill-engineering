"""Each current object keeps its notification slot beyond the ordinary queue."""
import agent_run.storage.notification_outbox as outbox
from agent_run.notifications.sender import NativeNotifications
from support.notification import _kernel


def test_current_ticket_boundaries_do_not_compete_for_ordinary_capacity(tmp_path, monkeypatch):
    count = outbox.MAX_PENDING + 1
    kernel = _kernel(tmp_path, count=count)
    for number in range(count):
        assert kernel.control("stop", f"original-{number}", subject=f"native-ticket-{number}") == "applied"
    monkeypatch.setattr(NativeNotifications, "_work", lambda _self: None)
    observer = NativeNotifications(kernel.storage.path, tmp_path, storage=kernel.storage)
    # Projection-only unit seam; producer wake no longer enqueues synchronously.
    assert observer._observe_committed()
    observer.close(drain=False)
    assert observer.failure_reason is None
    document = outbox.read_notifications(tmp_path, "run-native")
    boundaries = [item for item in document["pending"] if item["event"]["kind"] == "boundary"]
    assert len(boundaries) == count
    assert {item["event"]["work_subject"] for item in boundaries} == {
        f"native-ticket-{number}" for number in range(count)}
    assert all(item["attempts"] == 0 for item in boundaries)
    assert sum(not item["event"].get("current") for item in document["pending"]) <= outbox.MAX_PENDING
