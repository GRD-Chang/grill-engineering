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
from support.status_comments import CommentsAPI


def published(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, notifications=None, kind=DeliveryKind.PARENT_ONLY, runner_identity=None) -> Scenario:
    # Use the real Task creation API instead of inventing its location pointer.
    original = Kernel.create.__func__
    def create(cls: type[Kernel], path: Path, spec: Any, **kwargs: Any) -> Kernel:
        if notifications is not None or runner_identity is not None:
            from dataclasses import replace
            assert spec.inputs is not None
            values = {}
            if notifications is not None:
                values["notifications"] = notifications
            if runner_identity is not None:
                values["runner_identity"] = runner_identity
            spec = replace(spec, inputs=replace(spec.inputs, **values))
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
from support.status_comments import CommentsAPI
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
    script = "import sys;sys.path.insert(0,sys.argv.pop(1))\n" + script
    result = subprocess.run([sys.executable, "-c", script, str(Path(__file__).parent), str(database), subject, str(evidence), str(dispatched)],
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
    api = CommentsAPI(project_status(database, subject).target)
    assert sender.deliver(subject, api=api) == "confirmed"
    observer = NativeNotifications(database, database.parent, transport=transport, status_api=api)
    observer.close()
    assert observer.failure_reason is None
    assert sent
    assert [command[1] for command in api.commands] == ["POST"]
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


def test_concurrent_confirmation_is_no_work_before_claim_and_cancel_releases_claim(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    scenario = published(tmp_path, monkeypatch)
    database, subject = scenario.kernel.storage.path, scenario.delivery.key
    first, second = StatusComments(database), StatusComments(database)
    api = CommentsAPI(project_status(database, subject).target)
    entered, release = Event(), Event()
    stage = first.stage
    def staged(owner):
        value = stage(owner)
        entered.set()
        assert release.wait(5)
        return value
    before = database.read_bytes()
    with monkeypatch.context() as patch, ThreadPoolExecutor(max_workers=1) as pool:
        patch.setattr(first, "stage", staged)
        future = pool.submit(first.deliver, subject, api=api)
        try:
            assert entered.wait(5)
            assert second.deliver(subject, api=api) == "confirmed"
        finally:
            release.set()
        assert future.result(timeout=10) == "unchanged"
    assert [command[1] for command in api.commands] == ["POST"]
    assert database.read_bytes() == before
    scenario.kernel.control("stop", "cancel-status-after-concurrent-confirmation", subject=subject)
    assert first.deliver(subject, api=api, cancelled=lambda: True) == "unavailable"
    document = outbox.read_notifications(database.parent, scenario.spec.run_id)
    entry, = document["remote_status"]["bindings"].values()
    assert "claim" not in entry and entry["intent"]["sent"] is False
    assert second.deliver(subject, api=api) == "confirmed"
    assert first.deliver(subject, api=api) == "unchanged"
    assert [command[1] for command in api.commands] == ["POST", "PATCH"]


def test_app_identity_403_is_scoped_display_failure_without_dispatch(tmp_path, monkeypatch):
    from agent_run.adapters.auth.app import GitHubCredentialError
    identity = {"source": "app", "app_profile": {"app_id": "123", "installation_id": "456",
                "private_key_path": str(tmp_path / "unread-synthetic-key-reference.pem")},
                "app_read_only": False, "app_current_repository_only": True}
    # The existing physical fixture defaults to host. Select the declared App
    # in that adapter too; keep its real Git/provider objects and authority.
    from support.offline import OfflinePort
    from agent_run.adapters.remote import RemoteEffects
    original_remote = OfflinePort._remote
    def app_remote(port, *args, **kwargs):
        original = original_remote(port, *args, **kwargs)
        return RemoteEffects(original.root, original.repository, runner_identity=identity,
                             remote=original.remote, api=original.api, git_writes=original.git_writes)
    with monkeypatch.context() as patch:
        patch.setattr(OfflinePort, "_remote", app_remote)
        patch.setattr("agent_run.adapters.auth.app.resolve_app_login", lambda profile: "offline-publisher")
        scenario = published(tmp_path, monkeypatch, runner_identity=identity)
    database, subject = scenario.kernel.storage.path, scenario.delivery.key
    before = database.read_bytes()
    def denied(profile):
        assert profile.app_id == "123" and profile.installation_id == "456"
        raise GitHubCredentialError("synthetic-secret-must-not-be-logged", http_status=403)
    monkeypatch.setattr("agent_run.adapters.auth.app.resolve_app_login", denied)
    api = CommentsAPI(project_status(database, subject).target)
    assert StatusComments(database).deliver(subject, api=api) == "unavailable"
    document = outbox.read_notifications(database.parent, scenario.spec.run_id)
    entry, = document["remote_status"]["bindings"].values()
    assert entry["diagnostic"] == {"code": "github_credential_unavailable", "http_status": 403}
    assert "claim" not in entry and entry["intent"]["sent"] is False
    assert api.commands == [] and api.reads == 0
    assert database.read_bytes() == before
    assert "synthetic-secret" not in outbox.notification_path(database.parent, scenario.spec.run_id).read_text()

    def unexpected(profile):
        raise RuntimeError("unexpected program failure")
    monkeypatch.setattr("agent_run.adapters.auth.app.resolve_app_login", unexpected)
    with pytest.raises(RuntimeError, match="unexpected program failure"):
        StatusComments(database).deliver(subject, api=api)
    document = outbox.read_notifications(database.parent, scenario.spec.run_id)
    entry, = document["remote_status"]["bindings"].values()
    assert "claim" not in entry and entry["intent"]["sent"] is False
    assert database.read_bytes() == before


def test_existing_sender_automatically_updates_status_with_feishu_disabled(tmp_path, monkeypatch):
    from agent_run.notifications.sender import NativeNotifications
    from support.worker_process_timing import wait_until
    scenario = published(tmp_path, monkeypatch)
    database, subject = scenario.kernel.storage.path, scenario.delivery.key
    api = CommentsAPI(project_status(database, subject).target)
    def forbidden(*args, **kwargs):
        pytest.fail("Feishu was disabled in the frozen Run")
    observer = NativeNotifications(database, database.parent, transport=forbidden, status_api=api)
    try:
        wait_until(lambda: len(api.commands) == 1, timeout=5)
        assert [command[1] for command in api.commands] == ["POST"]
        for _ in range(3):
            observer.observe_committed()
        scenario.kernel.control("stop", "automatic-status-stop", subject=subject)
        observer.observe_committed()
        wait_until(lambda: len(api.commands) == 2, timeout=5)
        assert [command[1] for command in api.commands] == ["POST", "PATCH"]
        assert "operator_stopped" in api.comments[0]["body"]
    finally:
        observer.close()
    assert not observer.thread.is_alive()
    assert [command[1] for command in api.commands] == ["POST", "PATCH"]
    assert len(api.comments) == 1
    document = outbox.read_notifications(database.parent, scenario.spec.run_id)
    assert document["pending"] == [] and document["records"] == []


def test_automatic_unknown_status_is_not_polled_by_clock_or_passive_queries(tmp_path, monkeypatch):
    from agent_run.notifications.sender import NativeNotifications
    from agent_run.queries.run import read
    from support.worker_process_timing import wait_until
    scenario = published(tmp_path, monkeypatch)
    database, subject = scenario.kernel.storage.path, scenario.delivery.key
    api = CommentsAPI(project_status(database, subject).target)
    api.lose = True
    observer = NativeNotifications(database, database.parent, status_api=api)
    def unresolved():
        doc = outbox.read_notifications(database.parent, scenario.spec.run_id)
        values = list(doc.get("remote_status", {}).get("bindings", {}).values())
        return bool(values and values[0].get("intent", {}).get("sent") and not values[0].get("claim"))
    try:
        wait_until(unresolved, timeout=5)
        reads = api.reads
        for _ in range(3):
            read(database, "status")
            observer.observe_committed()
    finally:
        observer.close()
    assert api.reads == reads
    assert [command[1] for command in api.commands] == ["POST"]
    assert not observer.thread.is_alive()
    api.lose = False
    reopened = NativeNotifications(database, database.parent, status_api=api)
    reopened.close()
    assert not reopened.thread.is_alive()
    doc = outbox.read_notifications(database.parent, scenario.spec.run_id)
    entry, = doc["remote_status"]["bindings"].values()
    assert entry["intent"] is None and entry["comment_id"] == 100
    assert [command[1] for command in api.commands] == ["POST"]


@pytest.mark.parametrize("mode", ["read", "write"])
def test_status_transport_cancel_reaps_its_actual_child_without_retry(tmp_path, mode):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    import os
    import signal
    import sys
    from agent_run.adapters.github_commands import run_read_command, run_write_command
    from support.worker_process_timing import wait_until
    log = tmp_path / "command-pid"
    script = "from pathlib import Path;import os,signal;p=Path(" + repr(str(log)) + ");t=p.with_suffix('.new');t.write_text(str(os.getpid()));t.replace(p);signal.pause()"
    cancel = Event()
    authorized = []
    def command():
        args = [sys.executable, "-c", script]
        if mode == "read":
            return run_read_command(args, cancelled=cancel.is_set)
        return run_write_command(args, authorize=lambda: authorized.append("original"), cancelled=cancel.is_set)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(command)
        try:
            wait_until(log.exists, timeout=5)
            pid = int(log.read_text())
        finally:
            cancel.set()
        if mode == "read":
            with pytest.raises(InterruptedError, match="cancelled"):
                future.result(timeout=5)
        else:
            result = future.result(timeout=5)
            assert result.returncode == -signal.SIGKILL  # Observed exit, not an invented network response.
    assert authorized == ([] if mode == "read" else ["original"])
    with pytest.raises(ChildProcessError):
        os.waitpid(pid, os.WNOHANG)


def test_normal_exit_hook_uses_original_handoff_without_claiming_domain_release(tmp_path, monkeypatch):
    import json
    import os
    import select
    import shutil
    import subprocess
    import sys
    from agent_run.model import HostObservation
    from agent_run.runtime.handoff import prepare_handoff
    from agent_run.runtime.package_lease import runner_usage_lease
    from agent_run.runtime.process_identity import _process_start_token
    from agent_run.runtime.execution import _cgroup
    from support.systemd_manager import controlled_manager
    scenario = published(tmp_path, monkeypatch)
    kernel, subject = scenario.kernel, scenario.delivery.key
    kernel.control("stop", "offline-executor-exit")
    kernel.confirm_executor_exit(scenario.execution)
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    credentials = private / "credentials"
    credentials.mkdir(mode=0o700)
    manager_root = tmp_path / "manager"
    manager_root.mkdir()
    manager, _, response, _ = controlled_manager(manager_root)
    lock, release = tmp_path / "runner.lock", tmp_path / "release-child"
    program = "from pathlib import Path;import time;print('ready',flush=True)\nwhile not Path(" + repr(str(release)) + ").exists():time.sleep(.01)"
    command = (sys.executable, "-I", "-c", program)
    request = kernel.admit_host("status-normal-exit", kind="resume", command=command,
                               environment_carrier=private / "environment.json", preflight=lambda: None)
    child = subprocess.Popen(command, stdout=subprocess.PIPE, start_new_session=True)
    handoff = None
    try:
        assert child.stdout and select.select([child.stdout], [], [], 3)[0]
        assert child.stdout.readline() == b"ready\n"
        token = _process_start_token(child.pid)
        assert token
        environment = {**os.environ, "PATH": str(Path(manager.systemctl).parent) + os.pathsep + os.environ["PATH"]}
        output = tmp_path / "normal-status-hook.jsonl"
        with runner_usage_lease(lock) as terminal:
            handoff = prepare_handoff(request, environment, runner_lock=lock, terminal_lease=terminal, ttl_seconds=.5)
            shutil.copyfile(request.environment_carrier, credentials / "agent-run-environment")
            (credentials / "agent-run-environment").chmod(0o600)
            assert kernel.dispatch_host(request)
            evidence = json.dumps(handoff.receipt, sort_keys=True, separators=(",", ":"))
            kernel.observe_host(request, HostObservation("unknown", request.unit_name, evidence=evidence, usage_pending=True))
            kernel.observe_host(request, HostObservation("running", request.unit_name, invocation_id="1" * 32,
                cgroup=_cgroup(child.pid), boot_id=Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
                main_pid=child.pid, start_token=token, evidence=evidence, usage_ready=True))
            state = json.loads(response.read_text())
            state["fields"].update(Id=request.unit_name, Description=request.description, InvocationID="1" * 32, LoadState="loaded")
            response.write_text(json.dumps(state))
            release.write_text("exit")
            assert child.wait(timeout=3) == 0
            assert handoff.process.wait(timeout=3) == 1
            before = kernel.storage.path.read_bytes()
            script = '''
import json,sys
from pathlib import Path
import agent_run.notifications.sender as sender
from agent_run.queries.status_comment import project_status
def status(database,**kwargs):
 body=project_status(database,sys.argv[1]).body
 with Path(sys.argv[2]).open('a') as log:log.write(json.dumps(body)+'\\n')
sender.notify_committed=status
from agent_run.cli.host_exit import main
raise SystemExit(main(sys.argv[3:]))
'''
            args = [sys.executable, "-c", script, subject, str(output), "--database", str(kernel.storage.path),
                    "--action", "status-normal-exit", "--runner-lock", str(lock)]
            hook_environment = {**environment, "CREDENTIALS_DIRECTORY": str(credentials),
                                "SERVICE_RESULT": "success", "EXIT_CODE": "exited", "EXIT_STATUS": "0", "INVOCATION_ID": "1" * 32}
            hook = subprocess.run(args, env=hook_environment, capture_output=True, text=True, timeout=10)
            assert hook.returncode == 0, hook.stderr
            body, = [json.loads(line) for line in output.read_text().splitlines()]
            assert "Host exit: exit_unproven" in body  # Main return is not whole-domain release.
            wrong = subprocess.run(args, env={**hook_environment, "INVOCATION_ID": "2" * 32},
                                   capture_output=True, text=True, timeout=10)
            assert wrong.returncode == 0, wrong.stderr
            assert len(output.read_text().splitlines()) == 1
            assert kernel.storage.path.read_bytes() == before
    finally:
        if child.poll() is None:
            child.kill(); child.wait(timeout=3)
        if child.stdout:
            child.stdout.close()
        if handoff is not None and handoff.process.poll() is None:
            handoff.process.terminate(); handoff.process.wait(timeout=3)


def test_status_repository_case_uses_one_identity_and_still_rejects_foreign_objects(tmp_path, monkeypatch):
    from copy import deepcopy
    from agent_run.adapters.remote import RemoteEffects
    from support.offline_api import SyntheticGitHub
    def spelling(value):
        if isinstance(value, dict):
            return {key: item.upper() if key == "full_name" and isinstance(item, str) else spelling(item)
                    for key, item in value.items()}
        return [spelling(item) for item in value] if isinstance(value, list) else value
    original_object, original_list = SyntheticGitHub.object, SyntheticGitHub.list
    monkeypatch.setattr(SyntheticGitHub, "object", lambda self, endpoint: spelling(original_object(self, endpoint)))
    monkeypatch.setattr(SyntheticGitHub, "list", lambda self, endpoint: spelling(original_list(self, endpoint)))
    scenario = published(tmp_path, monkeypatch)
    database, subject = scenario.kernel.storage.path, scenario.delivery.key
    projection = project_status(database, subject)  # Actual normal publication receipt has uppercase full_name.
    api = CommentsAPI(projection.target)
    remote_object, remote_list = api.object, api.list
    api.object = lambda endpoint: spelling(remote_object(endpoint))
    def comments(endpoint):
        rows = deepcopy(remote_list(endpoint))
        for row in rows:
            row["issue_url"] = row["issue_url"].replace(projection.target.repository,
                                                        projection.target.repository.upper())
        return rows
    api.list = comments
    before = database.read_bytes()
    sender = StatusComments(database)
    assert sender.deliver(subject, api=api) == "confirmed"
    assert database.read_bytes() == before
    scenario.kernel.control("stop", "case-insensitive-status-stop", subject=subject)
    intent = sender.stage(subject)
    binding = scenario.kernel.operation_binding()
    remote = RemoteEffects(binding.repository_root, binding.repository,
                           runner_identity=binding.inputs.runner_identity, api=api)
    marker = intent["body"].split("\n", 1)[0]
    assert remote.read_status_comment(projection.target, marker, intent, lambda: False)["disposition"] == "prior"
    for invalid in ("foreign/repository", "example/other", None, 17):
        def foreign(endpoint, invalid=invalid):
            row = spelling(remote_object(endpoint))
            if endpoint != "user":
                row["base"]["repo"]["full_name"] = invalid
            return row
        with monkeypatch.context() as patch:
            patch.setattr(api, "object", foreign)
            assert remote.read_status_comment(projection.target, marker, intent, lambda: False)["disposition"] == "third"
    assert [command[1] for command in api.commands] == ["POST"]
    assert sender.deliver(subject, api=api) == "confirmed"
    assert [command[1] for command in api.commands] == ["POST", "PATCH"]


def test_pending_important_card_keeps_its_budget_before_slow_status_and_status_recovers(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import time
    import agent_run.notifications.sender as notifications
    import agent_run.notifications.status_comment as status_module
    from agent_run.storage.notification_outbox import DELIVERY_LIFETIME_SECONDS

    scenario = published(tmp_path, monkeypatch, notifications={"enabled": True, "mode": "concise",
                         "profile": "fixture", "open_id": "ou_fixture", "app_id": "app_fixture"})
    database, subject = scenario.kernel.storage.path, scenario.delivery.key
    scenario.kernel.control("stop", "important-card-before-status", subject=subject)
    # The public Status producer first creates this journal and immutable intent;
    # no fabricated native receipt or foreign-journal adoption is involved.
    StatusComments(database).stage(subject)
    clock = [time.time()]
    # Replace only these module clocks before starting their actual worker;
    # other Python threads and the product's Core clock are unchanged.
    monkeypatch.setattr(notifications, "time", SimpleNamespace(time=lambda: clock[0]))
    monkeypatch.setattr(status_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    api = CommentsAPI(project_status(database, subject).target)
    original_object = api.object
    delayed = []
    sent = []
    def delayed_object(endpoint):
        if not delayed:
            delayed.append(endpoint)
            clock[0] += DELIVERY_LIFETIME_SECONDS + 1  # One expired Status opportunity, without wall-clock sleep.
        return original_object(endpoint)
    api.object = delayed_object
    def transport(_config, rendered, *_args, **_kwargs):
        sent.append(rendered)
        return {"outcome": "success", "reason": None}
    observer = notifications.NativeNotifications(database, database.parent, transport=transport, status_api=api)
    try:
        observer.close()
        document = outbox.read_notifications(database.parent, scenario.spec.run_id)
        important = [row for row in document["records"] if row.get("important") and not row.get("cancelled")]
        assert important and all(row["outcome"] == "success" and row["attempts"] == 1 for row in important)
        assert all(row["deadline_at"] - row["created_at"] == DELIVERY_LIFETIME_SECONDS for row in important)
        assert sent and delayed and not observer.thread.is_alive()
    finally:
        observer.close(drain=False)
    # The timeout did not disable Status or turn an unsent request into sent.
    api.object = original_object
    recovered = notifications.NativeNotifications(database, database.parent, transport=transport, status_api=api)
    try:
        recovered.close()
        assert not recovered.thread.is_alive()
        assert [row[1] for row in api.commands] == ["POST"]
        assert "operator_stopped" in api.comments[0]["body"]
    finally:
        recovered.close(drain=False)


def test_fresh_enabled_sender_binds_its_own_journal_before_status(tmp_path, monkeypatch):
    from agent_run.notifications.sender import NativeNotifications
    scenario = published(tmp_path, monkeypatch, notifications={"enabled": True, "mode": "concise",
                         "profile": "fixture", "open_id": "ou_fixture", "app_id": "app_fixture"})
    database, subject = scenario.kernel.storage.path, scenario.delivery.key
    before = database.read_bytes()
    assert not outbox.notification_path(database.parent, scenario.spec.run_id).exists()
    api = CommentsAPI(project_status(database, subject).target)
    def transport(*_args, **_kwargs):
        return {"outcome": "success", "reason": None}
    observer = NativeNotifications(database, database.parent, status_api=api, transport=transport)
    try:
        observer.close()
        document = outbox.read_notifications(database.parent, scenario.spec.run_id)
        assert document["native_binding"] == [scenario.spec.run_id, scenario.spec.repository, 1]
        assert [row[1] for row in api.commands] == ["POST"]
        assert not observer.thread.is_alive()
        assert database.read_bytes() == before
    finally:
        observer.close(drain=False)


def test_new_important_card_preempts_actual_status_read_and_same_sender_continues(tmp_path, monkeypatch):
    import json
    import os
    import sys
    from threading import Event
    from agent_run.notifications.sender import NativeNotifications
    from support.worker_process_timing import wait_until
    scenario = published(tmp_path, monkeypatch, notifications={"enabled": True, "mode": "concise",
                         "profile": "fixture", "open_id": "ou_fixture", "app_id": "app_fixture"})
    database, subject = scenario.kernel.storage.path, scenario.delivery.key
    target = project_status(database, subject).target
    StatusComments(database).stage(subject)
    state, calls, blocked = tmp_path / "remote.json", tmp_path / "requests.jsonl", tmp_path / "read-pid"
    state.write_text(json.dumps({"comments": []}))
    binary = tmp_path / "status-cli"
    binary.mkdir()
    program = '''import json,os,signal,sys
from pathlib import Path
state,calls,blocked=map(Path,sys.argv[1:4]);args=sys.argv[4:]
assert args[0]=='api'
endpoint=args[1];method=args[args.index('--method')+1] if '--method' in args else 'GET'
with calls.open('a') as log:log.write(json.dumps([method,endpoint])+'\\n')
if endpoint=='user' and not blocked.exists():
 temporary=blocked.with_suffix('.new');temporary.write_text(str(os.getpid()));temporary.replace(blocked)
 signal.pause()
document=json.loads(state.read_text())
if endpoint=='user':result={'login':'offline-publisher'}
elif endpoint==REPO+'/pulls/'+str(NUMBER):
 result=PR
elif endpoint==REPO+'/issues/'+str(NUMBER)+'/comments?per_page=100' and method=='GET':
 result=[document['comments']]
elif endpoint==REPO+'/issues/'+str(NUMBER)+'/comments' and method=='POST':
 assert document['comments']==[]
 body=json.loads(Path(args[args.index('--input')+1]).read_text())
 result={'id':100,'body':body['body'],'user':{'login':'offline-publisher'},'issue_url':'https://api.github.com/'+REPO+'/issues/'+str(NUMBER)}
 document['comments'].append(result);temporary=state.with_suffix('.new');temporary.write_text(json.dumps(document));temporary.replace(state)
else:raise AssertionError('Unexpected synthetic endpoint')
print(json.dumps(result))
'''
    pr = {"number": target.pr_number, "user": {"login": target.actor},
          "head": {"ref": target.source_ref.removeprefix("refs/heads/"), "repo": {"full_name": target.repository}},
          "base": {"ref": target.target_ref.removeprefix("refs/heads/"), "repo": {"full_name": target.repository}}}
    executable = binary / "gh"
    executable.write_text("#!" + sys.executable + "\nimport sys\nsys.argv[1:1]=" + repr([str(state), str(calls), str(blocked)]) +
                          "\nREPO=" + repr("repos/" + target.repository) + "\nNUMBER=" + repr(target.pr_number) + "\nPR=" + repr(pr) + "\n" + program)
    executable.chmod(0o700)
    monkeypatch.setenv("PATH", str(binary) + os.pathsep + os.environ["PATH"])
    stopped, delivered = Event(), Event()
    def transport(*_args, **_kwargs):
        if stopped.is_set():
            delivered.set()
        return {"outcome": "success", "reason": None}
    observer = NativeNotifications(database, database.parent, transport=transport)
    try:
        wait_until(blocked.exists, timeout=5)
        pid = int(blocked.read_text())
        scenario.kernel.control("stop", "important-during-native-read", subject=subject)
        stopped.set()
        observer.observe_committed()
        assert delivered.wait(5), "Important card waited behind an active Status child"
        wait_until(lambda: len(json.loads(state.read_text())["comments"]) == 1, timeout=5)
        observer.close()
        assert not observer.thread.is_alive()
        rows = [json.loads(line) for line in calls.read_text().splitlines()]
        assert rows.count(["POST", f"repos/{target.repository}/issues/{target.pr_number}/comments"]) == 1
        assert "operator_stopped" in json.loads(state.read_text())["comments"][0]["body"]
        with pytest.raises(ChildProcessError):
            os.waitpid(pid, os.WNOHANG)
    finally:
        observer.close(drain=False)




def test_preempted_unknown_post_keeps_original_intent_and_never_creates_twice(tmp_path, monkeypatch):
    from threading import Event
    from agent_run.notifications.sender import NativeNotifications
    from support.worker_process_timing import wait_until
    scenario = published(tmp_path, monkeypatch, notifications={"enabled": True, "mode": "concise",
                         "profile": "fixture", "open_id": "ou_fixture", "app_id": "app_fixture"})
    database, subject = scenario.kernel.storage.path, scenario.delivery.key
    api = CommentsAPI(project_status(database, subject).target)
    posted, release, stopped, delivered = Event(), Event(), Event(), Event()
    real_write = api.write
    def lost_response(endpoint, method, body):
        result = real_write(endpoint, method, body)
        if method == "POST":
            posted.set()
            assert release.wait(5)
            raise OSError("Synthetic lost response after actual comment creation")
        return result
    api.write = lost_response
    def transport(*_args, **_kwargs):
        if stopped.is_set():
            delivered.set()
        return {"outcome": "success", "reason": None}
    observer = NativeNotifications(database, database.parent, transport=transport, status_api=api)
    try:
        assert posted.wait(5)
        document = outbox.read_notifications(database.parent, scenario.spec.run_id)
        entry, = document["remote_status"]["bindings"].values()
        original = dict(entry["intent"])
        assert original["sent"] is True and "operator_stopped" not in original["body"]
        scenario.kernel.control("stop", "important-after-physical-post", subject=subject)
        before = database.read_bytes()
        stopped.set()
        observer.observe_committed()
        unresolved, = outbox.read_notifications(database.parent, scenario.spec.run_id)["remote_status"]["bindings"].values()
        assert unresolved["intent"] == original
        release.set()
        assert delivered.wait(5)
        wait_until(lambda: "operator_stopped" in api.comments[0]["body"], timeout=5)
        observer.close()
        assert not observer.thread.is_alive()
        assert [command[1] for command in api.commands] == ["POST", "PATCH"]
        assert len(api.comments) == 1 and database.read_bytes() == before
        final, = outbox.read_notifications(database.parent, scenario.spec.run_id)["remote_status"]["bindings"].values()
        assert final["intent"] is None and final["comment_id"] == 100 and "claim" not in final
    finally:
        release.set()
        observer.close(drain=False)
