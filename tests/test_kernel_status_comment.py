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


def published(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, notifications=None, kind=DeliveryKind.PARENT_ONLY) -> Scenario:
    # Use the real Task creation API instead of inventing its location pointer.
    original = Kernel.create.__func__
    def create(cls: type[Kernel], path: Path, spec: Any, **kwargs: Any) -> Kernel:
        if notifications is not None:
            from dataclasses import replace
            assert spec.inputs is not None
            spec = replace(spec, inputs=replace(spec.inputs, notifications=notifications))
        with monkeypatch.context() as patch:
            patch.setattr(Kernel, "create", classmethod(original))
            return Kernel.create_task(path, spec, requirements=kwargs["requirements"])
    with monkeypatch.context() as patch:
        patch.setattr(Kernel, "create", classmethod(create))
        scenario = Scenario(tmp_path / "run", kind)
    assert scenario.run().kind == ("complete" if kind == DeliveryKind.TICKET else "approval")
    return scenario


@pytest.mark.parametrize("kind", [DeliveryKind.TICKET, DeliveryKind.PARENT_ONLY, DeliveryKind.FINAL_RUN])
def test_status_projection_and_intent_are_readonly_and_frozen(tmp_path, monkeypatch, kind):
    scenario = published(tmp_path, monkeypatch, kind=kind)
    database, subject = scenario.kernel.storage.path, scenario.delivery.key
    before = database.read_bytes()
    projection = project_status(database, subject)
    assert projection.target.pr_number == scenario.kernel.status(subject)["pr_number"]
    assert projection.target.issue == (2 if kind == DeliveryKind.TICKET else 1)
    assert projection.target.parent_issue == 1
    assert scenario.kernel.operation_binding().inputs.notifications["enabled"] is False
    assert projection.target.actor == "offline-publisher"
    assert "Agent Run Status" in projection.body
    assert ("Delivery: complete" if kind == DeliveryKind.TICKET else "Delivery: approval") in projection.body
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


def test_status_lost_create_and_patch_reconcile_original_without_reposting(tmp_path, monkeypatch):
    scenario = published(tmp_path, monkeypatch)
    database, subject = scenario.kernel.storage.path, scenario.delivery.key
    sender = StatusComments(database)
    api = CommentsAPI(project_status(database, subject).target)
    before = scenario.kernel.status(subject)
    api.lose = True
    assert sender.deliver(subject, api=api) == "unavailable"
    assert len(api.commands) == 1
    api.hidden = True
    scenario.kernel.control("stop", "status-stop")
    original = sender.stage(subject)
    assert original["sent"] is True
    assert "operator_stopped" not in original["body"]
    assert StatusComments(database).deliver(subject, api=api) == "unknown"
    assert len(api.commands) == 1  # Absence cannot authorize a duplicate POST.
    api.hidden = False
    api.lose = False
    assert sender.deliver(subject, api=api) == "confirmed"  # Original create, not the later Stop display.
    assert sender.deliver(subject, api=api) == "confirmed"
    assert [cmd[1] for cmd in api.commands] == ["POST", "PATCH"]
    assert "operator_stopped" in api.comments[0]["body"]
    count = api.reads
    assert sender.deliver(subject, api=api) == "unchanged"
    assert api.reads == count
    assert scenario.kernel.status(subject)["budgets"] == before["budgets"]
    assert scenario.kernel.status(subject)["pending_write"] is None
    with scenario.kernel.storage.transaction(write=False) as tx:
        assert tx.one("SELECT final_action FROM runs")[0] is None
        assert tx.one("SELECT grant_id FROM subjects WHERE id=?", (subject,))[0] is None
    scenario.kernel.confirm_executor_exit(scenario.execution)
    scenario.kernel.control("resume", "status-resume")
    api.lose = True
    assert sender.deliver(subject, api=api) == "unavailable"
    pending = sender.stage(subject)
    assert pending["comment_id"] == 100 and pending["sent"]
    scenario.kernel.control("stop", "status-stop-again")
    assert sender.stage(subject) == pending  # A later display cannot overwrite the dispatched PATCH.
    api.lose = False
    assert StatusComments(database).deliver(subject, api=api) == "confirmed"
    assert [cmd[1] for cmd in api.commands] == ["POST", "PATCH", "PATCH"]
    assert "operator_stopped" not in api.comments[0]["body"]
    assert sender.deliver(subject, api=api) == "confirmed"
    assert [cmd[1] for cmd in api.commands] == ["POST", "PATCH", "PATCH", "PATCH"]
    assert len(api.comments) == 1
    assert "private diagnostic" not in outbox.notification_path(database.parent, scenario.spec.run_id).read_text()


def test_status_remote_rejects_foreign_deleted_edited_and_duplicate_objects(tmp_path, monkeypatch):
    from copy import deepcopy
    from agent_run.adapters.remote import RemoteEffects
    from agent_run.contracts.write_authority import WriteAuthority
    scenario = published(tmp_path, monkeypatch)
    database, subject = scenario.kernel.storage.path, scenario.delivery.key
    projection = project_status(database, subject)
    sender = StatusComments(database)
    api = CommentsAPI(projection.target)
    assert sender.deliver(subject, api=api) == "confirmed"
    scenario.kernel.control("stop", "display-only-local-stop", subject=subject)
    intent = sender.stage(subject)
    binding = scenario.kernel.operation_binding()
    remote = RemoteEffects(binding.repository_root, binding.repository,
                           runner_identity=binding.inputs.runner_identity, api=api)
    original = deepcopy(api.comments)
    # >100 unrelated comments must not hide the owned comment at the end.
    api.comments = [{"id": n, "body": "other", "user": {"login": "other"}} for n in range(200, 351)] + original
    marker = intent["body"].split("\n", 1)[0]
    assert remote.read_status_comment(projection.target, marker, intent, lambda: False)["disposition"] == "prior"
    cases = []
    cases.append([])  # deletion
    edited = deepcopy(original); edited[0]["body"] += "\nHuman edit"; cases.append(edited)
    foreign = deepcopy(original); foreign[0]["user"]["login"] = "someone-else"; cases.append(foreign)
    wrong_pr = deepcopy(original); wrong_pr[0]["issue_url"] = "https://api.github.com/repos/example/repository/issues/999"; cases.append(wrong_pr)
    duplicate = deepcopy(original); duplicate.append({**original[0], "id": 101}); cases.append(duplicate)
    lost_binding = deepcopy(original); lost_binding[0]["body"] = lost_binding[0]["body"].replace(marker[-35:-3], "0" * 32); cases.append(lost_binding)
    for rows in cases:
        api.comments = rows
        assert remote.read_status_comment(projection.target, marker, intent, lambda: False)["disposition"] == "third"
        with pytest.raises(ValueError, match="changed"):
            remote.write_status_comment(projection.target, marker, intent, WriteAuthority(lambda: lambda: None), lambda: False)
    assert len(api.commands) == 1  # No PATCH/POST for any invalid boundary.
    api.comments = deepcopy(original)
    real_object = api.object
    for changed in ("actor", "pr", "repository", "ref"):
        def object_with_changed_binding(endpoint, changed=changed):
            row = real_object(endpoint)
            if endpoint == "user":
                if changed == "actor":
                    row["login"] = "different-login"
            elif changed == "pr":
                row["number"] += 1
            elif changed == "repository":
                row["head"]["repo"]["full_name"] = "foreign/repository"
            elif changed == "ref":
                row["head"]["ref"] = "foreign-branch"
            return row
        with monkeypatch.context() as patch:
            patch.setattr(api, "object", object_with_changed_binding)
            assert remote.read_status_comment(projection.target, marker, intent, lambda: False)["disposition"] == "third"
    # Loss of the local binding cannot adopt an old comment with a different
    # create marker, even if it has the same actor/Run slot and identical copy.
    replacement_marker = marker.replace(marker.rsplit(":", 1)[1].split()[0], "f" * 32)
    unbound = {**intent, "comment_id": None, "before": None, "body": intent["body"].replace(marker, replacement_marker)}
    assert remote.read_status_comment(projection.target, replacement_marker, unbound, lambda: False)["disposition"] == "third"
    # A foreign edit pauses the binding persistently, even if later undone.
    api.comments[0]["body"] += "\nHuman edit"
    assert sender.deliver(subject, api=api) == "foreign"
    count = api.reads
    api.comments = original
    assert sender.deliver(subject, api=api) == "unavailable"
    assert api.reads == count
    scenario.kernel.control("resume", "resume-business-only", subject=subject)
    assert scenario.run(approve=True).kind == "complete"  # No Status success is required for delivery.


def test_status_concurrent_sender_and_final_stop_cancel_before_post(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    scenario = published(tmp_path, monkeypatch)
    database, subject = scenario.kernel.storage.path, scenario.delivery.key
    sender = StatusComments(database)
    api = CommentsAPI(project_status(database, subject).target)
    entered, release = Event(), Event()
    real_list, calls = api.list, 0
    def blocked_boundary(endpoint):
        nonlocal calls
        calls += 1
        if calls == 2:
            entered.set()
            assert release.wait(5)
        return real_list(endpoint)
    api.list = blocked_boundary
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(sender.deliver, subject, api=api)
        try:
            assert entered.wait(5)
            assert StatusComments(database).deliver(subject, api=api) == "busy"
            scenario.kernel.control("stop", "during-status-boundary", subject=subject)
        finally:
            release.set()
        assert future.result(timeout=10) == "unavailable"
    assert api.commands == []
    assert sender.stage(subject)["sent"] is False
    assert sender.deliver(subject, api=api, cancelled=lambda: True) == "unavailable"
    assert api.commands == []
    assert sender.deliver(subject, api=api) == "confirmed"
    assert len(api.commands) == 1 and "operator_stopped" in api.comments[0]["body"]
    # Real read subprocess: cancellation is checked again before a transport
    # retry, not only between outer observation calls.
    import os
    import sys
    from agent_run.adapters.remote import RemoteEffects
    log, binary = tmp_path / "read-attempts", tmp_path / "bin"
    binary.mkdir()
    fake = binary / "gh"
    fake.write_text("#!" + sys.executable + "\nfrom pathlib import Path\n" +
                    "p=Path(" + repr(str(log)) + ");p.write_text(p.read_text()+'x' if p.exists() else 'x')\nraise SystemExit(1)\n")
    fake.chmod(0o700)
    monkeypatch.setenv("PATH", str(binary) + os.pathsep + os.environ["PATH"])
    binding = scenario.kernel.operation_binding()
    physical = RemoteEffects(binding.repository_root, binding.repository, runner_identity=binding.inputs.runner_identity)
    projection = project_status(database, subject)
    intent = {"comment_id": None, "before": None, "body": "unused"}
    with pytest.raises(ValueError, match="cancelled"):
        physical.read_status_comment(projection.target, "unused", intent, log.exists)
    assert log.read_text() == "x"


def test_status_capacity_and_damaged_records_leave_business_untouched(tmp_path, monkeypatch):
    scenario = published(tmp_path, monkeypatch)
    database, subject = scenario.kernel.storage.path, scenario.delivery.key
    sender = StatusComments(database)
    api = CommentsAPI(project_status(database, subject).target)
    before = database.read_bytes()
    with monkeypatch.context() as patch:
        patch.setattr(outbox, "MAX_JOURNAL_BYTES", 32)
        assert sender.deliver(subject, api=api) == "unavailable"
    assert api.reads == 0 and api.commands == []
    assert database.read_bytes() == before
    assert sender.deliver(subject, api=api) == "confirmed"
    path = outbox.notification_path(database.parent, scenario.spec.run_id)
    path.write_text('{"remote_status":')
    assert sender.deliver(subject, api=api) == "unavailable"
    assert database.read_bytes() == before
    scenario.kernel.control("stop", "still-revocable")
    assert scenario.kernel.status(subject)["stopped"]
    database.write_bytes(b"damaged sqlite")
    count = api.reads
    assert sender.deliver(subject, api=api) == "unavailable"
    assert api.reads == count


@pytest.mark.parametrize("dispatched", [True, False], ids=["sent", "prepared"])
def test_status_real_sender_exit_retains_sent_intent_through_journal_compaction(tmp_path, monkeypatch, dispatched):
    import json
    import os
    import subprocess
    import sys
    scenario = published(tmp_path, monkeypatch)
    database, subject = scenario.kernel.storage.path, scenario.delivery.key
    evidence = tmp_path / "physical-comment.json"
    script = '''
import json,os,sys
from pathlib import Path
from test_kernel_status_comment import CommentsAPI
from agent_run.queries.status_comment import project_status
from agent_run.notifications.status_comment import StatusComments
p,subject,output=Path(sys.argv[1]),sys.argv[2],Path(sys.argv[3])
dispatched=sys.argv[4]=='True'
class Interrupted(CommentsAPI):
 def object(self,endpoint):
  if not dispatched:os._exit(18)
  return super().object(endpoint)
 def write(self,*args):
  super().write(*args)
  output.write_text(json.dumps({'comments':self.comments,'commands':self.commands}))
  os._exit(17)
a=Interrupted(project_status(p,subject).target)
StatusComments(p).deliver(subject,api=a)
raise AssertionError('physical dispatch was not reached')
'''
    before = database.read_bytes()
    result = subprocess.run([sys.executable, "-c", script, str(database), subject, str(evidence), str(dispatched)],
                            env=dict(os.environ), capture_output=True, text=True, timeout=20)
    assert result.returncode == (17 if dispatched else 18), result.stderr
    assert database.read_bytes() == before
    physical = json.loads(evidence.read_text()) if dispatched else {"comments": [], "commands": []}
    assert len(physical["commands"]) == int(dispatched)
    with outbox.transaction(database.parent, scenario.spec.run_id) as document:
        entry, = document["remote_status"]["bindings"].values()
        assert entry["intent"]["sent"] is dispatched and entry["claim"]["pid"] != os.getpid()
        original_intent = dict(entry["intent"])
        for n in range(outbox.MAX_RECORDS + 1):
            outbox.enqueue(document, {"id": f"other-{n}", "kind": "stage_start"}, 0)
            document["pending"][-1]["outcome"] = "success"
            outbox.settle(document, 0)
        assert len(document["records"]) == outbox.MAX_RECORDS
        assert entry["intent"] == original_intent
    if not dispatched:
        scenario.kernel.control("stop", "after-preparation-exit", subject=subject)
        before = database.read_bytes()
    api = CommentsAPI(project_status(database, subject).target)
    api.comments = physical["comments"]
    assert StatusComments(database).deliver(subject, api=api) == "confirmed"
    if dispatched:
        assert api.commands == []  # Readback after a real process exit, not a second POST.
    else:
        assert len(api.commands) == 1 and "operator_stopped" in api.comments[0]["body"]
    assert database.read_bytes() == before


def test_status_denied_post_and_failed_readback_keep_observed_exit_without_retry(tmp_path, monkeypatch):
    import subprocess
    scenario = published(tmp_path, monkeypatch)
    database, subject = scenario.kernel.storage.path, scenario.delivery.key
    class Denied(CommentsAPI):
        readback_unavailable = False
        def write(self, endpoint, method, body):
            self.commands.append((endpoint, method, dict(body)))
            self.readback_unavailable = True
            return subprocess.CompletedProcess([], 1, "", "HTTP 403; secret must not enter journal")
        def list(self, endpoint):
            if self.readback_unavailable:
                from agent_run.errors import GitHubReadError
                raise GitHubReadError("synthetic_read_failed", "secret must not enter journal")
            return super().list(endpoint)
    api = Denied(project_status(database, subject).target)
    sender = StatusComments(database)
    before = database.read_bytes()
    assert sender.deliver(subject, api=api) == "unknown"
    api.readback_unavailable = False
    assert sender.deliver(subject, api=api) == "unknown"
    assert len(api.commands) == 1
    document = outbox.read_notifications(database.parent, scenario.spec.run_id)
    entry, = document["remote_status"]["bindings"].values()
    assert entry["write_result"] == {"intent": entry["intent"]["id"], "returncode": 1}
    assert "secret must" not in outbox.notification_path(database.parent, scenario.spec.run_id).read_text()
    assert database.read_bytes() == before


def test_status_missing_intent_field_is_damaged_display_not_business_exception(tmp_path, monkeypatch):
    scenario = published(tmp_path, monkeypatch)
    database, subject = scenario.kernel.storage.path, scenario.delivery.key
    sender = StatusComments(database)
    sender.stage(subject)
    with outbox.transaction(database.parent, scenario.spec.run_id) as document:
        entry, = document["remote_status"]["bindings"].values()
        del entry["intent"]
    api = CommentsAPI(project_status(database, subject).target)
    assert sender.deliver(subject, api=api) == "unavailable"
    assert api.commands == [] and api.reads == 0


def test_status_and_existing_enabled_sender_share_one_original_run_binding(tmp_path, monkeypatch):
    from agent_run.notifications.sender import NativeNotifications
    scenario = published(tmp_path, monkeypatch, notifications={"enabled": True, "mode": "concise",
                         "profile": "fixture", "open_id": "ou_fixture", "app_id": "app_fixture"})
    database, subject = scenario.kernel.storage.path, scenario.delivery.key
    sender = StatusComments(database)
    sender.stage(subject)
    before = database.read_bytes()
    sent = []
    def transport(*args, **kwargs):
        sent.append(args[2])
        return {"outcome": "success", "reason": None}
    observer = NativeNotifications(database, database.parent, transport=transport)
    observer.close()
    assert observer.failure_reason is None
    assert sent
    api = CommentsAPI(project_status(database, subject).target)
    assert sender.deliver(subject, api=api) == "confirmed"
    document = outbox.read_notifications(database.parent, scenario.spec.run_id)
    assert document["native_run_start"] is True
    assert document["pending"] == []
    assert len(document["remote_status"]["bindings"]) == 1
    assert database.read_bytes() == before


def test_status_state_change_between_staging_and_claim_fences_original_copy(tmp_path, monkeypatch):
    scenario = published(tmp_path, monkeypatch)
    database, subject = scenario.kernel.storage.path, scenario.delivery.key
    sender = StatusComments(database)
    api = CommentsAPI(project_status(database, subject).target)
    stage = sender.stage
    def changed_after_stage(owner):
        original = stage(owner)
        scenario.kernel.control("stop", "between-status-stage-and-claim", subject=subject)
        return original
    with monkeypatch.context() as patch:
        patch.setattr(sender, "stage", changed_after_stage)
        assert sender.deliver(subject, api=api) == "unavailable"
    assert api.commands == []
    assert sender.deliver(subject, api=api) == "confirmed"
    assert len(api.comments) == 1 and "operator_stopped" in api.comments[0]["body"]
