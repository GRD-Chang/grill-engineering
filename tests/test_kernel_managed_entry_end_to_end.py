"""Public run through a real local Host/Worker chain and controlled external I/O.

The manager really transfers FDs and waits for the exact Host. It is not a live
user-systemd service; the shared test cgroup cannot prove a released Host epoch.
"""
from __future__ import annotations

import array
from contextlib import ExitStack, contextmanager
import json
import os
from pathlib import Path
from queue import Queue
import select
import signal
import socket
import subprocess
import sys
import threading
import time

import pytest

from agent_run.config.paths import app_data_root
from agent_run.runtime.package_lease import RunnerLeaseBusy, default_runner_lock_path, runner_management_lease
from agent_run.config.defaults import UserDefaultsStore
from agent_run.core.kernel import Kernel
from support.systemd_manager import controlled_manager
from tests.test_kernel_remote_effects import FAKE_GH
from support.worker_process_timing import wait_until


def _git(root: Path, *arguments: str) -> str:
    return subprocess.run(["git", *arguments], cwd=root, check=True, text=True,
                          capture_output=True, timeout=10).stdout.strip()


def _executable(path: Path, program: str) -> None:
    path.write_text("#!" + sys.executable + "\n" + program)
    path.chmod(0o700)


_CODEX = r'''import errno, json, os, subprocess, sys
from pathlib import Path
prompt = sys.stdin.read()
if prompt.startswith('You are the development engineer'):
    role = 'development'
elif prompt.startswith('You are the independent acceptance engineer'):
    role = 'review'
elif prompt.startswith('You are the engineer responsible for describing'):
    role = 'publication'
else:
    raise AssertionError('Unexpected allocated role prompt: ' + prompt[:100])
thread = (sys.argv[sys.argv.index('--dangerously-bypass-approvals-and-sandbox') + 1]
          if sys.argv[1:3] == ['exec', 'resume'] else 'managed-entry-' + role)
head = subprocess.run(['git', 'rev-parse', 'HEAD'], check=True, capture_output=True, text=True).stdout.strip()
with open(os.environ['ENTRY_ROLE_TRACE'], 'a') as log:
    log.write(json.dumps({'role': role, 'thread': thread, 'checkout': str(Path.cwd()), 'head': head}) + '\n')
print(json.dumps({'type': 'thread.started', 'thread_id': thread}), flush=True)
print(json.dumps({'type': 'turn.started'}), flush=True)
content = 'Implementation from the managed native Worker.\n'
if role == 'development':
    Path('managed-delivery.txt').write_text(content)
    result = {'result_kind': 'development', 'summary': 'Implemented the requested file.', 'human_blockers': None}
elif role == 'review':
    assert Path('managed-delivery.txt').read_text() == content
    try:
        Path('managed-delivery.txt').write_text('Forbidden review mutation')
    except OSError as error:
        assert error.errno in (errno.EROFS, errno.EACCES, errno.EPERM)
    else:
        raise AssertionError('Review delivery content was writable')
    temporary = Path(sys.argv[sys.argv.index('--output-last-message') + 1]).parent / 'validation.tmp'
    temporary.write_text('Disposable validation output outside the read-only checkout.\n')
    result = {'checks': {lane: {'status': 'pass', 'evidence': 'Read the exact committed file in the isolated checkout.',
                              'findings': []} for lane in ('e2e', 'standards', 'spec')}}
else:
    assert Path('managed-delivery.txt').read_text() == content
    result = {'result_kind': 'publication', 'commit_message': 'feat: managed native delivery',
              'pr_title': 'feat: managed native delivery', 'pr_body_markdown': 'Implemented and independently inspected the requested file.',
              'human_blockers': None}
Path(sys.argv[sys.argv.index('--output-last-message') + 1]).write_text(json.dumps(result))
print(json.dumps({'type': 'turn.completed'}), flush=True)
'''


@contextmanager
def _local_manager(directory: Path, environment: dict[str, str], *, host_timeout: float = 90):
    """Only replace the external manager CLI; execute its original argv intact."""
    directory.mkdir()
    manager, _, response, calls = controlled_manager(directory)
    launch = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    launch.bind(str(directory / "launch"))
    launch.listen(1)
    launch.settimeout(15)
    notify = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    notify.bind("")  # Short, isolated abstract address, consumed by real SCM_RIGHTS.
    notify.settimeout(15)
    notify_address = "@" + notify.getsockname()[1:].decode()
    _executable(Path(manager.systemd_run), f'''import json, socket, sys
with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
    channel.settimeout(10)
    channel.connect({str(directory / 'launch')!r})
    channel.sendall(json.dumps(sys.argv[1:]).encode())
    channel.shutdown(socket.SHUT_WR)
    assert channel.recv(1) == b'1'
''')
    # Actual systemctl subprocesses must use the sealed terminal bus selection.
    systemctl = Path(manager.systemctl)
    systemctl.write_text(systemctl.read_text().replace(
        "state = json.loads(response.read_text())",
        "import os\nassert os.environ['DBUS_SESSION_BUS_ADDRESS'] == 'unix:path=/fixture-terminal/bus'\n"
        "state = json.loads(response.read_text())",
    ))
    errors: list[BaseException] = []
    host: list[subprocess.Popen[bytes]] = []
    stored: list[int] = []
    trace: list[str] = []
    launched: list[list[str]] = []
    output: list[tuple[bytes, bytes]] = []

    def serve() -> None:
        gate_read = gate_write = -1
        try:
            with launch.accept()[0] as channel:
                data = bytearray()
                while chunk := channel.recv(65536):
                    data.extend(chunk)
                    assert len(data) <= 65536
                arguments = json.loads(data)
                command = arguments[arguments.index("--") + 1:]
                launched.append(command)
                properties = dict(value.removeprefix("--property=").split("=", 1)
                                  for value in arguments if value.startswith("--property="))
                assert properties["FileDescriptorStorePreserve"] == "restart"
                assert properties["RemainAfterExit"] == "no" and properties["Restart"] == "no"
                name, carrier = properties["LoadCredential"].split(":", 1)
                credentials = directory / "credentials"
                credentials.mkdir(mode=0o700)
                original = Path(carrier).read_bytes()
                credential = credentials / name
                credential.write_bytes(original)
                credential.chmod(0o600)
                unit = next(arg.removeprefix("--unit=") for arg in arguments if arg.startswith("--unit="))
                description = next(arg.removeprefix("--description=") for arg in arguments if arg.startswith("--description="))
                gate_read, gate_write = os.pipe()
                # This barrier only registers the actual PID before exec. The
                # exact production command is neither substituted nor patched.
                bootstrap = "import os,sys;os.read(int(sys.argv[1]),1);os.close(int(sys.argv[1]));os.execvpe(sys.argv[2],sys.argv[2:],os.environ)"
                service_environment = {**environment, "CREDENTIALS_DIRECTORY": str(credentials),
                                       "NOTIFY_SOCKET": notify_address,
                                       "DBUS_SESSION_BUS_ADDRESS": "unix:path=/stale-service/bus"}
                child = subprocess.Popen([sys.executable, "-c", bootstrap, str(gate_read), *command],
                                         env=service_environment, pass_fds=(gate_read,), start_new_session=True,
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                host.append(child)
                os.close(gate_read); gate_read = -1
                group = next(line[3:] for line in Path(f"/proc/{child.pid}/cgroup").read_text().splitlines()
                             if line.startswith("0::"))
                state = json.loads(response.read_text())
                state["fields"].update(Id=unit, Description=description, ActiveState="active", SubState="running",
                                       MainPID=str(child.pid), ControlGroup=group, NFileDescriptorStore="0")
                response.write_text(json.dumps(state))
                os.write(gate_write, b"1")
                os.close(gate_write); gate_write = -1
                channel.sendall(b"1")
            for expected in (b"FDSTORE=1", b"BARRIER=1"):
                data, ancillary, _, _ = notify.recvmsg(1024, socket.CMSG_SPACE(array.array("i").itemsize))
                assert data.startswith(expected)
                descriptors = array.array("i")
                for level, kind, value in ancillary:
                    assert (level, kind) == (socket.SOL_SOCKET, socket.SCM_RIGHTS)
                    descriptors.frombytes(value[:len(value) - len(value) % descriptors.itemsize])
                assert len(descriptors) == 1
                if expected == b"FDSTORE=1":
                    stored.append(descriptors[0])
                    state["fields"]["NFileDescriptorStore"] = "1"
                    response.write_text(json.dumps(state))
                    trace.append("fdstore")
                else:
                    os.close(descriptors[0])
                    trace.append("barrier")
            with pytest.raises(RunnerLeaseBusy):
                with runner_management_lease(default_runner_lock_path(environment)):
                    raise AssertionError("Runner management crossed the real Host handoff")
            output.append(child.communicate(timeout=host_timeout))
            (directory / "host.stdout").write_bytes(output[-1][0])
            (directory / "host.stderr").write_bytes(output[-1][1])
            trace.append("host_exited")
            assert child.returncode == 0, output[-1]
            # Model the manager's physical automatic close after its actual
            # child exits. Do not invent an empty cgroup or a Kernel release.
            os.close(stored.pop())
            trace.append("store_closed")
        except BaseException as error:
            errors.append(error)
        finally:
            for descriptor in (gate_read, gate_write):
                if descriptor >= 0:
                    os.close(descriptor)

    serving = threading.Thread(target=serve)
    serving.start()
    try:
        yield manager, serving, errors, host, trace, launched, output
    finally:
        for process in host:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=3)
        launch.close()
        notify.close()
        serving.join(timeout=16)
        for descriptor in stored:
            os.close(descriptor)
        assert not serving.is_alive(), "Controlled manager did not finish cleanup"


def test_public_run_reaches_ticket_close_through_managed_host_after_lost_merge_reply(
    git_repo: Path, tmp_path: Path, monkeypatch,
) -> None:
    import agent_run.cli.lifecycle as lifecycle_cli

    remote = tmp_path / "origin.git"
    _git(git_repo, "init", "--bare", "--initial-branch=main", str(remote))
    _git(remote, "config", "gc.auto", "0")
    _git(remote, "config", "maintenance.auto", "false")
    _git(git_repo, "push", str(remote), "HEAD:refs/heads/main")
    base = _git(git_repo, "rev-parse", "HEAD")
    binary = tmp_path / "entry-bin"
    binary.mkdir()
    state, calls, roles = tmp_path / "github.json", tmp_path / "gh-calls.jsonl", tmp_path / "roles.jsonl"
    state.write_text(json.dumps({"prs": [], "comments": [], "events": [], "issue_state": "open",
        "lose_operation": "merge", "source_graph": {"parent": {"number": 1, "title": "Native Parent",
        "body": "Deliver the ready Ticket; keep the unready sibling untouched.", "sub_issues": [2, 3]}, "issues": {
        "2": {"number": 2, "title": "Managed delivery", "body": "Create managed-delivery.txt with the requested content.",
              "state": "OPEN", "labels": ["ready-for-agent"]},
        "3": {"number": 3, "title": "Not ready", "body": "Do not start without ready-for-agent.",
              "state": "OPEN", "labels": []}}}}))
    # Extend only the external executable's clone/auth boundary; all delivery
    # effects continue through the existing real-Git/stateful-gh fixture.
    gh = r'''import os, sys
if sys.argv[1:3] == ['repo', 'clone']:
    import subprocess
    raise SystemExit(subprocess.run(['git', 'clone', '--no-local', '--quiet', '--', os.environ['REMOTE_TEST_BARE'], sys.argv[4]]).returncode)
if '--hostname' in sys.argv:
    position = sys.argv.index('--hostname'); assert sys.argv[position + 1] == 'github.com'
    del sys.argv[position:position + 2]
'''
    _executable(binary / "gh", gh + FAKE_GH.replace(
        "d['issue_state']=body['state'];", "d['source_graph']['issues']['2']['state']=body['state'].upper();d['issue_state']=body['state'];"))
    _executable(binary / "codex", _CODEX)
    for name, value in {"PATH": str(binary) + os.pathsep + os.environ["PATH"], "REMOTE_TEST_STATE": str(state),
                        "REMOTE_TEST_CALLS": str(calls), "REMOTE_TEST_BARE": str(remote), "ENTRY_ROLE_TRACE": str(roles),
                        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/fixture-terminal/bus"}.items():
        monkeypatch.setenv(name, value)
    defaults = UserDefaultsStore()
    defaults.configure(language="en", profile={"preset": "economy"})
    active = app_data_root() / "active" / "current"
    active.parent.mkdir(parents=True)
    active.symlink_to(Path(lifecycle_cli.__file__).resolve().parents[3], target_is_directory=True)
    environment = dict(os.environ)
    with _local_manager(tmp_path / "manager", environment) as (manager, serving, errors, children, trace, commands, output):
        monkeypatch.setenv("PATH", str(Path(manager.systemctl).parent) + os.pathsep + environment["PATH"])
        # The same sealed terminal PATH must reach both the CLI and the Host.
        environment["PATH"] = os.environ["PATH"]
        result = subprocess.run([sys.executable, "-m", 'agent_run', "run", "1", "--repo", "example/project",
                                 "--no-notifications", "--json"], cwd=git_repo, env=environment,
                                text=True, capture_output=True, timeout=30)
        assert result.returncode == 0, (result.stdout, result.stderr)
        accepted = json.loads(result.stdout)
        assert accepted["result"] == "accepted"
        serving.join(timeout=95)
        assert not serving.is_alive() and not errors, errors
        assert len(children) == len(commands) == 1 and children[0].returncode == 0, output
        assert trace == ["fdstore", "barrier", "host_exited", "store_closed"]
        command = commands[0]
        assert command[command.index("-m") + 1] == 'agent_run.runtime.executor'
        database = Path(command[command.index("--database") + 1])
        kernel = Kernel(database)
        assert kernel.status("ticket:2")["phase"] == "complete"
        assert kernel.status("ticket:3")["phase"] != "complete"
        assert kernel.operation_state().active == ()
        observed = [json.loads(line) for line in roles.read_text().splitlines()]
        assert [row["role"] for row in observed] == ["development", "review", "publication"]
        assert observed[0]["head"] == base
        assert observed[1]["checkout"] != observed[0]["checkout"]
        history = kernel.history("ticket:2")
        assert sum(event["kind"] == "worker_started" for event in history) == 3
        assert sum(event["kind"] == "worker_exited" for event in history) == 3
        ready = next(event["position"] for event in history if event["kind"] == "host_usage_ready")
        assert all(event["position"] > ready for event in history if event["kind"] == "worker_started")
        # Read-only forensic evidence: no test writes business facts or chooses
        # a successor. These are the real adapter's original physical receipts.
        with kernel.storage.transaction(write=False) as tx:
            rows = tx.all("SELECT a.role,a.result_id,o.*,t.backend_id FROM attempts a "
                          "JOIN invocations i ON i.id=a.current_invocation "
                          "JOIN invocation_outputs o ON o.invocation=i.id JOIN threads t ON t.id=i.thread "
                          "WHERE a.subject='ticket:2' ORDER BY a.ordinal")
            assert [row["role"] for row in rows] == ["development", "review", "publication"]
            for row, role in zip(rows, observed, strict=True):
                assert row["ordinal"] == 0 and row["dispatch_state"] == "exited" and row["exit_code"] == 0
                assert row["backend_id"] == role["thread"] and row["raw_id"] and row["result_id"]
                identity = json.loads(tx.get(row["process_identity"], "ticket:2"))
                assert identity["host_pid"] == children[0].pid and identity["start_token"]
                assert json.loads(tx.get(row["raw_id"], "ticket:2"))
        usage = kernel.host_usage(accepted["action_id"])
        assert usage is not None and usage.usage_ready and not usage.usage_released
        assert usage.main_pid == children[0].pid
        document = json.loads(state.read_text())
        assert len(document["prs"]) == 1
        pr = document["prs"][0]
        assert pr["merged"] and document["issue_state"] == "closed"
        merged = pr["merge_commit_sha"]
        assert _git(remote, "show", "-s", "--format=%P", merged).split() == [base]
        assert _git(remote, "show", "-s", "--format=%T", merged) == _git(remote, "show", "-s", "--format=%T", pr["head"]["sha"])
        assert _git(remote, "show", "-s", "--format=%P", pr["head"]["sha"]).split() == [base]
        assert _git(remote, "rev-parse", "refs/heads/" + pr["base"]["ref"]) == merged
        assert _git(remote, "show", f"{merged}:managed-delivery.txt") == "Implementation from the managed native Worker."
        assert "validation.tmp" not in _git(remote, "ls-tree", "-r", "--name-only", merged).splitlines()
        assert _git(remote, "rev-parse", "refs/heads/main") == base
        calls_seen = [json.loads(line) for line in calls.read_text().splitlines()]
        writes = [row for row in calls_seen if "--method" in row]
        merges = [index for index, row in enumerate(writes) if row[1].endswith("/merge")]
        comments = [index for index, row in enumerate(writes) if row[1] == "repos/example/project/issues/2/comments"]
        closes = [index for index, row in enumerate(writes) if row[1] == "repos/example/project/issues/2"]
        assert len(merges) == len(comments) == len(closes) == 1
        assert merges[0] < comments[0] < closes[0]
        # Publication display is a separate purpose on the PR, never the
        # Ticket's authoritative completion comment or another merge/close.
        status_rows = [row for row in document["comments"] if row["body"].startswith("<!-- agent-run-status:")]
        assert len(status_rows) == 1
        status_writes = document["status_writes"]
        assert [row["method"] for row in status_writes].count("POST") == 1
        assert all(row["id"] == status_rows[0]["id"] for row in status_writes)
        assert all(a["body"] != b["body"] for a, b in zip(status_writes, status_writes[1:]))
        assert "Delivery: complete" in status_rows[0]["body"]
        assert "Host exit: exit_unproven" in status_rows[0]["body"]  # Shared test cgroup is still populated.
        stopped = subprocess.run([sys.executable, "-m", "agent_run", "stop", "1", "--repo", "example/project", "--json"],
                                 cwd=git_repo, env=environment, text=True, capture_output=True, timeout=20)
        assert stopped.returncode == 0, (stopped.stdout, stopped.stderr)
        assert json.loads(stopped.stdout)["action"] == "stop"
        stopped_document = json.loads(state.read_text())
        updated, = [row for row in stopped_document["comments"] if row["body"].startswith("<!-- agent-run-status:")]
        assert updated["id"] == status_rows[0]["id"] and "operator_stopped" in updated["body"]
        assert [row["method"] for row in stopped_document["status_writes"]].count("POST") == 1
        assert kernel.status("ticket:2")["phase"] == "complete"
        with runner_management_lease(default_runner_lock_path(environment)):
            pass
        assert _git(git_repo, "status", "--porcelain=v1") == ""


@contextmanager
def _worker_controls(directory: Path, *, held_roles: set[tuple[str, str]] | None = None):
    """Hold only external Worker replies; never manufacture a Kernel fact."""
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind("")
    listener.listen(8)
    listener.settimeout(0.1)
    address = "@" + listener.getsockname()[1:].decode()
    stopped = threading.Event()
    arrivals: Queue = Queue()
    held: list[socket.socket] = []
    errors: list[BaseException] = []
    seen: set[str] = set()

    def serve() -> None:
        try:
            while not stopped.is_set():
                try:
                    channel, _ = listener.accept()
                except TimeoutError:
                    continue
                channel.settimeout(10)
                raw = bytearray()
                while chunk := channel.recv(65536):
                    raw.extend(chunk)
                    assert len(raw) <= 1024 * 1024
                event = json.loads(raw)
                name = Path(event["checkout"]).name
                selected = ((event["role"] == "development" and name in {"ticket-2", "ticket-3"})
                            if held_roles is None else (event["subject"], event["role"]) in held_roles)
                if selected and name not in seen:
                    seen.add(name)
                    held.append(channel)
                    arrivals.put((event, channel))
                else:
                    channel.sendall(b"1")
                    channel.close()
        except BaseException as error:
            if not stopped.is_set():
                errors.append(error)

    serving = threading.Thread(target=serve)
    serving.start()
    try:
        yield address, arrivals
    finally:
        stopped.set()
        for channel in held:
            try:
                channel.sendall(b"1")
            except OSError:
                pass
            channel.close()
        serving.join(timeout=2)
        listener.close()
        assert not serving.is_alive() and not errors, errors


def _control_environment(git_repo: Path, directory: Path, monkeypatch, *, codex: str,
                         tickets: tuple[int, ...] = (2, 3)) -> tuple[dict[str, str], Path]:
    import agent_run.cli.lifecycle as lifecycle_cli
    remote = directory / "origin.git"
    _git(git_repo, "init", "--bare", "--initial-branch=main", str(remote))
    _git(remote, "config", "gc.auto", "0")
    _git(remote, "config", "maintenance.auto", "false")
    _git(git_repo, "push", str(remote), "HEAD:refs/heads/main")
    binary = directory / "control-bin"
    binary.mkdir()
    state, calls, roles = directory / "github.json", directory / "gh-calls.jsonl", directory / "roles.jsonl"
    state.write_text(json.dumps({"prs": [], "comments": [], "events": [], "issue_state": "open",
        "source_graph": {"parent": {"number": 1, "title": "Native control Parent",
        "body": "Deliver each ready Ticket using the exact recorded requirements.", "sub_issues": list(tickets)}, "issues": {
        str(number): {"number": number, "title": f"Managed Ticket {number}",
                     "body": f"Original requirement for Ticket {number}.", "state": "OPEN", "labels": ["ready-for-agent"]}
        for number in tickets}}}))
    clone = ('import sys\nsys.path.insert(0, ' + repr(str(Path(__file__).parent)) + ')\n'
             'from support.fixture_text import read_text\n') + r'''import json, os, sys
if sys.argv[1:3] == ['issue', 'view']:
    number = sys.argv[3]
    source = json.loads(read_text(os.environ['REMOTE_TEST_STATE']))
    print(json.dumps(source['source_graph']['parent'] if number == '1' else source['source_graph']['issues'][number]))
    raise SystemExit(0)
if sys.argv[1:3] == ['repo', 'clone']:
    import subprocess
    raise SystemExit(subprocess.run(['git', 'clone', '--no-local', '--quiet', '--', os.environ['REMOTE_TEST_BARE'], sys.argv[4]]).returncode)
if '--hostname' in sys.argv:
    position = sys.argv.index('--hostname'); assert sys.argv[position + 1] == 'github.com'
    del sys.argv[position:position + 2]
if os.environ.get('ENTRY_APPROVAL_CONTROL') and sys.argv[1:3] == ['api', 'graphql']:
    import socket
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
        channel.settimeout(25)
        channel.connect(chr(0) + os.environ['ENTRY_APPROVAL_CONTROL'][1:])
        channel.sendall(b'1'); channel.shutdown(socket.SHUT_WR)
        assert channel.recv(1) == b'1'
'''
    gh = FAKE_GH
    gh = gh.replace("v['issueId']=='issue-node-2'",
        "v['issueId'] in {f\"issue-node-{issue['number']}\" for issue in "
        "(d['source_graph']['parent'], *d['source_graph']['issues'].values())}")
    gh = gh.replace("'number':17,'state':'open'", "'number':17+len(d['prs']),'state':'open'")
    gh = gh.replace("else: result=[d['prs']] if '--slurp' in args else d['prs']", """else:
            from urllib.parse import parse_qs, urlsplit
            query = parse_qs(urlsplit(args[1]).query)
            matches = [pull for pull in d['prs'] if pull['head']['ref'] == query['head'][0].split(':', 1)[1]
                       and pull['base']['ref'] == query['base'][0]]
            result=[matches] if '--slurp' in args else matches""")
    gh = gh.replace("elif endpoint.startswith('repos/example/project/pulls'):\n",
        """elif endpoint.startswith('repos/example/project/pulls'):
    for pull in d['prs']:
        if not pull['merged']:
            for field in ('head', 'base'):
                observed = head('refs/heads/' + pull[field]['ref'])
                if observed: pull[field]['sha'] = observed
""")
    # The shared leaf fixture represents one Ticket. Managed controls also
    # observe their Parent (and original PR issue endpoints) independently.
    closed_event = "d['events'].append({'id':len(d['events'])+1,'event':'closed','actor':{'login':'publisher'},'created_at':'2026-10-21T10:01:00Z'})"
    gh = gh.replace(closed_event, closed_event + ";d.setdefault('events_by_issue',{}).setdefault(str(number),[]).append(d['events'][-1])")
    issue_start = gh.index("elif endpoint.startswith('repos/example/project/issues/'):\n")
    issue_end = gh.index("\nelse:raise SystemExit('unexpected endpoint '+endpoint)", issue_start)
    gh = gh[:issue_start] + r'''elif endpoint.startswith('repos/example/project/issues/'):
    if d.get('issue_read_error') and method=='GET':
        print('HTTP 403 Issue unavailable',file=sys.stderr);raise SystemExit(1)
    number=int(endpoint.split('/issues/',1)[1].split('/')[0])
    graph=d['source_graph']
    resource=graph['parent'] if number==graph['parent']['number'] else graph['issues'].get(str(number))
    if resource is None:
        resource=next((pull for pull in d['prs'] if pull['number']==number),None)
    if resource is None:
        print('HTTP 404 Issue not found',file=sys.stderr);raise SystemExit(1)
    comments=d.setdefault('comments_by_issue',{}).setdefault(str(number),[])
    events=d.setdefault('events_by_issue',{}).setdefault(str(number),[])
    if endpoint.endswith('/comments'):
        if method=='POST':
            comment={'id':len(d['comments'])+1,'body':body['body'],'user':{'login':'publisher'},'created_at':'2026-10-21T10:00:00Z'}
            d['comments'].append(comment);comments.append(comment);result=comment
        else:result=[comments] if '--slurp' in args else comments
    elif endpoint.endswith('/events'):result=[events] if '--slurp' in args else events
    elif method=='PATCH':
        resource['state']=body['state'].upper()
        d['issue_state']=body['state']
        event='closed' if body['state']=='closed' else 'reopened'
        at='2026-10-21T10:01:00Z' if event=='closed' else '2026-10-21T10:02:00Z'
        observed={'id':len(d['events'])+1,'event':event,'actor':{'login':'publisher'},'created_at':at}
        d['events'].append(observed);events.append(observed);result={'state':body['state']}
    else:result={'number':number,'node_id':f'issue-node-{number}','state':resource.get('state','OPEN').lower()}
''' + gh[issue_end:]
    _executable(binary / "gh", clone + gh)
    _executable(binary / "codex", codex)
    for name, value in {"PATH": str(binary) + os.pathsep + os.environ["PATH"], "REMOTE_TEST_STATE": str(state),
                        "REMOTE_TEST_CALLS": str(calls), "REMOTE_TEST_BARE": str(remote), "ENTRY_ROLE_TRACE": str(roles),
                        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/fixture-terminal/bus"}.items():
        monkeypatch.setenv(name, value)
    UserDefaultsStore().configure(language="en", profile={"preset": "economy"}, policy={"ticket_development_workers": 2})
    active = app_data_root() / "active" / "current"
    active.parent.mkdir(parents=True)
    active.symlink_to(Path(lifecycle_cli.__file__).resolve().parents[3], target_is_directory=True)
    return dict(os.environ), state


@contextmanager
def _control_process(*arguments, **options):
    """Own the original CLI before any fallible Host identity observation."""
    with ExitStack() as handles:
        child = subprocess.Popen(*arguments, **options)
        descriptor = -1
        try:
            for stream in (child.stdin, child.stdout, child.stderr):
                if stream is not None:
                    handles.callback(stream.close)
            descriptor = os.pidfd_open(child.pid)
            handles.callback(os.close, descriptor)
            yield child, descriptor
        finally:
            try:
                if child.poll() is None:
                    if descriptor >= 0:
                        _kill_control_process(descriptor)
                    else:
                        # Acquisition failed before any wait: this unreaped direct
                        # child still belongs exclusively to its creating Popen.
                        child.kill()
            finally:
                child.wait(timeout=3)
    # CLI exit alone is never evidence that its namespace has exited.


def _kill_control_process(descriptor):
    try:
        signal.pidfd_send_signal(descriptor, signal.SIGKILL)
    except ProcessLookupError:
        if not select.select([descriptor], [], [], 0)[0]:
            raise


def _pin_control_host(record, pins):
    from agent_run.adapters.foreground import ProcessPin

    for witness in (record['domain']['executor'], record['domain']['init'], record['monitor']):
        # Transfer each handle to cleanup before the next open can fail.
        pins.append(ProcessPin.open(witness['pid'], witness))
    return pins[0]


def _finish_control_host(child, pins, *, child_fd, stop=None, signal_host=True):
    """Cleanup exact owned handles; never infer domain exit from CLI exit."""
    deadline = time.monotonic() + 20
    errors = []
    try:
        try:
            if stop is not None and child.poll() is None:
                stop()
        except BaseException as error:
            errors.append(error)
        finally:
            try:
                if signal_host and pins and pins[0].observe() != 'exited':
                    pins[0].kill()
                elif not pins and child.poll() is None:
                    _kill_control_process(child_fd)
            except BaseException as error:
                errors.append(error)
            try:
                if child.poll() is None:
                    child.communicate(timeout=max(0, deadline - time.monotonic()))
            except BaseException as error:
                errors.append(error)
            finally:
                try:
                    # Popen alone reaps its original CLI child.
                    child.wait(timeout=max(0, deadline - time.monotonic()))
                except BaseException as error:
                    errors.append(error)
    finally:
        for pin in pins:
            try:
                pin.wait_exit(max(0, deadline - time.monotonic()))
                # Only an adopted child may be reaped through this pidfd.
                # ECHILD/None never invents a successful business exit.
                pin.reap()
            except BaseException as error:
                errors.append(error)
            finally:
                try:
                    pin.close()
                except BaseException as error:
                    errors.append(error)
        for stream in (child.stdout, child.stderr):
            try:
                if stream is not None:
                    stream.close()
            except BaseException as error:
                errors.append(error)
    if errors:
        raise BaseExceptionGroup('Owned control Host cleanup failed', errors)


@pytest.mark.parametrize('fault', ('pin-executor', 'pin-init', 'pin-monitor', 'stop', 'communicate', 'pin-wait', 'pin-close'))
def test_control_host_cleanup_preserves_fault_and_closes_every_owned_handle(monkeypatch, fault) -> None:
    """Mechanical cleanup contract; not a substitute for the public stories."""
    from agent_run.adapters.foreground import ProcessIdentity, ProcessPin

    with _control_process([sys.executable, '-c', 'import time;time.sleep(30)'],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as (child, child_fd):
        pins, signalled = [], []
        original_open, original_send = ProcessPin.open, signal.pidfd_send_signal
        original_wait, original_close = ProcessPin.wait_exit, ProcessPin.close
        witness = ProcessIdentity.read(child.pid).wire()
        record = {'domain': {'executor': witness, 'init': witness}, 'monitor': witness}
        failure = OSError('injected ' + fault)
        failed_open = {'pin-executor': 0, 'pin-init': 1, 'pin-monitor': 2}.get(fault)

        def open_pin(cls, pid, expected=None):
            if failed_open is not None and len(pins) == failed_open:
                raise failure
            return original_open(pid, expected)

        def send(descriptor, number, *arguments):
            assert descriptor in {child_fd, *(pin.descriptor for pin in pins)}
            signalled.append((descriptor, number))
            original_send(descriptor, number, *arguments)

        def fail(*args, **kwargs):
            raise failure

        def wait(pin, seconds):
            if fault == 'pin-wait' and pin is pins[0]:
                raise failure
            original_wait(pin, seconds)

        def close(pin):
            original_close(pin)
            if fault == 'pin-close' and pin is pins[0]:
                raise failure

        monkeypatch.setattr(ProcessPin, 'open', classmethod(open_pin))
        monkeypatch.setattr(signal, 'pidfd_send_signal', send)
        monkeypatch.setattr(ProcessPin, 'wait_exit', wait)
        monkeypatch.setattr(ProcessPin, 'close', close)
        if fault == 'communicate':
            monkeypatch.setattr(child, 'communicate', fail)
        if failed_open is not None:
            with pytest.raises(OSError, match='injected') as opened:
                _pin_control_host(record, pins)
            assert opened.value is failure
            expected_signal = child_fd if not pins else pins[0].descriptor
            _finish_control_host(child, pins, child_fd=child_fd)
        else:
            _pin_control_host(record, pins)
            expected_signal = pins[0].descriptor
            with pytest.raises(BaseExceptionGroup) as cleaned:
                _finish_control_host(child, pins, child_fd=child_fd, stop=fail if fault == 'stop' else None)
            assert failure in cleaned.value.exceptions
        assert signalled == [(expected_signal, signal.SIGKILL)]
        assert child.returncode == -signal.SIGKILL
        assert all(pin.descriptor == pin.namespace_fd == -1 for pin in pins)
        assert child.stdout.closed and child.stderr.closed
        assert select.select([child_fd], [], [], 0)[0] == [child_fd]
        with pytest.raises(ChildProcessError):
            os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
    with pytest.raises(OSError):
        os.fstat(child_fd)


@pytest.mark.parametrize('original_exit', ('normal-return', 'prior-fault'))
def test_control_host_cleanup_does_not_repeat_the_original_fault_signal(monkeypatch, original_exit) -> None:
    from agent_run.adapters.foreground import ProcessPin

    with _control_process([sys.executable, '-c', 'import sys;sys.stdin.read(1)'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as (child, child_fd):
        pin = None
        try:
            pin = ProcessPin.open(child.pid)
            signalled = []
            if original_exit == 'prior-fault':
                pin.kill()
            else:
                child.communicate(input='1', timeout=10)
            monkeypatch.setattr(ProcessPin, 'kill', lambda target: signalled.append(target.identity.pid))
            _finish_control_host(child, [pin], child_fd=child_fd, signal_host=original_exit != 'prior-fault')
            assert signalled == []
            assert child.returncode == (0 if original_exit == 'normal-return' else -signal.SIGKILL)
            assert pin.descriptor == pin.namespace_fd == -1
            assert child.stdout.closed and child.stderr.closed and child.stdin.closed
        finally:
            if pin is not None:
                pin.close()


@pytest.mark.parametrize('failure_at', ('identity', 'owner-fd'))
def test_control_process_owns_child_before_first_identity_observation(monkeypatch, failure_at) -> None:
    from agent_run.adapters.foreground import ProcessPin

    original_popen, original_open = subprocess.Popen, os.pidfd_open
    owned = []
    failure = OSError('first observation failed')

    def popen(*arguments, **options):
        child = original_popen(*arguments, **options)
        try:
            owned.append((child, original_open(child.pid)))
        except BaseException:
            child.kill()
            child.wait(timeout=3)
            for stream in (child.stdout, child.stderr):
                stream.close()
            raise
        return child

    def fail(*arguments, **options):
        raise failure

    monkeypatch.setattr(subprocess, 'Popen', popen)
    monkeypatch.setattr(ProcessPin, 'open', fail)
    if failure_at == 'owner-fd':
        monkeypatch.setattr(os, 'pidfd_open', fail)
    try:
        with pytest.raises(OSError) as observed:
            with _control_process([sys.executable, '-c', 'import time;time.sleep(30)'],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as (child, _):
                ProcessPin.open(child.pid)
        assert observed.value is failure
        child, witness = owned[0]
        assert child.returncode == -signal.SIGKILL
        assert select.select([witness], [], [], 0)[0] == [witness]
        assert child.stdout.closed and child.stderr.closed
    finally:
        for child, witness in owned:
            try:
                if child.poll() is None:
                    signal.pidfd_send_signal(witness, signal.SIGKILL)
                    child.wait(timeout=3)
            finally:
                os.close(witness)


def test_public_parallel_review_and_publication_keep_each_threads_binding(
    git_repo: Path, tmp_path: Path, monkeypatch,
) -> None:
    from agent_run.adapters.foreground import ProcessPin, read_record, record_root, subreaper
    classification = """schema = json.loads(Path(sys.argv[sys.argv.index('--output-schema') + 1]).read_text())
role = 'review' if 'checks' in schema['properties'] else 'publication' if 'commit_message' in schema['properties'] else 'development'
"""
    codex = _CODEX[:_CODEX.index("if prompt.startswith")] + classification + _CODEX[_CODEX.index("thread = "):]
    codex = codex.replace("'managed-entry-' + role)", "'managed-entry-' + role + '-' + os.urandom(16).hex())")
    marker = "print(json.dumps({'type': 'thread.started', 'thread_id': thread}), flush=True)"
    before = r"""control = None
import re
issue = int(re.search(r'https://github.com/example/project/issues/(\d+)', prompt).group(1))
subject = 'ticket-' + str(issue)
if (subject, role) in {('ticket-2', 'publication'), ('ticket-3', 'review')}:
    import socket
    control = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    control.settimeout(30)
    control.connect('\0' + os.environ['ENTRY_WORKER_CONTROL'][1:])
    control.sendall(json.dumps({'role': role, 'thread': thread, 'checkout': str(Path.cwd()), 'head': head, 'subject': subject}).encode())
    control.shutdown(socket.SHUT_WR)
    assert control.recv(1) == b'1'
"""
    codex = codex.replace(marker, before + marker)
    codex = codex.replace("content = 'Implementation", """if control is not None:
    assert control.recv(1) == b'1'
    control.close()
content = 'Implementation""")
    roles = {('ticket-2', 'publication'), ('ticket-3', 'review')}
    with _worker_controls(tmp_path, held_roles=roles) as (address, arrivals), subreaper(), ExitStack() as processes:
        monkeypatch.setenv('ENTRY_WORKER_CONTROL', address)
        environment, remote_state = _control_environment(git_repo, tmp_path, monkeypatch, codex=codex)
        child, child_fd = processes.enter_context(_control_process([sys.executable, '-m', 'agent_run', 'run', '1', '--foreground',
            '--no-notifications', '--repo', 'example/project', '--json'], cwd=git_repo, env=environment,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
        pins = []
        snapshots = []

        def public(command):
            result = subprocess.run([sys.executable, '-m', 'agent_run', command, '--parent', '1',
                '--repo', 'example/project', '--json'], cwd=git_repo, env=environment,
                capture_output=True, text=True, timeout=20)
            assert result.returncode == 0, (result.stdout, result.stderr)
            return json.loads(result.stdout)

        def invocation(number, role):
            history = public('history')['history']
            attempt, = (row for row in history['attempts'] if row['subject'] == f'ticket:{number}' and row['role'] == role)
            original, = attempt['invocations']
            return attempt, original

        try:
            events = [arrivals.get(timeout=30) for _ in range(2)]
            channels = {event['role']: (event, channel) for event, channel in events}
            assert set(channels) == {'review', 'publication'}
            status = public('status')
            run_id = status['run_id']
            budgets = {row['subject']: row['budgets'] for row in status['subjects'] if row['kind'] == 'ticket'}
            lock = default_runner_lock_path(environment)
            record, = (read_record(path) for path in record_root(lock).glob('*.json'))
            assert record['binding'][0] == run_id and record['phase'] == 'running'
            _pin_control_host(record, pins)

            # Force the historical order using real stdout, not bind_thread calls.
            event_b, channel_b = channels['review']
            event_a, channel_a = channels['publication']
            channel_b.sendall(b'1')
            def bound_b():
                attempt, row = invocation(3, 'review')
                return (attempt, row) if row['backend_id'] == event_b['thread'] and any(
                    item['active'] and item['evidence']['type'] == 'turn.started'
                    for output in row['outputs'] for item in output['activity']) else None
            attempt_b, row_b = wait_until(bound_b, timeout=10)
            snapshot_b = {key: row_b[key] for key in ('id', 'attempt', 'thread', 'backend_id', 'profile_revision', 'model', 'effort')}
            assert attempt_b['result'] is None
            channel_a.sendall(b'1')
            def bound_a():
                attempt, row = invocation(2, 'publication')
                return (attempt, row) if row['backend_id'] == event_a['thread'] and any(
                    item['active'] and item['evidence']['type'] == 'turn.started'
                    for output in row['outputs'] for item in output['activity']) else None
            attempt_a, row_a = wait_until(bound_a, timeout=10)
            assert attempt_b['candidate_sha'] == event_b['head'] and attempt_a['candidate_sha'] == event_a['head']
            after_b, current_b = invocation(3, 'review')
            assert {key: current_b[key] for key in snapshot_b} == snapshot_b and after_b['result'] is None
            assert row_a['thread'] != row_b['thread'] and event_a['thread'] != event_b['thread']
            assert {row['subject']: row['budgets'] for row in public('status')['subjects'] if row['kind'] == 'ticket'} == budgets
            snapshots.append({'review_after_publication_callback': current_b, 'publication': row_a})

            channel_b.sendall(b'1')
            def consumed_b():
                attempt, _ = invocation(3, 'review')
                return attempt if attempt['outcome'] is not None else None
            completed_b = wait_until(consumed_b, timeout=15)
            assert completed_b['outcome'] == 'pass'
            still_a, current_a = invocation(2, 'publication')
            assert current_a['backend_id'] == event_a['thread'] and still_a['result'] is None
            channel_a.sendall(b'1')
            output, error = child.communicate(timeout=90)
            assert child.returncode == 0, (output, error)
            accepted = json.loads(output)
            assert accepted['run_id'] == run_id and accepted['host'] == 'exited'
            final = public('status')
            tickets = [row for row in final['subjects'] if row['kind'] == 'ticket']
            assert len(tickets) == 2 and all(row['phase'] == 'complete' and not row['gates'] for row in tickets)
            assert all(row['delivery_cleanup']['state'] == 'complete' for row in tickets)
            for ticket in tickets:
                budget, = (row for row in ticket['budgets'] if row['purpose'] == 'requirements')
                assert (budget['window'], budget['developments_used'], budget['reviews_used']) == (1, 1, 1)
            for number, role, original in ((3, 'review', row_b), (2, 'publication', row_a)):
                attempt, current = invocation(number, role)
                assert current['id'] == original['id'] and current['backend_id'] == original['backend_id']
                assert attempt['result'] is not None and all(item['dispatch_state'] == 'exited' for item in current['outputs'])
                assert sum(event['kind'] == 'role_consumed' and event['identity'] == attempt['id']
                           for event in public('history')['history']['events']) == 1
            document = json.loads(remote_state.read_text())
            assert all(document['source_graph']['issues'][str(number)]['state'] == 'CLOSED' for number in (2, 3))
            assert all(pin.observe() == 'exited' for pin in pins)
            assert not list(record_root(lock).glob('*.json'))
            with runner_management_lease(lock):
                pass
        finally:
            def stop():
                subprocess.run([sys.executable, '-m', 'agent_run', 'stop', '1', '--repo', 'example/project', '--json'],
                    cwd=git_repo, env=environment, capture_output=True, timeout=10)
            try:
                (tmp_path / 'parallel-callback-snapshots.json').write_text(json.dumps(snapshots))
            finally:
                _finish_control_host(child, pins, child_fd=child_fd, stop=stop)


def test_public_resume_after_exact_foreground_host_loss_preserves_parallel_work(
    git_repo: Path, tmp_path: Path, monkeypatch, record_property,
) -> None:
    from agent_run.adapters.foreground import ProcessPin, read_record, record_root, subreaper
    classification = """schema = json.loads(Path(sys.argv[sys.argv.index('--output-schema') + 1]).read_text())
role = 'review' if 'checks' in schema['properties'] else 'publication' if 'commit_message' in schema['properties'] else 'development'
"""
    codex = _CODEX[:_CODEX.index("if prompt.startswith")] + classification + _CODEX[_CODEX.index("thread = "):]
    codex = codex.replace("'managed-entry-' + role)", "'managed-entry-' + role + '-' + os.urandom(16).hex())")
    held = r"""if role == 'development':
    import socket
    Path('managed-delivery.txt').write_text('Implementation from the managed native Worker.\n')
    Path(sys.argv[sys.argv.index('--output-last-message') + 1]).write_text(json.dumps(
        {'result_kind':'development', 'summary':'Old file before Host loss is not an accepted return.', 'human_blockers':None}))
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as control:
        control.settimeout(30)
        control.connect('\0' + os.environ['ENTRY_WORKER_CONTROL'][1:])
        control.sendall(json.dumps({'role':role, 'thread':thread, 'checkout':str(Path.cwd()), 'head':head}).encode())
        control.shutdown(socket.SHUT_WR)
        assert control.recv(1) == b'1'
"""
    codex = codex.replace("content = 'Implementation", held + "\ncontent = 'Implementation")
    with _worker_controls(tmp_path) as (_address, arrivals), subreaper(), ExitStack() as processes:
        monkeypatch.setenv('ENTRY_WORKER_CONTROL', _address)
        environment, remote_state = _control_environment(git_repo, tmp_path, monkeypatch, codex=codex)
        child, child_fd = processes.enter_context(_control_process([sys.executable, '-m', 'agent_run', 'run', '1', '--foreground',
            '--no-notifications', '--repo', 'example/project', '--json'], cwd=git_repo, env=environment,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
        pins, observations = [], {}
        host_pin = None
        sent = False

        def public(*arguments):
            command = [sys.executable, '-m', 'agent_run', *arguments,
                       '--repo', 'example/project', '--json']
            if arguments[0] not in {'resume', 'approve'}:
                return subprocess.run(command, cwd=git_repo, env=environment,
                    capture_output=True, text=True, timeout=90)
            deadline = time.monotonic() + 90
            with _control_process(command, cwd=git_repo, env=environment,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as (continued, continued_fd):
                continued_pins = []
                try:
                    def launched():
                        for path in record_root(default_runner_lock_path(environment)).glob('*.json'):
                            record = read_record(path)
                            if record['phase'] == 'running':
                                return (record,)
                        return (None,) if continued.poll() is not None else None
                    launch, = wait_until(launched, timeout=min(10, max(0, deadline - time.monotonic())))
                    if launch is not None:
                        assert launch['binding'][0] == observations['run']
                        _pin_control_host(launch, continued_pins)
                    stdout, stderr = continued.communicate(timeout=max(0, deadline - time.monotonic()))
                    if continued_pins:
                        assert all(pin.observe() == 'exited' for pin in continued_pins)
                    observations.setdefault('continuations', []).append({
                        'command': arguments[0], 'record': launch,
                        'pinned': [pin.identity.wire() for pin in continued_pins],
                        'physical_exit': [pin.observe() for pin in continued_pins],
                        'returncode': continued.returncode, 'stdout': stdout, 'stderr': stderr})
                    return subprocess.CompletedProcess(command, continued.returncode, stdout, stderr)
                finally:
                    _finish_control_host(continued, continued_pins, child_fd=continued_fd)

        def query(command):
            result = public(command, '--parent', '1')
            assert result.returncode == 0, (result.stdout, result.stderr)
            return json.loads(result.stdout)

        try:
            ready = [arrivals.get(timeout=30) for _ in range(2)]
            assert {Path(event['checkout']).name for event, _ in ready} == {'ticket-2', 'ticket-3'}
            before = query('status')
            expected_threads = {Path(event['checkout']).name.replace('-', ':'): event['thread'] for event, _ in ready}
            def bound_attempts():
                rows = {row['subject']: row for row in query('history')['history']['attempts'] if row['role'] == 'development'}
                return rows if set(rows) == set(expected_threads) and all(
                    len(row['invocations']) == 1 and row['invocations'][0]['backend_id'] == expected_threads[subject]
                    for subject, row in rows.items()) else None
            attempts = wait_until(bound_attempts, timeout=10)
            lock = default_runner_lock_path(environment)
            record, = (read_record(path) for path in record_root(lock).glob('*.json'))
            assert record['phase'] == 'running' and record['binding'][0] == before['run_id']
            host_pin = _pin_control_host(record, pins)
            kernel = Kernel(Path(record['database']))
            # Physical witness only: observe existing bindings, never repair SQL.
            with kernel.storage.transaction(write=False) as tx:
                workers = []
                for subject, attempt in attempts.items():
                    invocation, = attempt['invocations']
                    assert invocation['backend_id'] == next(event['thread'] for event, _ in ready
                        if Path(event['checkout']).name == subject.replace(':', '-'))
                    row = tx.one('SELECT process_identity,raw_id FROM invocation_outputs WHERE invocation=?', (invocation['id'],))
                    assert row['raw_id'] is None and attempt['result'] is None
                    workers.append(json.loads(tx.get(row['process_identity'], subject)))
            # Core's authentic Worker receipt binds the process to this exact
            # Host namespace. Retained namespace-init exit proves that whole
            # domain is dead; inner PID numbers must not be reopened outside it.
            assert len({(item['pid'], item['start_token']) for item in workers}) == 2
            for witness in workers:
                assert witness['schema'] == 'linux-namespace-process-v1'
                assert (witness['host_pid'], witness['host_start_token'], witness['boot_id'],
                        witness['host_namespace'], witness['pid_namespace']) == (
                    host_pin.identity.namespace_pids[-1], host_pin.identity.start_token,
                    host_pin.identity.boot_id, host_pin.identity.pid_namespace, host_pin.identity.pid_namespace)
            assert all(pin.observe() == 'running' for pin in pins)
            observations.update(run=before['run_id'], original_record=record, before=before,
                original_attempts=attempts, workers=workers, pinned=[pin.identity.wire() for pin in pins])

            # Only the exact original executor handle receives this fault signal.
            sent = True
            host_pin.kill()
            out, err = child.communicate(timeout=20)
            for pin in pins:
                pin.wait_exit(10)
            observations.update(original_cli={'returncode': child.returncode, 'stdout': out, 'stderr': err},
                physical_exit=[pin.observe() for pin in pins])
            after = query('status')
            history = query('history')['history']
            assert after['run_id'] == before['run_id']
            assert {row['subject']: row['budgets'] for row in after['subjects']} == {
                row['subject']: row['budgets'] for row in before['subjects']}
            assert not list(record_root(lock).glob('*.json'))
            with kernel.storage.transaction(write=False) as tx:
                assert not tx.rows('SELECT id FROM leases')
                assert tx.one('SELECT status,usage_released FROM hosts WHERE nonce=?', (record['binding'][3],))[:] == ('exited', 1)
            for subject, original in attempts.items():
                current, = (row for row in history['attempts'] if row['id'] == original['id'])
                assert current['result'] is None
                invocation, = current['invocations']
                prior, = original['invocations']
                assert (invocation['id'], invocation['backend_id']) == (prior['id'], prior['backend_id'])
                assert all(output['dispatch_state'] == 'exited' and output['exit_code'] is None
                           and output['raw_return'] is None for output in invocation['outputs'])
                assert any(event['kind'] == 'worker_host_exited' and event['identity'] == invocation['id']
                           for event in history['events'])
            observations.update(after_exit=after, history_after_exit=history)
            original_action = json.loads(out)['action_id']
            original_request = kernel.host_request(original_action)
            assert original_request.run_id == before['run_id']
            resumed = public('resume', before['run_id'], '--foreground')
            observations['first_resume'] = {'returncode': resumed.returncode, 'stdout': resumed.stdout, 'stderr': resumed.stderr}
            (tmp_path / 'host-loss-first-resume.json').write_text(json.dumps(observations))
            assert resumed.returncode == 0, (resumed.stdout, resumed.stderr)
            receipt = json.loads(resumed.stdout)
            assert receipt['run_id'] == before['run_id'] and receipt['host'] == 'exited'
            observations['first_resume_passed'] = True
            assert kernel.host_request(original_action) == original_request
            at_approval = query('status')
            assert at_approval['run_id'] == before['run_id']
            tickets = [row for row in at_approval['subjects'] if row['kind'] == 'ticket']
            final, = [row for row in at_approval['subjects'] if row['kind'] == 'final_run']
            assert {row['subject'] for row in tickets} == set(attempts)
            assert all(row['phase'] == 'complete' and not row['gates']
                       and row['delivery_cleanup']['state'] == 'complete' for row in tickets)
            assert final['phase'] == 'approval' and final['pr_number'] is not None
            final_budgets = final['budgets']
            for ticket in tickets:
                old = next(row for row in before['subjects'] if row['subject'] == ticket['subject'])
                initial, = [b for b in old['budgets'] if b['purpose'] == 'requirements']
                completed, = [b for b in ticket['budgets'] if b['purpose'] == 'requirements']
                assert all(completed[key] == initial[key] for key in (
                    'window', 'developments_used', 'development_limit', 'review_limit'))
                assert completed['reviews_used'] == initial['reviews_used'] + 1
            recovered_history = query('history')['history']
            for original in attempts.values():
                current, = [row for row in recovered_history['attempts'] if row['id'] == original['id']]
                prior, = original['invocations']
                old, = [row for row in current['invocations'] if row['id'] == prior['id']]
                renewed, = [row for row in current['invocations'] if row['id'] != prior['id']]
                assert old['backend_id'] == renewed['backend_id'] == prior['backend_id']
                assert old['thread'] == renewed['thread'] == prior['thread']
                assert all(row['raw_return'] is None and row['exit_code'] is None
                           and row['dispatch_state'] == 'exited' for row in old['outputs'])
                assert current['result'] is not None and all(row['dispatch_state'] == 'exited'
                    and row['exit_code'] == 0 and row['raw_return'] is not None for row in renewed['outputs'])
                assert sum(event['kind'] == 'role_consumed' and event['identity'] == original['id']
                           for event in recovered_history['events']) == 1
            observations.update(before_approval=at_approval, recovered_history=recovered_history,
                                original_action=original_action)
            (tmp_path / 'host-loss-first-resume.json').write_text(json.dumps(observations))
            approved = public('approve', before['run_id'], '--foreground')
            observations['approval'] = {'returncode': approved.returncode,
                                        'stdout': approved.stdout, 'stderr': approved.stderr}
            assert approved.returncode == 0, (approved.stdout, approved.stderr)
            approval = json.loads(approved.stdout)
            assert approval['run_id'] == before['run_id'] and approval['host'] == 'exited'
            assert approval['action_id'] not in {original_action, receipt['action_id']}
            finished = query('status')
            assert finished['run_id'] == before['run_id'] and finished['status'] == 'completed'
            assert all(row['phase'] == 'complete' and not row['gates']
                       and row['delivery_cleanup']['state'] == 'complete' for row in finished['subjects'])
            completed_final, = [row for row in finished['subjects'] if row['subject'] == final['subject']]
            assert completed_final['pr_number'] == final['pr_number'] and completed_final['budgets'] == final_budgets
            assert kernel.host_request(original_action) == original_request
            with kernel.storage.transaction(write=False) as tx:
                assert not tx.rows('SELECT id FROM leases')
                assert not tx.rows("SELECT nonce FROM hosts WHERE status!='exited' OR usage_released!=1")
                grant = tx.one('SELECT action,pr_number FROM grants WHERE subject=?', (final['subject'],))
                assert (grant['action'], grant['pr_number']) == (approval['action_id'], final['pr_number'])
            document = json.loads(remote_state.read_text())
            assert document['source_graph']['parent']['state'] == 'CLOSED'
            assert all(document['source_graph']['issues'][str(n)]['state'] == 'CLOSED' for n in (2, 3))
            assert len(document['prs']) == 3 and all(pr['merged'] for pr in document['prs'])
            assert _git(tmp_path / 'origin.git', 'show', 'refs/heads/main:managed-delivery.txt') == 'Implementation from the managed native Worker.'
            for job in finished['subjects']:
                cleanup = job['delivery_cleanup']
                absent = subprocess.run(['git', '-C', str(tmp_path / 'origin.git'), 'show-ref',
                    '--verify', '--quiet', cleanup['source_ref']], capture_output=True, timeout=3)
                assert absent.returncode == 1
                assert not Path(cleanup['checkout']).exists()
            assert not list(record_root(lock).glob('*.json'))
            assert all(pin.observe() == 'exited' for pin in pins)
            with runner_management_lease(lock):
                pass
            observations.update(completed=finished, remote=document, complete_same_run=True)
        finally:
            # No recovery Stop/gate is added, including after a rejected Resume.
            try:
                (tmp_path / 'host-loss-first-resume.json').write_text(json.dumps(observations))
                record_property('host_loss_recovery', json.dumps(observations))
            finally:
                _finish_control_host(child, pins, child_fd=child_fd, signal_host=not sent)


def test_public_ticket_stop_keeps_late_raw_unconsumed_then_resumes_in_the_original_managed_host(
    git_repo: Path, tmp_path: Path, monkeypatch,
) -> None:
    late = r'''
import socket
requirement = None
if role == 'development' and Path.cwd().name.startswith('ticket-'):
    number = Path.cwd().name.removeprefix('ticket-')
    requirement = json.loads(subprocess.check_output(['gh', 'issue', 'view', number, '--repo', 'example/project',
        '--json', 'number,title,body'], text=True))
if role == 'development' and Path.cwd().name == 'ticket-2':
    Path('managed-delivery.txt').write_text('Implementation from the managed native Worker.\n')
    Path(sys.argv[sys.argv.index('--output-last-message') + 1]).write_text(json.dumps(
        {'result_kind':'development', 'summary':'Late original Worker return after Stop.', 'human_blockers':None}))
with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as control:
    control.settimeout(30)
    control.connect('\0' + os.environ['ENTRY_WORKER_CONTROL'][1:])
    control.sendall(json.dumps({'role':role, 'thread':thread, 'checkout':str(Path.cwd()), 'prompt':prompt,
                               'requirement':requirement}).encode())
    control.shutdown(socket.SHUT_WR)
    assert control.recv(1) == b'1'
'''
    codex = _CODEX.replace("'managed-entry-' + role)", "'managed-entry-' + role + '-' + Path.cwd().name)")
    codex = codex.replace("content = 'Implementation", late + "\ncontent = 'Implementation")
    with _worker_controls(tmp_path) as (address, arrivals):
        monkeypatch.setenv("ENTRY_WORKER_CONTROL", address)
        environment, _state = _control_environment(git_repo, tmp_path, monkeypatch, codex=codex)
        with _local_manager(tmp_path / "manager", environment) as (manager, serving, errors, children, trace, commands, output):
            environment["PATH"] = str(Path(manager.systemctl).parent) + os.pathsep + environment["PATH"]
            supervision_started = time.monotonic()
            supervision_deadline = supervision_started + 90
            def remaining():
                return max(0.01, supervision_deadline - time.monotonic())
            def public(*arguments: str):
                result = subprocess.run([sys.executable, "-m", 'agent_run', *arguments, "--repo", "example/project", "--json"],
                    cwd=git_repo, env=environment, text=True, capture_output=True, timeout=min(20, remaining()))
                return result.returncode, json.loads(result.stdout)
            code, accepted = public("run", "1", "--no-notifications")
            assert code == 0, accepted
            initial = [arrivals.get(timeout=min(15, remaining())) for _ in range(2)]
            assert {Path(event["checkout"]).name for event, _channel in initial} == {"ticket-2", "ticket-3"}
            command = commands[0]
            kernel = Kernel(Path(command[command.index("--database") + 1]))
            request = kernel.host_request(accepted["action_id"])
            before = kernel.status("ticket:2")
            attempt = before["pending_attempt"]
            assert attempt is not None
            def original_thread():
                with kernel.storage.transaction(write=False) as tx:
                    row = tx.one("SELECT i.id,i.thread,t.backend_id FROM invocations i JOIN threads t ON t.id=i.thread WHERE i.attempt=?", (attempt,))
                    return dict(row) if row["backend_id"] else None
            original = wait_until(original_thread, timeout=min(10, remaining()))
            def sibling_running():
                from agent_run.runtime.process_identity import _process_start_token
                with kernel.storage.transaction(write=False) as tx:
                    row = tx.one("SELECT l.id,l.invocation,o.process_identity,o.dispatch_state,o.exit_code FROM leases l "
                        "JOIN invocation_outputs o ON o.lease=l.id WHERE l.subject='ticket:3'")
                    assert row["dispatch_state"] == "started" and row["exit_code"] is None
                    identity = json.loads(tx.get(row["process_identity"], "ticket:3"))
                    assert identity["host_pid"] == children[0].pid
                    assert _process_start_token(identity["pid"]) == identity["start_token"]
                    descriptor = os.pidfd_open(identity["pid"])
                    try:
                        assert select.select([descriptor], [], [], 0)[0] == []
                    finally:
                        os.close(descriptor)
                    return row["id"], row["invocation"], row["process_identity"]
            sibling = sibling_running()
            for event, _channel in initial:
                number = event["requirement"]["number"]
                assert event["requirement"]["body"] == f"Original requirement for Ticket {number}."
                assert f"https://github.com/example/project/issues/{number}" in event["prompt"]
            stop_started = time.monotonic()
            code, stopped = public("stop", accepted["run_id"], "--ticket", "2")
            assert code == 0, stopped
            # Stop revokes authority; it does not claim a fresh physical Host observation.
            assert stopped["host"] == "unknown" and kernel.host_request(accepted["action_id"]) == request
            def settled():
                with kernel.storage.transaction(write=False) as tx:
                    row = tx.one("SELECT raw_id,dispatch_state,exit_code FROM invocation_outputs WHERE invocation=?", (original["id"],))
                    return dict(row) if row["raw_id"] and row["dispatch_state"] == "exited" else None
            raw = wait_until(settled, timeout=min(10, remaining()))
            paused = kernel.status("ticket:2")
            assert "operator_stopped" in paused["gates"] and paused["pending_return"]
            assert paused["pending_attempt"] == attempt and paused["phase"] == "development"
            assert paused["budgets"] == before["budgets"]
            assert sibling_running() == sibling
            with kernel.storage.transaction(write=False) as tx:
                assert b"Late original Worker return after Stop." in tx.get(raw["raw_id"], "ticket:2")
                assert tx.one("SELECT status FROM attempts WHERE id=?", (attempt,))[0] == "returned"
                assert not tx.rows("SELECT identity FROM history WHERE kind='role_consumed' AND identity=?", (attempt,))
                stopped_at = tx.one("SELECT position FROM history WHERE kind='control_applied' AND identity=?", (stopped["action_id"],))[0]
                assert tx.one("SELECT position FROM history WHERE kind='raw_returned' AND identity=?", (original["id"],))[0] > stopped_at
                assert tx.one("SELECT position FROM history WHERE kind='worker_exited' AND identity=?", (original["id"],))[0] > stopped_at
            resume_started = time.monotonic()
            code, resumed = public("resume", accepted["run_id"], "--ticket", "2", "--message", "Continue the exact original Ticket.")
            assert code == 0 and resumed["host"] == "running", resumed
            assert kernel.host_request(accepted["action_id"]) == request and sibling_running() == sibling
            # The sibling has proved the original Host survives until Resume.
            # Stop it normally now rather than supervising unrelated work
            # throughout the target Ticket's complete delivery pipeline.
            code, _ = public("stop", accepted["run_id"], "--ticket", "3")
            assert code == 0
            phases = []
            def completed():
                with kernel.storage.transaction(write=False) as tx:
                    phase = tx.one("SELECT phase FROM subjects WHERE id='ticket:2'")[0]
                    gates = [row["code"] for row in tx.all("SELECT code FROM gates WHERE subject='ticket:2'")]
                if not phases or phases[-1]["phase"] != phase:
                    phases.append({"phase": phase, "since_run_seconds": time.monotonic() - supervision_started,
                                   "since_resume_seconds": time.monotonic() - resume_started})
                assert not gates and not errors, (gates, errors)
                assert children[0].poll() is None or phase == "complete", (children[0].returncode, phase, output)
                return phase == "complete"
            try:
                wait_until(completed, timeout=remaining())
                serving.join(timeout=remaining())
            finally:
                with kernel.storage.transaction(write=False) as tx:
                    history = [dict(row) for row in tx.all("SELECT position,subject,kind,phase,created_at FROM history ORDER BY position DESC LIMIT 20")]
                (tmp_path / "control-timing.json").write_text(json.dumps({
                    "run_to_stop_seconds": stop_started - supervision_started,
                    "stop_to_resume_seconds": resume_started - stop_started,
                    "total_seconds": time.monotonic() - supervision_started,
                    "phases_after_resume": phases, "host_poll": children[0].poll(),
                    "manager_trace": trace, "manager_errors": [repr(error) for error in errors], "history_tail": history}))
            assert kernel.status("ticket:2")["phase"] == "complete"
            assert kernel.host_request(accepted["action_id"]) == request
            with kernel.storage.transaction(write=False) as tx:
                assert tx.one("SELECT count(*) FROM hosts")[0] == 1
                assert tx.one("SELECT count(*) FROM invocations WHERE attempt=?", (attempt,))[0] == 1
                assert tx.one("SELECT thread FROM invocations WHERE id=?", (original["id"],))[0] == original["thread"]
                assert tx.one("SELECT count(*) FROM history WHERE kind='role_consumed' AND identity=?", (attempt,))[0] == 1
                workers = tx.all("SELECT o.process_identity FROM invocation_outputs o JOIN invocations i ON i.id=o.invocation "
                                 "JOIN attempts a ON a.id=i.attempt WHERE a.subject='ticket:2'")
                assert len(workers) == 3
                assert all(json.loads(tx.get(row[0], "ticket:2"))["host_pid"] == children[0].pid for row in workers)
            assert not serving.is_alive() and not errors, errors
            assert len(commands) == 1 and trace == ["fdstore", "barrier", "host_exited", "store_closed"]


@contextmanager
def _approval_read_barrier(*, boundary=None, released=None):
    """Delay one real remote read at a recorded control boundary."""
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind("")
    listener.listen(8)
    listener.settimeout(0.1)
    address = "@" + listener.getsockname()[1:].decode()
    databases: list[Kernel] = []
    reached, release, stopped = threading.Event(), threading.Event(), threading.Event()
    errors: list[BaseException] = []

    def serve() -> None:
        try:
            while not stopped.is_set():
                try:
                    channel, _ = listener.accept()
                except TimeoutError:
                    continue
                with channel:
                    channel.settimeout(10)
                    assert channel.recv(2) == b"1"
                    if databases and not reached.is_set():
                        if boundary is None:
                            with databases[0].storage.transaction(write=False) as tx:
                                approval = tx.rows("SELECT id FROM subjects WHERE kind='final_run' AND phase='approval'")
                        else:
                            approval = boundary(databases[0])
                        if approval:
                            reached.set()
                            assert release.wait(25), "Control boundary read was never released"
                            if released is not None:
                                released.set()
                    channel.sendall(b"1")
        except BaseException as error:
            if not stopped.is_set():
                errors.append(error)

    serving = threading.Thread(target=serve)
    serving.start()
    try:
        yield address, databases, reached, release
    finally:
        release.set()
        stopped.set()
        serving.join(timeout=2)
        listener.close()
        assert not serving.is_alive() and not errors, errors


def _hold_host_operation_return(directory: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Offer committed operation returns to the existing control barrier.

    A quiescent approval needs no extra source poll. Instrument only the actual
    Host process at the public operation seam; keep its command, Core execution
    and returned Step intact. Other Python processes retain their normal entry.
    """
    hook = directory / "host-return-hook"
    hook.mkdir()
    (hook / "sitecustomize.py").write_text("""import os, sys
if sys.orig_argv[1:3] == ["-m", 'agent_run.runtime.executor']:
    import socket
    from agent_run.core.operation_runner import OperationRunner
    original_run_once = OperationRunner.run_once
    def observe_return(self, *, agent_capacity=1):
        step = original_run_once(self, agent_capacity=agent_capacity)
        if step.kind in {"progress", "approval"} and step.call is None:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
                channel.settimeout(25)
                channel.connect(chr(0) + os.environ["ENTRY_APPROVAL_CONTROL"][1:])
                channel.sendall(b"1")
                channel.shutdown(socket.SHUT_WR)
                assert channel.recv(1) == b"1"
        return step
    OperationRunner.run_once = observe_return
""")
    monkeypatch.setenv("PYTHONPATH", str(hook) + os.pathsep + os.environ.get("PYTHONPATH", ""))


def test_public_revise_repairs_the_real_final_pr_from_original_feedback_in_the_same_managed_host(
    git_repo: Path, tmp_path: Path, monkeypatch,
) -> None:
    message = "请补充兼容性证据。\r\nPreserve this exact maintainer message."
    classification = """schema = json.loads(Path(sys.argv[sys.argv.index('--output-schema') + 1]).read_text())
role = 'review' if 'checks' in schema['properties'] else 'publication' if 'commit_message' in schema['properties'] else 'development'
"""
    codex = _CODEX[:_CODEX.index("if prompt.startswith")] + classification + _CODEX[_CODEX.index("thread = "):]
    # A fresh backend session has its own identity even in the same checkout.
    # The original resume branch still returns the exact requested identity.
    codex = codex.replace("'managed-entry-' + role)", "'managed-entry-' + role + '-' + os.urandom(16).hex())")
    codex = codex.replace("'head': head}", "'head': head, 'prompt': prompt}")
    codex = codex.replace("content = 'Implementation", """if role == 'development' and Path.cwd().name == 'run-publication':
    assert os.environ['ENTRY_REVISION_MESSAGE'] in prompt
    requirement = json.loads(subprocess.check_output(['gh', 'issue', 'view', '1', '--repo', 'example/project',
        '--json', 'number,title,body'], text=True))
    assert requirement['body'] == 'Deliver each ready Ticket using the exact recorded requirements.'
    Path('revision-evidence.txt').write_text(json.dumps(os.environ['ENTRY_REVISION_MESSAGE'], ensure_ascii=False) + chr(10))
content = 'Implementation""")
    codex = codex.replace("if role == 'development':\n", """if Path('revision-evidence.txt').is_file():
    content += 'Maintainer compatibility evidence implemented.' + chr(10)
if role == 'development':
""")
    with _approval_read_barrier() as (address, databases, reached, release):
        _hold_host_operation_return(tmp_path, monkeypatch)
        monkeypatch.setenv("ENTRY_APPROVAL_CONTROL", address)
        monkeypatch.setenv("ENTRY_REVISION_MESSAGE", message)
        environment, state = _control_environment(git_repo, tmp_path, monkeypatch, codex=codex, tickets=(2,))
        with _local_manager(tmp_path / "manager", environment, host_timeout=150) as (manager, serving, errors, children, trace, commands, output):
            try:
                environment["PATH"] = str(Path(manager.systemctl).parent) + os.pathsep + environment["PATH"]
                def public(*arguments: str):
                    result = subprocess.run([sys.executable, "-m", 'agent_run', *arguments, "--repo", "example/project", "--json"],
                        cwd=git_repo, env=environment, text=True, capture_output=True, timeout=20)
                    return result.returncode, json.loads(result.stdout)
                preparation_started = time.monotonic()
                preparation_deadline = preparation_started + 90
                total_deadline = preparation_started + 150
                code, accepted = public("run", "1", "--no-notifications")
                assert code == 0, accepted
                command = commands[0]
                kernel = Kernel(Path(command[command.index("--database") + 1]))
                databases.append(kernel)
                # Only the original Host's external reads use the barrier.
                # This terminal's new Revise still reads the actual fresh world.
                environment.pop("ENTRY_APPROVAL_CONTROL")
                # Preparation is bounded within one overall supervision window.
                # Repair, approval and normal exit share its remaining time.
                while not reached.wait(0.1):
                    assert serving.is_alive() and time.monotonic() < preparation_deadline, (kernel.source_status(), output, errors)
                preparation_seconds = time.monotonic() - preparation_started
                (tmp_path / "control-timing.json").write_text(json.dumps({"preparation_seconds": preparation_seconds}))
                subject = kernel.source_status()["final_subject"]
                before = kernel.status(subject)
                assert before["phase"] == "approval"
                request = kernel.host_request(accepted["action_id"])
                old_budget = before["budgets"]["integration"]
                old_pr = next(pr for pr in json.loads(state.read_text())["prs"] if pr["number"] == before["pr_number"])
                with kernel.storage.transaction(write=False) as tx:
                    old_threads = [dict(row) for row in tx.all("SELECT * FROM threads ORDER BY id")]
                    original_requirements = [dict(row) for row in tx.all("SELECT * FROM requirement_versions ORDER BY issue,revision")]
                control_started = time.monotonic()
                code, revised = public("revise", accepted["run_id"], "--message", message)
                assert code == 0, revised
                assert revised["host"] == "running" and kernel.host_request(accepted["action_id"]) == request
                current = kernel.status(subject)
                assert current["phase"] == "development" and current["purpose"] == "integration"
                assert current["generation"] == before["generation"] and current["pr_number"] == old_pr["number"]
                budget = current["budgets"]["integration"]
                assert (budget["window"], budget["developments_used"], budget["reviews_used"]) == (old_budget["window"] + 1, 0, 0)
                with kernel.storage.transaction(write=False) as tx:
                    assert [dict(row) for row in tx.all("SELECT * FROM threads ORDER BY id")] == old_threads
                    assert [dict(row) for row in tx.all("SELECT * FROM requirement_versions ORDER BY issue,revision")] == original_requirements
                    assert tx.one("SELECT count(*) FROM hosts")[0] == 1
                    assert tx.one("SELECT count(*) FROM grants WHERE subject=?", (subject,))[0] == 0
                    event = tx.one("SELECT material_id FROM history WHERE kind='revision_requested' AND identity=?", (revised["action_id"],))
                    assert tx.get(event[0], subject) == message.encode()
                release.set()
                serving.join(timeout=max(0, total_deadline - time.monotonic()))
                assert not serving.is_alive() and not errors, errors
                assert children[0].returncode == 0 and len(commands) == 1, output
                assert kernel.status(subject)["phase"] == "approval"
                publication = next(Path(tmp_path / "user").glob("**/run-publication/revision-evidence.txt"))
                assert json.loads(publication.read_text()) == message
                updated = next(pr for pr in json.loads(state.read_text())["prs"] if pr["number"] == old_pr["number"])
                assert updated["head"]["sha"] != old_pr["head"]["sha"] and not updated["merged"]
                assert len(json.loads(state.read_text())["prs"]) == 2  # Original Ticket and original Final PR.
                roles = [json.loads(line) for line in (tmp_path / "roles.jsonl").read_text().splitlines()]
                feedback = [event for event in roles if message in event["prompt"] or json.dumps(message, ensure_ascii=False) in event["prompt"]]
                assert [event["role"] for event in feedback] == ["development", "review", "publication"]
                assert f"https://github.com/example/project/issues/1" in feedback[0]["prompt"]
                assert trace == ["fdstore", "barrier", "host_exited", "store_closed"]
                (tmp_path / "control-timing.json").write_text(json.dumps({"preparation_seconds": preparation_seconds,
                    "revise_to_approval_seconds": time.monotonic() - control_started,
                    "total_seconds": time.monotonic() - preparation_started}))
            finally:
                release.set()


def test_public_requeue_delivers_fresh_ticket_and_final_generations_in_the_original_managed_host(
    git_repo: Path, tmp_path: Path, monkeypatch,
) -> None:
    fresh_body = "Keep the original deliverable and record this clarified requirement in requeue-evidence.txt."
    classification = """schema = json.loads(Path(sys.argv[sys.argv.index('--output-schema') + 1]).read_text())
role = 'review' if 'checks' in schema['properties'] else 'publication' if 'commit_message' in schema['properties'] else 'development'
"""
    codex = _CODEX[:_CODEX.index("if prompt.startswith")] + classification + _CODEX[_CODEX.index("thread = "):]
    codex = codex.replace("'managed-entry-' + role)", "'managed-entry-' + role + '-' + os.urandom(16).hex())")
    codex = codex.replace("with open(os.environ['ENTRY_ROLE_TRACE'], 'a') as log:", """requirement = None
if role == 'development' and Path.cwd().name.startswith('ticket-'):
    requirement = json.loads(subprocess.check_output(['gh', 'issue', 'view', '2', '--repo', 'example/project',
        '--json', 'number,title,body'], text=True))
    if Path.cwd().name.endswith('-g2'):
        assert requirement['body'] == os.environ['ENTRY_REQUEUE_BODY']
        Path('requeue-evidence.txt').write_text(requirement['body'] + chr(10))
with open(os.environ['ENTRY_ROLE_TRACE'], 'a') as log:""")
    codex = codex.replace("'head': head}", "'head': head, 'prompt': prompt, 'requirement': requirement}")
    hold = r'''
import socket
with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as control:
    control.settimeout(30)
    control.connect('\0' + os.environ['ENTRY_WORKER_CONTROL'][1:])
    control.sendall(json.dumps({'role':role, 'thread':thread, 'checkout':str(Path.cwd()), 'prompt':prompt,
                               'requirement':requirement}).encode())
    control.shutdown(socket.SHUT_WR)
    assert control.recv(1) == b'1'
'''
    codex = codex.replace("content = 'Implementation", hold + "\ncontent = 'Implementation")

    boundary_stage = {"final_generation": None}
    released = threading.Event()

    def current_boundary(kernel):
        with kernel.storage.transaction(write=False) as tx:
            if boundary_stage["final_generation"] is not None:
                return bool(tx.rows("SELECT id FROM subjects WHERE kind='final_run' AND generation=? AND phase='approval'",
                                    (boundary_stage["final_generation"],)))
            return bool(tx.rows("SELECT id FROM subjects WHERE id='ticket:2' AND generation=1 AND phase='candidate'")) and not tx.rows(
                "SELECT id FROM leases WHERE subject='ticket:2'")

    with _worker_controls(tmp_path) as (worker_address, arrivals), _approval_read_barrier(boundary=current_boundary, released=released) as (address, databases, reached, release):
        _hold_host_operation_return(tmp_path, monkeypatch)
        monkeypatch.setenv("ENTRY_WORKER_CONTROL", worker_address)
        monkeypatch.setenv("ENTRY_APPROVAL_CONTROL", address)
        monkeypatch.setenv("ENTRY_REQUEUE_BODY", fresh_body)
        environment, state = _control_environment(git_repo, tmp_path, monkeypatch, codex=codex, tickets=(2,))
        with _local_manager(tmp_path / "manager", environment, host_timeout=180) as (manager, serving, errors, children, trace, commands, output):
            started = time.monotonic()
            deadline = started + 180
            timing = {"overall_budget_seconds": 180}
            kernel = None
            environment["PATH"] = str(Path(manager.systemctl).parent) + os.pathsep + environment["PATH"]

            def remaining():
                return max(0.01, deadline - time.monotonic())

            def public(*arguments):
                result = subprocess.run([sys.executable, "-m", 'agent_run', *arguments,
                    "--repo", "example/project", "--json"], cwd=git_repo, env=environment,
                    text=True, capture_output=True, timeout=min(20, remaining()))
                return result.returncode, json.loads(result.stdout)

            try:
                code, accepted = public("run", "1", "--no-notifications")
                assert code == 0, accepted
                command = commands[0]
                kernel = Kernel(Path(command[command.index("--database") + 1]))
                databases.append(kernel)
                environment.pop("ENTRY_APPROVAL_CONTROL")
                original_event, channel = arrivals.get(timeout=min(20, remaining()))
                assert original_event["requirement"]["body"] == "Original requirement for Ticket 2."
                channel.sendall(b"1")
                while not reached.wait(0.1):
                    assert serving.is_alive() and time.monotonic() < deadline, (kernel.source_status(), output, errors)
                preparation_seconds = time.monotonic() - started
                timing["preparation_seconds"] = preparation_seconds
                request = kernel.host_request(accepted["action_id"])
                before = kernel.status("ticket:2")
                assert before["generation"] == 1 and before["phase"] == "candidate"
                with kernel.storage.transaction(write=False) as tx:
                    original_threads = [dict(row) for row in tx.all("SELECT * FROM threads ORDER BY id")]
                    original_attempt = tx.one("SELECT id FROM attempts WHERE subject='ticket:2' AND generation=1 AND role='development'")[0]
                changed = json.loads(state.read_text())
                changed["source_graph"]["issues"]["2"]["body"] = fresh_body
                state.write_text(json.dumps(changed))
                controlled_at = time.monotonic()
                timing["ticket_control_since_run_seconds"] = controlled_at - started
                code, requeued = public("requeue", accepted["run_id"])
                assert code == 0, requeued
                assert requeued["host"] == "running" and requeued["status"] == "action_applied"
                assert kernel.host_request(accepted["action_id"]) == request
                assert kernel.status("ticket:2")["generation"] == 2
                assert kernel.status("ticket:2")["phase"] == "workspace"
                boundary_stage["final_generation"] = 1
                release.set()
                assert released.wait(min(5, remaining()))
                release.clear()
                reached.clear()
                while not reached.wait(0.1):
                    assert serving.is_alive() and time.monotonic() < deadline, (kernel.source_status(), output, errors)
                ticket_to_approval_seconds = time.monotonic() - controlled_at
                timing["ticket_requeue_to_final_approval_seconds"] = ticket_to_approval_seconds
                final = kernel.source_status()["final_subject"]
                assert final is not None and kernel.status(final)["phase"] == "approval"
                final_before = kernel.status(final)
                final_delivery = kernel.delivery(final)
                completions = kernel.source_status()["completions"]
                original_run_head = _git(tmp_path / "origin.git", "rev-parse", final_delivery.source_ref)
                changed = json.loads(state.read_text())
                changed["source_graph"]["parent"]["body"] += "\nRecheck the entire delivered Run against this clarified Parent outcome."
                state.write_text(json.dumps(changed))
                final_controlled_at = time.monotonic()
                timing["final_control_since_run_seconds"] = final_controlled_at - started
                code, final_requeued = public("requeue", accepted["run_id"])
                assert code == 0, final_requeued
                assert final_requeued["host"] == "running" and final_requeued["status"] == "action_applied"
                assert kernel.host_request(accepted["action_id"]) == request
                renewed = kernel.status(final)
                assert renewed["generation"] == final_before["generation"] + 1 and renewed["phase"] == "workspace"
                assert renewed["budgets"] == final_before["budgets"]
                assert renewed["pr_number"] == final_before["pr_number"]
                assert kernel.delivery(final).source_ref == final_delivery.source_ref
                assert kernel.delivery(final).target_ref == final_delivery.target_ref
                assert kernel.delivery(final).checkout != final_delivery.checkout
                assert kernel.source_status()["completions"] == completions
                with kernel.storage.transaction(write=False) as tx:
                    assert tx.one("SELECT count(*) FROM grants WHERE subject=?", (final,))[0] == 0
                boundary_stage["final_generation"] = renewed["generation"]
                released.clear()
                release.set()
                assert released.wait(min(5, remaining()))
                release.clear()
                reached.clear()
                while not reached.wait(0.1):
                    assert serving.is_alive() and time.monotonic() < deadline, (kernel.source_status(), output, errors)
                assert kernel.status(final)["phase"] == "approval"
                timing["final_requeue_to_approval_seconds"] = time.monotonic() - final_controlled_at
                assert _git(tmp_path / "origin.git", "rev-parse", final_delivery.source_ref) == original_run_head
                approve_started = time.monotonic()
                code, approved = public("approve", accepted["run_id"])
                assert code == 0 and approved["status"] == "action_pending" and approved["host"] == "running", approved
                assert kernel.host_request(accepted["action_id"]) == request
                with kernel.storage.transaction(write=False) as tx:
                    grant = tx.one("SELECT action,pr_number,head_sha FROM grants WHERE subject=?", (final,))
                    assert grant["action"] == approved["action_id"] and grant["pr_number"] == final_before["pr_number"]
                release.set()
                serving.join(timeout=remaining())
                assert not serving.is_alive() and not errors, errors
                assert children[0].returncode == 0 and len(commands) == 1, output
                status = kernel.status("ticket:2")
                assert status["phase"] == "complete" and status["generation"] == 2
                assert status["retired_generations"][0]["generation"] == 1
                assert len(kernel.source_status()["completions"]) == 1
                assert kernel.source_status()["completions"][0]["generation"] == 2
                assert kernel.status(final)["phase"] == "complete"
                assert kernel.status(final)["generation"] == renewed["generation"]
                assert kernel.status(final)["pr_number"] == final_before["pr_number"]
                assert kernel.status(final)["source_sha"] == original_run_head
                absent = subprocess.run(["git", "-C", str(tmp_path / "origin.git"), "show-ref", "--verify", "--quiet",
                                         final_delivery.source_ref], capture_output=True, timeout=min(3, remaining()))
                assert absent.returncode == 1  # Successful terminal cleanup retires the owned Run Branch.
                events = [json.loads(line) for line in (tmp_path / "roles.jsonl").read_text().splitlines()]
                with kernel.storage.transaction(write=False) as tx:
                    ticket_threads = {row[0] for row in tx.all(
                        "SELECT t.backend_id FROM attempts a JOIN invocations i ON i.attempt=a.id JOIN threads t ON t.id=i.thread "
                        "WHERE a.subject='ticket:2' AND a.generation=2")}
                    final_threads = {row[0] for row in tx.all(
                        "SELECT t.backend_id FROM attempts a JOIN invocations i ON i.attempt=a.id JOIN threads t ON t.id=i.thread "
                        "WHERE a.subject=? AND a.generation=?", (final, renewed["generation"]))}
                fresh = [event for event in events if event["thread"] in ticket_threads]
                assert [event["role"] for event in fresh] == ["development", "review", "publication"]
                assert fresh[0]["requirement"]["body"] == fresh_body
                assert f"https://github.com/example/project/issues/2" in fresh[0]["prompt"]
                assert fresh[0]["thread"] != original_event["thread"]
                assert fresh[1]["checkout"] != fresh[0]["checkout"]  # Review keeps its original isolated checkout.
                fresh_final = [event for event in events if event["thread"] in final_threads]
                assert [event["role"] for event in fresh_final] == ["review", "publication"]
                with kernel.storage.transaction(write=False) as tx:
                    assert all(dict(tx.one("SELECT * FROM threads WHERE id=?", (thread["id"],))) == thread for thread in original_threads)
                    assert tx.one("SELECT count(*) FROM history WHERE kind='role_consumed' AND identity=?", (original_attempt,))[0] == 1
                    assert tx.one("SELECT count(*) FROM hosts")[0] == 1
                    workers = tx.all("SELECT a.generation,o.process_identity FROM invocation_outputs o JOIN invocations i ON i.id=o.invocation "
                                     "JOIN attempts a ON a.id=i.attempt WHERE a.subject='ticket:2'")
                    assert {row["generation"] for row in workers} == {1, 2}
                    assert all(json.loads(tx.get(row["process_identity"], "ticket:2"))["host_pid"] == children[0].pid for row in workers)
                assert _git(tmp_path / "origin.git", "show", original_run_head + ":requeue-evidence.txt") == fresh_body
                assert json.loads(state.read_text())["source_graph"]["issues"]["2"]["state"] == "CLOSED"
                assert len(json.loads(state.read_text())["prs"]) == 2
                assert next(pr for pr in json.loads(state.read_text())["prs"] if pr["number"] == final_before["pr_number"])["merged"]
                code, completed = public("status", accepted["run_id"])
                assert code == 0 and completed["status"] == "completed", completed
                assert trace == ["fdstore", "barrier", "host_exited", "store_closed"]
                timing["approve_to_complete_seconds"] = time.monotonic() - approve_started
            finally:
                release.set()
                timing.update(total_seconds=time.monotonic() - started,
                              host_poll=children[0].poll() if children else None,
                              manager_trace=trace, manager_errors=[repr(error) for error in errors])
                if kernel is not None:
                    with kernel.storage.transaction(write=False) as tx:
                        timing["history_tail"] = [dict(row) for row in tx.all(
                            "SELECT position,subject,kind,phase,created_at FROM history ORDER BY position DESC LIMIT 20")]
                (tmp_path / "control-timing.json").write_text(json.dumps(timing))


FINDING = "Remove obsolete-extra.txt from the final diff; keep the requested implementation."
EXTRA = "This earlier Candidate file is outside the requested scope.\n"


def test_public_ticket_repair_deletes_an_earlier_candidate_file_without_a_human_blocker(
    git_repo: Path, tmp_path: Path, monkeypatch,
) -> None:
    # The fake CLI edits files and returns raw role artifacts. The installed
    # Controller still owns staging, commits, selection, Review and publication.
    classification = """schema = json.loads(Path(sys.argv[sys.argv.index('--output-schema') + 1]).read_text())
role = 'review' if 'checks' in schema['properties'] else 'publication' if 'commit_message' in schema['properties'] else 'development'
"""
    codex = _CODEX[:_CODEX.index("if prompt.startswith")] + classification + _CODEX[_CODEX.index("thread = "):]
    codex = codex.replace("'managed-entry-' + role)", "'managed-entry-' + role + '-' + os.urandom(16).hex())")
    codex = codex.replace("'head': head}", "'head': head, 'prompt': prompt}")
    codex = codex.replace("if role == 'development':\n", f"""if role == 'development':
    extra = Path('obsolete-extra.txt')
    if Path('managed-delivery.txt').exists():
        assert {FINDING!r} in prompt, 'The original Finding did not reach the repair Worker'
        assert extra.read_text() == {EXTRA!r}
        extra.unlink()
    else:
        extra.write_text({EXTRA!r})
""")
    codex = codex.replace("'status': 'pass', 'evidence':",
        "'status': 'fail' if lane == 'spec' and Path('obsolete-extra.txt').exists() else 'pass', 'evidence':")
    codex = codex.replace("'findings': []}",
        f"'findings': [{FINDING!r}] if lane == 'spec' and Path('obsolete-extra.txt').exists() else []}}")
    environment, state = _control_environment(git_repo, tmp_path, monkeypatch, codex=codex, tickets=(2,))
    origin = tmp_path / "origin.git"
    base = _git(origin, "rev-parse", "refs/heads/main")
    deadline = time.monotonic() + 120
    with _local_manager(tmp_path / "manager", environment, host_timeout=120) as (
        manager, serving, errors, children, trace, commands, output,
    ):
        environment["PATH"] = str(Path(manager.systemctl).parent) + os.pathsep + environment["PATH"]

        def public(*arguments: str):
            result = subprocess.run([sys.executable, "-m", "agent_run", *arguments,
                "--repo", "example/project", "--json"], cwd=git_repo, env=environment,
                text=True, capture_output=True, timeout=min(20, max(0.1, deadline - time.monotonic())))
            assert result.returncode == 0, (result.stdout, result.stderr)
            return json.loads(result.stdout)

        accepted = public("run", "1", "--no-notifications")
        assert accepted["result"] == "accepted"
        serving.join(timeout=max(0, deadline - time.monotonic()))
        assert not serving.is_alive() and not errors, (errors, output)
        assert len(commands) == len(children) == 1 and children[0].returncode == 0
        assert trace == ["fdstore", "barrier", "host_exited", "store_closed"]
        status = public("status", accepted["run_id"])
        history = public("history", accepted["run_id"])["history"]
        ticket, = (job for job in status["subjects"] if job["kind"] == "ticket")
        assert ticket["issue"] == 2 and ticket["phase"] == "complete" and ticket["generation"] == 1
        assert ticket["gates"] == [] and ticket["pending_attempt"] is None and ticket["pending_writes"] == []
        assert ticket["delivery_cleanup"]["state"] == "complete"
        budget, = (row for row in ticket["budgets"] if row["purpose"] == "requirements")
        assert (budget["window"], budget["developments_used"], budget["reviews_used"]) == (1, 2, 2)
        reviews = [row for row in history["attempts"] if row["subject"] == ticket["subject"] and row["role"] == "review"]
        reviews.sort(key=lambda row: row["ordinal"])
        assert [row["outcome"] for row in reviews] == ["fail", "pass"]
        assert reviews[0]["result"]["checks"]["spec"]["findings"] == [FINDING]
        assert reviews[1]["result"]["checks"]["spec"]["findings"] == []
        first, repaired = (row["candidate_sha"] for row in reviews)
        assert first != repaired
        root = Path(status["repository_root"])
        assert _git(root, "rev-list", "--parents", "-n", "1", repaired).split() == [repaired, first]
        assert _git(root, "show", first + ":obsolete-extra.txt") == EXTRA.strip()
        assert _git(root, "ls-tree", "--name-only", repaired, "--", "obsolete-extra.txt") == ""
        assert set(_git(root, "diff", "--name-only", base, first).splitlines()) == {
            "managed-delivery.txt", "obsolete-extra.txt"}
        assert _git(root, "diff", "--name-only", base, repaired).splitlines() == ["managed-delivery.txt"]
        roles = [json.loads(line) for line in (tmp_path / "roles.jsonl").read_text().splitlines()]
        developers = [row for row in roles if row["role"] == "development"]
        reviewers = [row for row in roles if row["role"] == "review"]
        assert len(developers) == 2 and developers[1]["head"] == first
        assert FINDING in developers[1]["prompt"]
        assert [row["head"] for row in reviewers[:2]] == [first, repaired]
        assert reviewers[0]["thread"] != reviewers[1]["thread"]
        remote = json.loads(state.read_text())
        ticket_pr, = (pr for pr in remote["prs"] if pr["number"] == ticket["pr_number"])
        assert ticket_pr["merged"] is True
        assert _git(origin, "diff", "--name-only", base, ticket_pr["merge_commit_sha"]).splitlines() == ["managed-delivery.txt"]
        assert remote["source_graph"]["issues"]["2"]["state"] == "CLOSED"
        assert _git(origin, "rev-parse", "refs/heads/main") == base
