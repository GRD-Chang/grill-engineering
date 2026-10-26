"""A Status comment is a recoverable projection, never delivery authority."""
from pathlib import Path
from typing import Any

import pytest

from agent_run.core.kernel import Kernel
from agent_run.model import DeliveryKind
from agent_run.notifications.status_comment import StatusComments
from agent_run.queries.status_comment import project_status
from agent_run.storage import notification_outbox as outbox
from support.offline import Scenario


def published(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Scenario:
    # Use the real Task creation API instead of inventing its location pointer.
    original = Kernel.create.__func__
    def create(cls: type[Kernel], path: Path, spec: Any, **kwargs: Any) -> Kernel:
        with monkeypatch.context() as patch:
            patch.setattr(Kernel, "create", classmethod(original))
            return Kernel.create_task(path, spec, requirements=kwargs["requirements"])
    with monkeypatch.context() as patch:
        patch.setattr(Kernel, "create", classmethod(create))
        scenario = Scenario(tmp_path / "run", DeliveryKind.PARENT_ONLY)
    assert scenario.run().kind == "approval"
    return scenario


def test_status_projection_and_intent_are_readonly_and_frozen(tmp_path, monkeypatch):
    scenario = published(tmp_path, monkeypatch)
    database, subject = scenario.kernel.storage.path, scenario.delivery.key
    before = database.read_bytes()
    projection = project_status(database, subject)
    assert projection.target.pr_number == scenario.kernel.status(subject)["pr_number"]
    assert projection.target.issue == 1
    assert projection.target.actor == "offline-publisher"
    assert "Agent Run Status" in projection.body
    assert "approval" in projection.body
    assert "not_recorded" in projection.body  # This offline producer never launched a formal Host.
    sender = StatusComments(database)
    intent = sender.stage(subject)
    assert intent is not None
    assert intent["body"].endswith(projection.body)
    assert intent["sent"] is False
    assert sender.stage(subject) == intent
    assert database.read_bytes() == before
    document = outbox.read_notifications(database.parent, scenario.spec.run_id)
    assert document["remote_status"]["version"] == 1
    assert len(document["remote_status"]["bindings"]) == 1
    assert document["pending"] == []  # No business effect or second notification event.


def test_status_rejects_damaged_journal_and_task_without_mutating_core(tmp_path, monkeypatch):
    from agent_run.contracts.task import TaskKey
    from agent_run.storage.tasks import task_path
    scenario = published(tmp_path, monkeypatch)
    database, subject = scenario.kernel.storage.path, scenario.delivery.key
    sender = StatusComments(database)
    sender.stage(subject)
    before = database.read_bytes()
    with outbox.transaction(database.parent, scenario.spec.run_id) as document:
        document["remote_status"]["version"] = 999
    with pytest.raises(ValueError, match="journal"):
        sender.stage(subject)
    location = task_path(TaskKey(Path(scenario.spec.repository_root), scenario.spec.repository, 1))
    location.write_text('{"run_id":"other","database":"/tmp/other.sqlite3"}')
    with pytest.raises(ValueError, match="Task binding"):
        project_status(database, subject)
    assert database.read_bytes() == before
