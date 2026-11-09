"""Synthetic comment API objects for Status tests and their real child processes."""
from functools import partial
from pathlib import Path

from agent_run.contracts.status_comment import StatusProjection, StatusTarget
from agent_run.core.kernel import Kernel
from agent_run.core.operation_contract import OperationBinding
from agent_run.model import CreationInputs
from agent_run.notifications.status_comment import StatusComments
from agent_run.queries.status_comment import project_status


def core_status_comments(kernel: Kernel) -> StatusComments:
    """Wire real read-only Core facts for the retained integration stories."""
    return StatusComments(kernel.storage.path.parent,
        read_projection=partial(project_status, kernel.storage.path, storage=kernel.storage),
        read_binding=kernel.operation_binding)


def display_inputs(root: Path) -> tuple[StatusProjection, OperationBinding]:
    """Recorded display inputs, without claiming a Core publication happened."""
    target = StatusTarget('example/project', 'frozen-run', 1, 'parent:1', 1, 1, 7,
        'offline-publisher', 'publication', 'receipt', 'refs/heads/work', 'refs/heads/main')
    inputs = CreationInputs(profiles={}, prompt_resources={}, worker_identity={},
        runner_identity={'source': 'host'}, notifications={}, provenance={})
    return StatusProjection(target, 'original'), OperationBinding(root, target.repository, 1, inputs)


class CommentsAPI:
    """Independent remote objects; successful writes may lose their response."""
    def __init__(self, target):
        self.target = target
        self.comments = []
        self.commands = []
        self.reads = 0
        self.lose = False
        self.hidden = False
        self.before_write = lambda: None

    def object(self, endpoint):
        self.reads += 1
        if endpoint == "user":
            return {"login": "offline-publisher"}
        assert endpoint == f"repos/{self.target.repository}/pulls/{self.target.pr_number}"
        return {"number": self.target.pr_number, "user": {"login": "offline-publisher"},
                "head": {"ref": self.target.source_ref.removeprefix("refs/heads/"),
                         "repo": {"full_name": self.target.repository}},
                "base": {"ref": self.target.target_ref.removeprefix("refs/heads/"),
                         "repo": {"full_name": self.target.repository}}}

    def list(self, endpoint):
        self.reads += 1
        assert endpoint == f"repos/{self.target.repository}/issues/{self.target.pr_number}/comments?per_page=100"
        return [] if self.hidden else [dict(row) for row in self.comments]

    def write(self, endpoint, method, body):
        import subprocess
        self.before_write()
        self.commands.append((endpoint, method, dict(body)))
        if method == "POST":
            assert endpoint == f"repos/{self.target.repository}/issues/{self.target.pr_number}/comments"
            self.comments.append({"id": 100 + len(self.comments), "user": {"login": "offline-publisher"},
                                  "body": body["body"],
                                  "issue_url": f"https://api.github.com/repos/{self.target.repository}/issues/{self.target.pr_number}"})
        else:
            assert method == "PATCH"
            row, = [row for row in self.comments if endpoint == f"repos/{self.target.repository}/issues/comments/{row['id']}"]
            row["body"] = body["body"]
        if self.lose:
            raise OSError("lost reply; private diagnostic must not enter journal")
        return subprocess.CompletedProcess([], 0, "", "")


def assert_completed_ticket_sender(scenario, *, summary, completed):
    """Consume the caller's original Completion, without another Git journey."""
    import json
    from agent_run.notifications.sender import NativeNotifications
    from agent_run.storage import notification_outbox as outbox

    database, subject = scenario.kernel.storage.path, scenario.delivery.key
    with scenario.kernel.storage.transaction(write=False) as tx:
        original = tx.one("SELECT e.receipt_id,c.development_attempt FROM completions c "
                          "JOIN effects e ON e.id=c.close_effect WHERE c.subject=?", (subject,))
        receipt = original["receipt_id"]
    before = database.read_bytes()
    projection = project_status(database, subject, storage=scenario.kernel.storage)
    assert projection.target.pr_number == scenario.kernel.status(subject)["pr_number"]
    assert projection.target.issue == scenario.delivery.issue and projection.target.parent_issue == 1
    assert projection.target.actor == "offline-publisher"
    assert "Recorded Review: pass" in projection.body and "Delivery: complete" in projection.body
    assert "not_recorded" in projection.body
    sender = core_status_comments(scenario.kernel)
    intent = sender.stage(subject)
    assert intent is not None and intent["body"].endswith(projection.body) and intent["sent"] is False
    assert sender.stage(subject) == intent and database.read_bytes() == before
    captured = []
    def transport(_config, rendered, *_args, **_kwargs):
        active, = [row["event"] for row in outbox.read_notifications(database.parent, scenario.spec.run_id)["pending"]
                   if row["outcome"] == "sending"]
        captured.append((active, rendered))
        return {"outcome": "success", "reason": None}
    api = CommentsAPI(project_status(database, subject, storage=scenario.kernel.storage).target)
    observer = NativeNotifications(database, database.parent, status_api=api, transport=transport, storage=scenario.kernel.storage)
    try:
        observer.close()
        assert observer.failure_reason is None and observer.status_failure_reason is None
        event, rendered = next(item for item in captured if item[0]["kind"] == "ticket_completed")
        assert sum(item[0]["kind"] == "ticket_completed" for item in captured) == 1
        assert event["source_material_id"] == receipt
        assert event["summary"] == summary
        assert event["ticket_progress"] == {"completed": 1, "total": 1} and event["parent_only"] is False
        assert event["status_query"] == f"agent-run status --repo {scenario.spec.repository} -- {scenario.spec.run_id}"
        assert event["id"] == completed["id"] and event["effort"] == completed["effort"]
        assert event["total_seconds"] == completed["total_seconds"]
        assert event["execution_timing_complete"] == completed["execution_timing_complete"]
        assert "1/1" in json.dumps(rendered, ensure_ascii=False)
        records = outbox.read_notifications(database.parent, scenario.spec.run_id)["records"]
        assert any(row["id"] == event["id"] and row["outcome"] == "success" for row in records)
        assert not observer.thread.is_alive() and database.read_bytes() == before
    finally:
        observer.close(drain=False)

