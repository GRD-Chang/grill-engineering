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
import signal
import socket
import subprocess
import sys
import threading

import pytest

from agent_run.paths import app_data_root
from agent_run.runner_lease import RunnerLeaseBusy, default_runner_lock_path, runner_management_lease
from agent_run.user_defaults import UserDefaultsStore
from agent_run_kernel import Kernel
from tests.test_kernel_host_adapter import controlled_manager
from tests.test_kernel_remote_effects import FAKE_GH


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
def _local_manager(directory: Path, environment: dict[str, str]):
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
            output.append(child.communicate(timeout=90))
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
