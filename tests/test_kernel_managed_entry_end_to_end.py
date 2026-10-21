"""Public run through a real local Host/Worker chain and controlled external I/O.

The manager really transfers FDs and waits for the exact Host. It is not a live
user-systemd service; the shared test cgroup cannot prove a released Host epoch.
"""
from __future__ import annotations

import array
from contextlib import contextmanager
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

from agent_run.paths import app_data_root
from agent_run.runner_lease import RunnerLeaseBusy, default_runner_lock_path, runner_management_lease
from agent_run.user_defaults import UserDefaultsStore
from agent_run_kernel import Kernel
from tests.test_kernel_host_adapter import controlled_manager
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
    from agent_run_kernel import lifecycle_cli

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
    gh = r'''import os, subprocess, sys
if sys.argv[1:3] == ['repo', 'clone']:
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
    active.symlink_to(Path(lifecycle_cli.__file__).resolve().parents[2], target_is_directory=True)
    environment = dict(os.environ)
    with _local_manager(tmp_path / "manager", environment) as (manager, serving, errors, children, trace, commands, output):
        monkeypatch.setenv("PATH", str(Path(manager.systemctl).parent) + os.pathsep + environment["PATH"])
        # The same sealed terminal PATH must reach both the CLI and the Host.
        environment["PATH"] = os.environ["PATH"]
        result = subprocess.run([sys.executable, "-m", "agent_run_kernel", "run", "1", "--repo", "example/project",
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
        assert command[command.index("-m") + 1] == "agent_run_kernel.executor"
        database = Path(command[command.index("--database") + 1])
        kernel = Kernel(database)
        assert kernel.status("ticket:2")["phase"] == "complete"
        assert kernel.status("ticket:3")["phase"] != "complete"
        assert kernel.scheduling()["active"] == []
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
        comments = [index for index, row in enumerate(writes) if row[1].endswith("/comments")]
        closes = [index for index, row in enumerate(writes) if row[1] == "repos/example/project/issues/2"]
        assert len(merges) == len(comments) == len(closes) == 1
        assert merges[0] < comments[0] < closes[0]
        with runner_management_lease(default_runner_lock_path(environment)):
            pass
        assert _git(git_repo, "status", "--porcelain=v1") == ""


@contextmanager
def _worker_controls(directory: Path):
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
                if event["role"] == "development" and name in {"ticket-2", "ticket-3"} and name not in seen:
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
    from agent_run_kernel import lifecycle_cli
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
    clone = r'''import json, os, subprocess, sys
from pathlib import Path
if sys.argv[1:3] == ['issue', 'view']:
    number = sys.argv[3]
    source = json.loads(Path(os.environ['REMOTE_TEST_STATE']).read_text())
    print(json.dumps(source['source_graph']['parent'] if number == '1' else source['source_graph']['issues'][number]))
    raise SystemExit(0)
if sys.argv[1:3] == ['repo', 'clone']:
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
    gh = FAKE_GH.replace("d['issue_state']=body['state'];",
        "d['source_graph']['issues']['2']['state']=body['state'].upper();d['issue_state']=body['state'];")
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
    _executable(binary / "gh", clone + gh)
    _executable(binary / "codex", codex)
    for name, value in {"PATH": str(binary) + os.pathsep + os.environ["PATH"], "REMOTE_TEST_STATE": str(state),
                        "REMOTE_TEST_CALLS": str(calls), "REMOTE_TEST_BARE": str(remote), "ENTRY_ROLE_TRACE": str(roles),
                        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/fixture-terminal/bus"}.items():
        monkeypatch.setenv(name, value)
    UserDefaultsStore().configure(language="en", profile={"preset": "economy"}, policy={"ticket_development_workers": 2})
    active = app_data_root() / "active" / "current"
    active.parent.mkdir(parents=True)
    active.symlink_to(Path(lifecycle_cli.__file__).resolve().parents[2], target_is_directory=True)
    return dict(os.environ), state


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
                result = subprocess.run([sys.executable, "-m", "agent_run_kernel", *arguments, "--repo", "example/project", "--json"],
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
                from agent_run.process_binding import _process_start_token
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
def _approval_read_barrier():
    """Delay one real remote read after the durable Final approval boundary."""
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
                        with databases[0].storage.transaction(write=False) as tx:
                            approval = tx.rows("SELECT id FROM subjects WHERE kind='final_run' AND phase='approval'")
                        if approval:
                            reached.set()
                            assert release.wait(25), "Final approval read was never released"
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
        monkeypatch.setenv("ENTRY_APPROVAL_CONTROL", address)
        monkeypatch.setenv("ENTRY_REVISION_MESSAGE", message)
        environment, state = _control_environment(git_repo, tmp_path, monkeypatch, codex=codex, tickets=(2,))
        with _local_manager(tmp_path / "manager", environment, host_timeout=150) as (manager, serving, errors, children, trace, commands, output):
            try:
                environment["PATH"] = str(Path(manager.systemctl).parent) + os.pathsep + environment["PATH"]
                def public(*arguments: str):
                    result = subprocess.run([sys.executable, "-m", "agent_run_kernel", *arguments, "--repo", "example/project", "--json"],
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
