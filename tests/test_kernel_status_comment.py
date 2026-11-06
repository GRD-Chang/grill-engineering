"""A Status comment is a recoverable projection, never delivery authority."""
from pathlib import Path
from dataclasses import replace
from typing import Any

import pytest

from agent_run.core.kernel import Kernel
from agent_run.model import DeliveryKind
from agent_run.queries.status_comment import project_status
from agent_run.notifications.status_comment import StatusComments
from agent_run.storage import notification_outbox as outbox
from support.offline import Scenario
from support.status_comments import CommentsAPI, core_status_comments


def published(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, notifications=None, kind=DeliveryKind.PARENT_ONLY, runner_identity=None) -> Scenario:
    # The fixture uses the real atomic Task/Run producer in one repository DB.
    from support import offline
    original = offline.create_kernel
    def create(path: Path, spec: Any, **kwargs: Any) -> Kernel:
        if notifications is not None or runner_identity is not None:
            from dataclasses import replace
            assert spec.inputs is not None
            values = {}
            if notifications is not None:
                values["notifications"] = notifications
            if runner_identity is not None:
                values["runner_identity"] = runner_identity
            spec = replace(spec, inputs=replace(spec.inputs, **values))
        return original(path, spec, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(offline, "create_kernel", create)
        scenario = Scenario(tmp_path / "run", kind)
    assert scenario.run().kind == ("complete" if kind == DeliveryKind.TICKET else "approval")
    return scenario


def sender_inputs(tmp_path, monkeypatch, *, enabled=False):
    """Native Run/event producer plus declared display input, without a PR journey."""
    import agent_run.notifications.sender as notifications
    from support.notification import _kernel
    from support.status_comments import display_inputs

    kernel = _kernel(tmp_path / "sender", notifications=enabled)
    delivery = kernel.delivery("native-ticket")
    projection, _ = display_inputs(kernel.storage.path.parent)
    target = replace(projection.target, run_id=kernel.storage.run_id, subject=delivery.key,
        issue=delivery.issue, source_ref=delivery.source_ref, target_ref=delivery.target_ref)
    current = [replace(projection, target=target)]
    def read(database, subject, **kwargs):
        assert database == kernel.storage.path and subject == target.subject
        return current[0]
    monkeypatch.setattr(notifications, "project_status", read)
    monkeypatch.setattr(notifications, "status_subjects", lambda *_args, **_kwargs: [target.subject])
    sender = StatusComments(kernel.storage.path.parent,
        read_projection=lambda subject: read(kernel.storage.path, subject), read_binding=kernel.operation_binding)
    return kernel, current, sender


def test_status_projection_and_intent_are_readonly_and_frozen(tmp_path, monkeypatch):
    # Exercise mixed-case publication receipts within the existing real path.
    from support.offline_api import SyntheticGitHub
    def spelling(value):
        if isinstance(value, dict):
            return {key: item.upper() if key == "full_name" and isinstance(item, str) else spelling(item)
                    for key, item in value.items()}
        return [spelling(item) for item in value] if isinstance(value, list) else value
    original_object, original_list = SyntheticGitHub.object, SyntheticGitHub.list
    monkeypatch.setattr(SyntheticGitHub, "object", lambda self, endpoint: spelling(original_object(self, endpoint)))
    monkeypatch.setattr(SyntheticGitHub, "list", lambda self, endpoint: spelling(original_list(self, endpoint)))
    scenario = published(tmp_path, monkeypatch, kind=DeliveryKind.PARENT_ONLY)
    assert_stale_and_unknown_review_records_are_readonly(scenario.kernel, scenario.delivery.key)
    database, subject = scenario.kernel.storage.path, scenario.delivery.key
    before = database.read_bytes()
    projection = project_status(database, subject, storage=scenario.kernel.storage)
    assert projection.target.pr_number == scenario.kernel.status(subject)["pr_number"]
    assert projection.target.issue == 1
    assert projection.target.parent_issue == 1
    assert scenario.kernel.operation_binding().inputs.notifications["enabled"] is False
    assert projection.target.actor == "offline-publisher"
    assert "Agent Run Status" in projection.body
    assert "Recorded Review: pass" in projection.body
    assert "Delivery: approval" in projection.body
    assert "not_recorded" in projection.body  # This offline producer never launched a formal Host.
    sender = core_status_comments(scenario.kernel)
    intent = sender.stage(subject)
    assert intent is not None
    assert intent["body"].endswith(projection.body)
    assert intent["sent"] is False
    assert sender.stage(subject) == intent
    assert database.read_bytes() == before
    from agent_run.contracts.task import TaskKey
    from agent_run.storage.tasks import task_path
    location = task_path(TaskKey(Path(scenario.spec.repository_root), scenario.spec.repository, 1))
    location.parent.mkdir(parents=True, exist_ok=True)
    location.write_text('{"run_id":"other","database":"/tmp/other.sqlite3"}')
    assert project_status(database, subject, storage=scenario.kernel.storage) == projection
    assert database.read_bytes() == before
    document = outbox.read_notifications(database.parent, scenario.spec.run_id)
    assert document["remote_status"]["version"] == 1
    assert len(document["remote_status"]["bindings"]) == 1
    assert document["pending"] == []  # No business effect or second notification event.
    database.write_bytes(b"damaged sqlite")
    api = CommentsAPI(projection.target)
    assert sender.deliver(subject, api=api) == "unavailable"
    assert api.reads == 0 and api.commands == []



def test_status_concurrent_sender_and_final_stop_cancel_before_post(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    kernel, current, sender = sender_inputs(tmp_path, monkeypatch, enabled=False)
    database, subject = kernel.storage.path, current[0].target.subject
    api = CommentsAPI(current[0].target)
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
            assert StatusComments(database.parent, read_projection=lambda _: current[0],
                read_binding=kernel.operation_binding).deliver(subject, api=api) == "busy"
            kernel.control("stop", "during-status-boundary", subject=subject)
            current[0] = replace(current[0], body="operator_stopped")
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
    binding = kernel.operation_binding()
    physical = RemoteEffects(binding.repository_root, binding.repository, runner_identity=binding.inputs.runner_identity)
    projection = current[0]
    intent = {"comment_id": None, "before": None, "body": "unused"}
    with pytest.raises(ValueError, match="cancelled"):
        physical.read_status_comment(projection.target, "unused", intent, log.exists)
    assert log.read_text() == "x"


def test_enabled_sender_binds_fresh_and_preexisting_status_journals(tmp_path, monkeypatch):
    from agent_run.notifications.sender import NativeNotifications
    kernel, current, sender = sender_inputs(tmp_path, monkeypatch, enabled=True)
    database, subject = kernel.storage.path, current[0].target.subject
    before = database.read_bytes()
    journal = outbox.notification_path(database.parent, kernel.storage.run_id)
    # Two independent journals share the same native Run and declared projection.
    # The previous sender is fully stopped before its journal is removed.
    for preexisting_status in (False, True):
        assert not journal.exists()
        sent = []
        def transport(*args, **kwargs):
            sent.append(args[2])
            return {"outcome": "success", "reason": None}
        api = CommentsAPI(current[0].target)
        if preexisting_status:
            sender.stage(subject)
            assert sender.deliver(subject, api=api) == "confirmed"
        observer = NativeNotifications(database, database.parent, transport=transport, status_api=api,
            storage=kernel.storage)
        try:
            observer.close()
            assert not observer.thread.is_alive() and observer.failure_reason is None
            assert sent
            assert [command[1] for command in api.commands] == ["POST"]
            document = outbox.read_notifications(database.parent, kernel.storage.run_id)
            assert document["native_binding"] == [kernel.storage.run_id, kernel.operation_binding().repository, 1]
            assert document["native_run_start"] is True and document["pending"] == []
            assert len(document["remote_status"]["bindings"]) == 1
            assert database.read_bytes() == before
        finally:
            observer.close(drain=False)
        journal.unlink()


def test_automatic_unknown_status_is_not_polled_by_clock_or_passive_queries(tmp_path, monkeypatch):
    from agent_run.notifications.sender import NativeNotifications
    from agent_run.queries.run import read
    from support.worker_process_timing import wait_until
    kernel, current, sender = sender_inputs(tmp_path, monkeypatch, enabled=False)
    database, subject = kernel.storage.path, current[0].target.subject
    api = CommentsAPI(current[0].target)
    def forbidden(*args, **kwargs):
        pytest.fail("Feishu was disabled in the frozen Run")
    api.lose = True
    observer = NativeNotifications(database, database.parent, transport=forbidden, status_api=api, storage=kernel.storage)
    def unresolved():
        doc = outbox.read_notifications(database.parent, kernel.storage.run_id)
        values = list(doc.get("remote_status", {}).get("bindings", {}).values())
        return bool(values and values[0].get("intent", {}).get("sent") and not values[0].get("claim"))
    try:
        wait_until(unresolved, timeout=5)
        reads = api.reads
        for _ in range(3):
            read(database, "status", expected_run=kernel.storage.run_id, storage=kernel.storage)
            observer.observe_committed()
    finally:
        observer.close()
    assert api.reads == reads
    assert [command[1] for command in api.commands] == ["POST"]
    assert not observer.thread.is_alive()
    api.lose = False
    reopened = NativeNotifications(database, database.parent, transport=forbidden, status_api=api, storage=kernel.storage)
    def recovered():
        doc = outbox.read_notifications(database.parent, kernel.storage.run_id)
        entry, = doc["remote_status"]["bindings"].values()
        return entry["intent"] is None and entry["comment_id"] == 100
    try:
        wait_until(recovered, timeout=5)
        assert [command[1] for command in api.commands] == ["POST"]
        kernel.control("stop", "automatic-status-stop", subject=subject)
        current[0] = replace(current[0], body="operator_stopped")
        reopened.observe_committed()
        wait_until(lambda: len(api.commands) == 2, timeout=5)
        assert "operator_stopped" in api.comments[0]["body"]
    finally:
        reopened.close()
    assert not reopened.thread.is_alive()
    assert [command[1] for command in api.commands] == ["POST", "PATCH"]
    assert len(api.comments) == 1
    document = outbox.read_notifications(database.parent, kernel.storage.run_id)
    assert document["pending"] == [] and document["records"] == []


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
 body=project_status(database,sys.argv[1],**kwargs).body
 with Path(sys.argv[2]).open('a') as log:log.write(json.dumps(body)+'\\n')
sender.notify_committed=status
from agent_run.cli.host_exit import main
raise SystemExit(main(sys.argv[3:]))
'''
            args = [sys.executable, "-c", script, subject, str(output), "--database", str(kernel.storage.path),
                    "--action", "status-normal-exit", "--runner-lock", str(lock), "--run-id=" + kernel.storage.run_id]
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


def test_pending_important_card_keeps_its_budget_before_slow_status_and_status_recovers(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import time
    import agent_run.notifications.sender as notifications
    import agent_run.notifications.status_comment as status_module
    from agent_run.storage.notification_outbox import DELIVERY_LIFETIME_SECONDS

    kernel, current, sender = sender_inputs(tmp_path, monkeypatch, enabled=True)
    database, subject = kernel.storage.path, current[0].target.subject
    kernel.control("stop", "important-card-before-status", subject=subject)
    current[0] = replace(current[0], body="operator_stopped")
    # The real Status consumer creates this journal and immutable intent;
    # the native Run still produces the important stop notification.
    sender.stage(subject)
    clock = [time.time()]
    # Replace only these module clocks before starting their actual worker;
    # other Python threads and the product's Core clock are unchanged.
    monkeypatch.setattr(notifications, "time", SimpleNamespace(time=lambda: clock[0]))
    monkeypatch.setattr(status_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    api = CommentsAPI(current[0].target)
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
    observer = notifications.NativeNotifications(database, database.parent, transport=transport, status_api=api, storage=kernel.storage)
    try:
        observer.close()
        document = outbox.read_notifications(database.parent, kernel.storage.run_id)
        important = [row for row in document["records"] if row.get("important") and not row.get("cancelled")]
        assert important and all(row["outcome"] == "success" and row["attempts"] == 1 for row in important)
        assert all(row["deadline_at"] - row["created_at"] == DELIVERY_LIFETIME_SECONDS for row in important)
        assert sent and delayed and not observer.thread.is_alive()
    finally:
        observer.close(drain=False)
    # The timeout did not disable Status or turn an unsent request into sent.
    api.object = original_object
    recovered = notifications.NativeNotifications(database, database.parent, transport=transport, status_api=api, storage=kernel.storage)
    try:
        recovered.close()
        assert not recovered.thread.is_alive()
        assert [row[1] for row in api.commands] == ["POST"]
        assert "operator_stopped" in api.comments[0]["body"]
    finally:
        recovered.close(drain=False)



def test_new_important_card_preempts_actual_status_read_and_same_sender_continues(tmp_path, monkeypatch):
    import json
    import os
    import sys
    from threading import Event
    from agent_run.notifications.sender import NativeNotifications
    from support.worker_process_timing import wait_until
    kernel, current, sender = sender_inputs(tmp_path, monkeypatch, enabled=True)
    database, subject = kernel.storage.path, current[0].target.subject
    target = current[0].target
    sender.stage(subject)
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
if endpoint in {'user','graphql'} and not blocked.exists():
 temporary=blocked.with_suffix('.new');temporary.write_text(str(os.getpid()));temporary.replace(blocked)
 signal.pause()
document=json.loads(state.read_text())
if endpoint=='graphql':
 assert '--paginate' in args and '--slurp' in args
 comments=[{'fullDatabaseId':str(row['id']),'body':row['body'],'author':{'__typename':'User',**row['user']},
            'repository':{'nameWithOwner':REPO.removeprefix('repos/')},
            'pullRequest':{'number':NUMBER,'repository':{'nameWithOwner':REPO.removeprefix('repos/')}}}
           for row in document['comments']]
 pull={'number':NUMBER,'author':{'__typename':'User',**PR['user']},'headRefName':PR['head']['ref'],'baseRefName':PR['base']['ref'],
       'headRepository':{'nameWithOwner':PR['head']['repo']['full_name']},
       'baseRepository':{'nameWithOwner':PR['base']['repo']['full_name']},
       'comments':{'totalCount':len(comments),'nodes':comments,
                   'pageInfo':{'hasNextPage':False,'endCursor':'1' if comments else None}}}
 result=[{'data':{'viewer':{'login':'offline-publisher'},
          'repository':{'nameWithOwner':REPO.removeprefix('repos/'),'pullRequest':pull}}}]
elif endpoint=='user':result={'login':'offline-publisher'}
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
    observer = NativeNotifications(database, database.parent, transport=transport, storage=kernel.storage)
    try:
        wait_until(blocked.exists, timeout=5)
        pid = int(blocked.read_text())
        kernel.control("stop", "important-during-native-read", subject=subject)
        current[0] = replace(current[0], body="operator_stopped")
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
    kernel, current, sender = sender_inputs(tmp_path, monkeypatch, enabled=True)
    database, subject = kernel.storage.path, current[0].target.subject
    api = CommentsAPI(current[0].target)
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
    observer = NativeNotifications(database, database.parent, transport=transport, status_api=api, storage=kernel.storage)
    try:
        assert posted.wait(5)
        document = outbox.read_notifications(database.parent, kernel.storage.run_id)
        entry, = document["remote_status"]["bindings"].values()
        original = dict(entry["intent"])
        assert original["sent"] is True and "operator_stopped" not in original["body"]
        kernel.control("stop", "important-after-physical-post", subject=subject)
        current[0] = replace(current[0], body="operator_stopped")
        before = database.read_bytes()
        stopped.set()
        observer.observe_committed()
        unresolved, = outbox.read_notifications(database.parent, kernel.storage.run_id)["remote_status"]["bindings"].values()
        assert unresolved["intent"] == original
        release.set()
        assert delivered.wait(5)
        wait_until(lambda: "operator_stopped" in api.comments[0]["body"], timeout=5)
        observer.close()
        assert not observer.thread.is_alive()
        assert [command[1] for command in api.commands] == ["POST", "PATCH"]
        assert len(api.comments) == 1 and database.read_bytes() == before
        final, = outbox.read_notifications(database.parent, kernel.storage.run_id)["remote_status"]["bindings"].values()
        assert final["intent"] is None and final["comment_id"] == 100 and "claim" not in final
    finally:
        release.set()
        observer.close(drain=False)

def test_attach_updates_same_status_only_after_original_domain_and_usage_release(tmp_path, monkeypatch):
    import json
    import os
    import subprocess
    import sys
    import agent_run.notifications.sender as notifications
    from agent_run.cli.lifecycle import attach
    from agent_run.model import HostObservation
    from agent_run.runtime.handoff import prepare_handoff
    from agent_run.runtime.package_lease import runner_usage_lease
    from support.systemd_manager import controlled_manager
    scenario = published(tmp_path, monkeypatch)
    kernel, subject = scenario.kernel, scenario.delivery.key
    kernel.control("stop", "retire-offline-for-managed-release")
    kernel.confirm_executor_exit(scenario.execution)
    kernel.control("resume", "original-managed-release-resume")
    manager_root = tmp_path / "release-manager"
    manager_root.mkdir()
    manager, _, response, _ = controlled_manager(manager_root)
    private = tmp_path / "release-private"
    private.mkdir(mode=0o700)
    lock = tmp_path / "runner-release.lock"
    command = (sys.executable, "-I", "-c", "raise SystemExit(0)")
    request = kernel.admit_host("original-status-release", kind="run", command=command,
                               environment_carrier=private / "environment.json", preflight=manager.preflight)
    api = CommentsAPI(project_status(kernel.storage.path, subject, storage=kernel.storage).target)
    original_sender = notifications.NativeNotifications
    monkeypatch.setattr(notifications, "NativeNotifications",
                        lambda *args, **kwargs: original_sender(*args, **kwargs, status_api=api))
    handoff = None
    try:
        with runner_usage_lease(lock) as terminal:
            handoff = prepare_handoff(request, {"PATH": os.defpath}, runner_lock=lock, terminal_lease=terminal, ttl_seconds=.5)
            assert kernel.dispatch_host(request)
            receipt = json.dumps(handoff.receipt, sort_keys=True, separators=(",", ":"))
            assert kernel.observe_host(request, HostObservation("unknown", request.unit_name,
                                        usage_pending=True, evidence=receipt)) == "unknown"
            assert core_status_comments(kernel).deliver(subject, api=api) == "confirmed"
            comment_id = api.comments[0]["id"]
            assert "Host exit: exit_unproven" in api.comments[0]["body"]
            child = subprocess.Popen(command, start_new_session=True)
            assert child.wait(timeout=3) == 0
            assert handoff.process.wait(timeout=3) == 1
            state = json.loads(response.read_text())
            # Existing external-manager/sysfs seam is synthetic. The local
            # child and original guardian really exited; this is not live
            # systemd/cgroup validation in the container's shared cgroup.
            state["fields"].update(Id=request.unit_name, Description=request.description,
                ActiveState="inactive", SubState="dead", ExecMainStatus="0", NFileDescriptorStore="1")
            response.write_text(json.dumps(state))
            group_events = manager.cgroup_root / "unit-group" / "cgroup.events"
            group_events.write_text("populated 1\nfrozen 0\n")
            original = attach(kernel, requested_action="resume", manager=manager,
                              environment={"PATH": os.defpath}, runner_lock=lock)
            assert original["host"] == "unknown" and kernel.host_usage("original-status-release").usage_released is False
            assert [row[1] for row in api.commands] == ["POST"]
            group_events.write_text("populated 0\nfrozen 0\n")
            held = attach(kernel, requested_action="resume", manager=manager,
                          environment={"PATH": os.defpath}, runner_lock=lock)
            assert held["host"] == "exited" and kernel.host_usage("original-status-release").usage_released is False
            assert "Host exit: exit_unproven" in project_status(kernel.storage.path, subject, storage=kernel.storage).body
            assert [row[1] for row in api.commands] == ["POST"]
            state["fields"]["NFileDescriptorStore"] = "0"
            response.write_text(json.dumps(state))
            released = attach(kernel, requested_action="resume", manager=manager,
                              environment={"PATH": os.defpath}, runner_lock=lock)
            assert released["host"] == "exited" and released["requested_action_applied"] is False
            assert kernel.host_usage("original-status-release").usage_released is True
            assert [row[1] for row in api.commands] == ["POST", "PATCH"]
            assert api.comments[0]["id"] == comment_id and "Host exit: exited" in api.comments[0]["body"]
    finally:
        if handoff is not None and handoff.process.poll() is None:
            handoff.process.terminate(); handoff.process.wait(timeout=3)


def test_terminal_sender_and_status_share_exact_cleanup_guidance(tmp_path, monkeypatch):
    run_id = "a'  b"
    from dataclasses import replace
    import json
    import shlex
    from agent_run.notifications.sender import NativeNotifications
    from support.offline import OfflinePort
    original_prepare = OfflinePort.prepare
    def named_prepare(*args, **kwargs):
        spec, delivery = original_prepare(*args, **kwargs)
        return replace(spec, run_id=run_id), delivery
    with monkeypatch.context() as patch:
        patch.setattr(OfflinePort, "prepare", named_prepare)
        scenario = published(tmp_path, monkeypatch, notifications={"enabled": True, "mode": "concise",
                             "profile": "fixture", "open_id": "ou_fixture", "app_id": "app_fixture"})
    kernel, subject = scenario.kernel, scenario.delivery.key
    assert scenario.spec.run_id == run_id
    assert kernel.control("approve", "original-cleanup-approval", subject=subject, world=scenario.world()) == "pending"
    for _ in range(100):
        if kernel.status(subject)["phase"] == "complete":
            break
        assert scenario.step().kind not in {"paused", "waiting", "approval"}
    else:
        pytest.fail("The original close was not consumed")
    database = kernel.storage.path
    with kernel.storage.transaction(write=False) as tx:
        closure = dict(tx.one("SELECT id,receipt_id FROM effects WHERE operation='close' AND status='confirmed'"))
        assert tx.one("SELECT state FROM resource_cleanup WHERE subject=?", (subject,))[0] != "complete"
    before = database.read_bytes()
    captured = []
    def transport(_config, rendered, *_args, **_kwargs):
        document = outbox.read_notifications(database.parent, run_id)
        active, = [row["event"] for row in document["pending"] if row["outcome"] == "sending"]
        captured.append((active, rendered))
        return {"outcome": "success", "reason": None}
    api = CommentsAPI(project_status(database, subject, storage=kernel.storage).target)
    observer = NativeNotifications(database, database.parent, transport=transport, status_api=api, storage=kernel.storage)
    try:
        observer.close()
        terminal, = [(event, rendered) for event, rendered in captured if event.get("terminal")]
        event, rendered = terminal
        guidance = event["next_step"]
        assert shlex.split(guidance) == ["agent-run", "resume", "--repo", scenario.spec.repository, "--", run_id]
        assert event["parent_only"] is True and event["ticket_progress"] is None
        assert event["completion_actions"] == []
        assert guidance in project_status(database, subject, storage=kernel.storage).body
        assert scenario.spec.repository in json.dumps(rendered, ensure_ascii=False)
        assert [row[1] for row in api.commands] == ["POST"]
        assert database.read_bytes() == before
        with kernel.storage.transaction(write=False) as tx:
            assert dict(tx.one("SELECT id,receipt_id FROM effects WHERE operation='close' AND status='confirmed'")) == closure
        assert not observer.thread.is_alive()
    finally:
        observer.close(drain=False)


def test_actual_sender_keeps_ticket_completion_receipt_summary_and_parent_progress(tmp_path, monkeypatch):
    import json
    from agent_run.notifications.sender import NativeNotifications
    scenario = published(tmp_path, monkeypatch, kind=DeliveryKind.TICKET,
        notifications={"enabled": True, "mode": "concise", "profile": "fixture",
                       "open_id": "ou_fixture", "app_id": "app_fixture"})
    database, subject = scenario.kernel.storage.path, scenario.delivery.key
    with scenario.kernel.storage.transaction(write=False) as tx:
        original = tx.one("SELECT e.receipt_id,c.development_attempt FROM completions c "
                          "JOIN effects e ON e.id=c.close_effect WHERE c.subject=?", (subject,))
        receipt = original["receipt_id"]
    before = database.read_bytes()
    projection = project_status(database, subject, storage=scenario.kernel.storage)
    assert projection.target.pr_number == scenario.kernel.status(subject)["pr_number"]
    assert projection.target.issue == 2 and projection.target.parent_issue == 1
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
        event, rendered = next(item for item in captured if item[0]["kind"] == "ticket_completed")
        assert sum(item[0]["kind"] == "ticket_completed" for item in captured) == 1
        assert event["source_material_id"] == receipt
        assert event["summary"] == "Changed synthetic behavior 1"
        assert event["ticket_progress"] == {"completed": 1, "total": 1} and event["parent_only"] is False
        assert event["status_query"] == "agent-run status --repo example/repository -- offline-run"
        assert event["total_seconds"] is None and event["execution_timing_complete"] is False
        assert "子任务已完成 1/1" in json.dumps(rendered, ensure_ascii=False)
        assert not observer.thread.is_alive() and database.read_bytes() == before
    finally:
        observer.close(drain=False)


def test_status_dirty_journal_fault_keeps_worker_alive_until_explicit_recovery(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from threading import Event, current_thread
    import agent_run.notifications.sender as notifications

    kernel, current, sender = sender_inputs(tmp_path, monkeypatch, enabled=True)
    database, subject = kernel.storage.path, current[0].target.subject
    kernel.control("stop", "important-before-one-journal-fault", subject=subject)
    current[0] = replace(current[0], body="operator_stopped")
    started, release, failed, paused, ended, delivered = (Event() for _ in range(6))
    original_work, original_pause = notifications.NativeNotifications._work, notifications.NativeNotifications._pause
    original_transaction = outbox.transaction
    def held_work(self):
        # The worker owns the initial projection before this claim-fault barrier.
        assert self._observe_committed()
        started.set()
        assert release.wait(5)
        try:
            original_work(self)
        finally:
            ended.set()
    def observed_pause(self, error):
        original_pause(self, error)
        paused.set()
    @contextmanager
    def one_failure(*args, **kwargs):
        if current_thread().name == "kernel-notifications" and not failed.is_set():
            failed.set()
            raise OSError("one controlled journal failure before claim")
        with original_transaction(*args, **kwargs) as document:
            yield document
    sent = []
    def transport(*_args, **_kwargs):
        sent.append(1)
        delivered.set()
        return {"outcome": "success", "reason": None}
    monkeypatch.setattr(notifications.NativeNotifications, "_work", held_work)
    monkeypatch.setattr(notifications.NativeNotifications, "_pause", observed_pause)
    observer = notifications.NativeNotifications(database, database.parent, transport=transport, status_api=CommentsAPI(current[0].target), storage=kernel.storage)
    try:
        assert started.wait(5)
        before = outbox.read_notifications(database.parent, kernel.storage.run_id)
        important = [item for item in before["pending"] if item.get("important")]
        assert important and observer._status_dirty
        deadlines = {item["event"]["id"]: item["deadline_at"] for item in important}
        monkeypatch.setattr(outbox, "transaction", one_failure)
        release.set()
        assert paused.wait(5) and failed.is_set()
        assert not ended.wait(.5), "Unclosed sender died with dirty Status after a single journal fault"
        assert observer.thread.is_alive() and observer._mode == "running" and sent == []
        faulted = outbox.read_notifications(database.parent, kernel.storage.run_id)
        assert all(item["attempts"] == 0 for item in faulted["pending"])
        observer.observe_committed()
        assert delivered.wait(5)
        observer.close()
        assert not observer.thread.is_alive() and observer.failure_reason is None
        final = outbox.read_notifications(database.parent, kernel.storage.run_id)
        for item in final["records"]:
            if item["id"] in deadlines:
                assert item["outcome"] == "success" and item["attempts"] == 1
                assert item["deadline_at"] == deadlines[item["id"]]
    finally:
        release.set()
        observer.close(drain=False)


def test_repeated_wakes_during_projection_keep_new_important_card_ahead_of_status(tmp_path, monkeypatch):
    from threading import Event
    import agent_run.notifications.sender as notifications
    from support.worker_process_timing import wait_until

    kernel, current, sender = sender_inputs(tmp_path, monkeypatch, enabled=True)
    database, subject = kernel.storage.path, current[0].target.subject
    # Leave a real history page to catch up after the blocked older projection.
    # These settled gates are not current cards; only the later stop is important.
    for index in range(9):
        kernel.control("stop", f"historical-stop-{index}", subject=subject)
        kernel.control("resume", f"historical-resume-{index}", subject=subject)
    api = CommentsAPI(current[0].target)
    older_ready, older_release, important_release = (Event() for _ in range(3))
    important_sent = Event()
    ordering = []
    original_write = api.write
    def write(*args):
        ordering.append("status")
        return original_write(*args)
    api.write = write
    def transport(*_args, **_kwargs):
        pending = outbox.read_notifications(database.parent, kernel.storage.run_id)["pending"]
        if older_ready.is_set() and any(item.get("important") and item["outcome"] == "sending" for item in pending):
            ordering.append("important")
            important_sent.set()
            assert important_release.wait(5)
        return {"outcome": "success", "reason": None}
    original_wake = notifications.NativeNotifications._wake_pending
    def reordered_wake(self, document, *args):
        if not older_ready.is_set() and not any(item.get("important") for item in document["pending"]):
            older_ready.set()
            assert older_release.wait(5)
        return original_wake(self, document, *args)
    monkeypatch.setattr(notifications.NativeNotifications, "_wake_pending", reordered_wake)
    observer = notifications.NativeNotifications(database, database.parent, transport=transport, status_api=api, storage=kernel.storage)
    try:
        assert older_ready.wait(5)
        kernel.control("stop", "new-important-after-old-observation", subject=subject)
        current[0] = replace(current[0], body="operator_stopped")
        for _ in range(3):
            observer.observe_committed()
        assert observer._call is None, "A wake alone cannot start physical Status work"
        older_release.set()
        assert important_sent.wait(5)
        pending = outbox.read_notifications(database.parent, kernel.storage.run_id)["pending"]
        assert len([item for item in pending if item.get("important")]) == 1
        assert observer._call is not None and observer._call.kind == "feishu"
        assert ordering == ["important"]
        important_release.set()
        wait_until(lambda: len(api.comments) == 1 and "operator_stopped" in api.comments[0]["body"], timeout=5)
        observer.close()
        assert ordering[0] == "important"
        assert [row[1] for row in api.commands] == ["POST"]
        assert not observer.thread.is_alive()
    finally:
        older_release.set()
        important_release.set()
        observer.close(drain=False)


def test_existing_pr_status_tracks_current_review_failure_repair_and_pass(tmp_path, monkeypatch):
    scenario = published(tmp_path, monkeypatch)
    kernel, subject = scenario.kernel, scenario.delivery.key
    sender = core_status_comments(kernel)
    api = CommentsAPI(project_status(kernel.storage.path, subject, storage=kernel.storage).target)
    assert sender.deliver(subject, api=api) == "confirmed"
    comment_id = api.comments[0]["id"]
    scenario.reviews = ["pass", "fail", "pass"]
    original_agent = scenario.agent

    def agent(call):
        raw = original_agent(call)
        if call.role == "review":
            import json
            value = json.loads(raw)
            for check in value["checks"].values():
                check["evidence"] = "private-artifact-<script>\n```" * 1000
            return json.dumps(value).encode()
        return raw

    monkeypatch.setattr(scenario, "agent", agent)
    scenario.port.external_remote_append(scenario.delivery.target_ref,
        {"new-target.txt": "An independent target change\n"}, "review-status-target")
    assert kernel.control("approve", "review-status-update", subject=subject, world=scenario.world()) == "updating"
    assert "Recorded Review: stale" in project_status(kernel.storage.path, subject, storage=kernel.storage).body
    for _ in range(100):
        scenario.step()
        with kernel.storage.transaction(write=False) as tx:
            job = tx.one("SELECT * FROM subjects WHERE id=?", (subject,))
            review = tx.one("SELECT * FROM attempts WHERE id=?", (job["review_attempt"],)) if job["review_attempt"] else None
        if review is not None and review["outcome"] == "fail":
            break
    else:
        pytest.fail("An existing PR never reached the real failed Review")
    assert job["phase"] == "development" and job["pr_number"] == api.target.pr_number
    before = kernel.storage.path.read_bytes()
    projection = project_status(kernel.storage.path, subject, storage=kernel.storage)
    assert "Recorded Review: request_changes" in projection.body
    assert review["candidate_sha"] in projection.body
    assert kernel.storage.path.read_bytes() == before
    assert sender.deliver(subject, api=api) == "confirmed"
    assert api.comments[0]["id"] == comment_id and "Recorded Review: request_changes" in api.comments[0]["body"]
    # Observe the real next Review before credentials/Invocation admission.
    from support.core_operations import prepare_scripted_agent
    for _ in range(100):
        runner = scenario.runner()
        step = runner.run_once()
        if step.preparation is not None and step.preparation.role == "review":
            break
        step = prepare_scripted_agent(kernel, step)
        if step.call is not None:
            runner.authorize_agent(step.call)
            kernel.receive(step.call, scenario.agent(step.call))
    else:
        pytest.fail("Repair did not prepare a fresh Review")
    assert step.preparation.context["candidate_sha"] != review["candidate_sha"]
    pending = project_status(kernel.storage.path, subject, storage=kernel.storage)
    assert "Recorded Review: pending" in pending.body
    assert "Recorded Review: request_changes" not in pending.body and "Recorded Review: pass" not in pending.body
    call_step = prepare_scripted_agent(kernel, step)
    assert call_step.call is not None
    runner.authorize_agent(call_step.call)
    kernel.receive(call_step.call, scenario.agent(call_step.call))
    assert "Recorded Review: pending" in project_status(kernel.storage.path, subject, storage=kernel.storage).body
    assert scenario.run().kind == "approval"
    current = project_status(kernel.storage.path, subject, storage=kernel.storage)
    assert "Recorded Review: pass" in current.body
    assert "Recorded Review: request_changes" not in current.body
    assert len(current.body.encode()) <= 8192 and "private-artifact" not in current.body
    assert sender.deliver(subject, api=api) == "confirmed"
    assert len(api.comments) == 1 and api.comments[0]["id"] == comment_id
    assert [command[1] for command in api.commands] == ["POST", "PATCH", "PATCH"]
    # A foreign display edit pauses only this comment; actual delivery can finish.
    kernel.control("stop", "display-only-local-stop", subject=subject)
    original_body = api.comments[0]["body"]
    api.comments[0]["body"] += "\nHuman edit"
    assert sender.deliver(subject, api=api) == "foreign"
    reads, writes = api.reads, len(api.commands)
    api.comments[0]["body"] = original_body
    assert sender.deliver(subject, api=api) == "unavailable"
    assert api.reads == reads and len(api.commands) == writes
    kernel.control("resume", "resume-business-only", subject=subject)
    assert scenario.run(approve=True).kind == "complete"


def assert_stale_and_unknown_review_records_are_readonly(kernel, subject):
    """Negative records on the same original PR; restore each before the next query."""
    with kernel.storage.transaction(write=False) as tx:
        job = tx.one("SELECT * FROM subjects WHERE id=?", (subject,))
        review = dict(tx.one("SELECT * FROM attempts WHERE id=?", (job["review_attempt"],)))
    assert "Recorded Review: pass" in project_status(kernel.storage.path, subject, storage=kernel.storage).body
    for key in ("role", "generation", "revision", "parent_revision", "graph_revision", "candidate_sha",
                "candidate_tree", "base_sha", "expected_tree", "completion_revision"):
        corrupt = review[key] + 1 if key == "generation" else "foreign-record"
        with kernel.storage.transaction() as tx:
            tx.execute(f"UPDATE attempts SET {key}=? WHERE id=?", (corrupt, review["id"]))
        try:
            before = kernel.storage.path.read_bytes()
            body = project_status(kernel.storage.path, subject, storage=kernel.storage).body
            assert "Recorded Review: stale" in body, key
            assert "Recorded Review: pass" not in body and "Recorded Review: request_changes" not in body, key
            assert kernel.storage.path.read_bytes() == before
        finally:
            with kernel.storage.transaction() as tx:
                tx.execute(f"UPDATE attempts SET {key}=? WHERE id=?", (review[key], review["id"]))
    for key, invalid in (("outcome", None), ("outcome", "fail"), ("result_id", None), ("status", "returned")):
        with kernel.storage.transaction() as tx:
            tx.execute(f"UPDATE attempts SET {key}=? WHERE id=?", (invalid, review["id"]))
        try:
            body = project_status(kernel.storage.path, subject, storage=kernel.storage).body
            assert "Recorded Review: unknown" in body, (key, invalid)
            assert "Recorded Review: pass" not in body and "Recorded Review: request_changes" not in body
        finally:
            with kernel.storage.transaction() as tx:
                tx.execute(f"UPDATE attempts SET {key}=? WHERE id=?", (review[key], review["id"]))
    with kernel.storage.transaction() as tx:
        tx.execute("UPDATE subjects SET review_attempt=NULL WHERE id=?", (subject,))
    try:
        assert "Recorded Review: not_recorded" in project_status(kernel.storage.path, subject, storage=kernel.storage).body
    finally:
        with kernel.storage.transaction() as tx:
            tx.execute("UPDATE subjects SET review_attempt=? WHERE id=?", (review["id"], subject))
    assert "Recorded Review: pass" in project_status(kernel.storage.path, subject, storage=kernel.storage).body
    with kernel.storage.transaction(write=False) as tx:
        assert not tx.rows("SELECT id FROM effects WHERE operation='merge'")
