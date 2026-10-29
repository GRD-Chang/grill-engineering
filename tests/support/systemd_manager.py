"""Controlled systemd responses and sysfs; not a live manager.

The helpers execute normally, while unit/cgroup facts are synthetic.
"""
import array
from collections import deque
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sys
import signal
import socket
import stat
import subprocess
import threading
import time

import pytest
from support.fixture_text import replace_once

from agent_run.adapters.systemd import SystemdHost
from agent_run.model import HostLaunch
from agent_run.config.paths import app_data_root
from agent_run.config.defaults import UserDefaultsStore
from agent_run.core.kernel import Kernel
from agent_run.runtime.package_lease import RunnerLeaseBusy, default_runner_lock_path, runner_management_lease
from support.remote_effects import FAKE_GH


def controlled_manager(tmp_path: Path) -> tuple[SystemdHost, HostLaunch, Path, Path]:
    """Only systemd CLI responses/sysfs are synthetic; helpers really execute."""
    carrier = tmp_path / "environment-carrier"
    carrier.write_text("Private test environment material; never an online credential")
    carrier.chmod(0o600)
    request = HostLaunch("run-1", 3, "executor-3", "nonce-3", "agent-run-nonce-3.service",
                         (sys.executable, "-c", "print('$literal')"), carrier)
    response, calls = tmp_path / "manager-response.json", tmp_path / "helper-calls.jsonl"
    group = tmp_path / "cgroups" / "unit-group"
    group.mkdir(parents=True)
    (group / "cgroup.events").write_text("populated 0\nfrozen 0\n")
    fields = {
        "Id": request.unit_name, "Description": 'agent-run-kernel:["run-1",3,"executor-3","nonce-3"]', "LoadState": "loaded",
        "ActiveState": "active", "SubState": "exited", "InvocationID": "1" * 32,
        "ControlGroup": "/unit-group", "MainPID": "0", "ControlPID": "0", "ExecMainCode": "1",
        "ExecMainStatus": "7", "ExecMainExitTimestamp": "@1700000000.25",
        "ExecMainExitTimestampMonotonic": "1234567",
        "NFileDescriptorStore": "0",
    }
    response.write_text(json.dumps({"fields": fields, "launch_returncode": 0}))
    program = f"#!{sys.executable}\n" + f"""\
import json, sys
from pathlib import Path
response, calls = Path({str(response)!r}), Path({str(calls)!r})
with calls.open('a') as stream:
    stream.write(json.dumps([Path(sys.argv[0]).name, *sys.argv[1:]]) + '\\n')
state = json.loads(response.read_text())
if Path(sys.argv[0]).name == 'systemd-run':
    raise SystemExit(state['launch_returncode'])
if '--property=Version' in sys.argv:
    print('257')
else:
    for key, value in state['fields'].items():
        print(key + '=' + value)
    if 'next_fields' in state:
        state['fields'] = state.pop('next_fields')
        response.write_text(json.dumps(state))
"""
    for name in ("systemctl", "systemd-run"):
        executable = tmp_path / name
        executable.write_text(program)
        executable.chmod(0o700)
    host = SystemdHost(systemctl=str(tmp_path / "systemctl"), systemd_run=str(tmp_path / "systemd-run"),
                       environment={"PATH": os.defpath, "HOME": str(tmp_path)}, cgroup_root=tmp_path / "cgroups")
    return host, request, response, calls


def _git(root: Path, *arguments: str) -> str:
    return subprocess.run(["git", *arguments], cwd=root, check=True, text=True,
                          capture_output=True, timeout=10).stdout.strip()


def _executable(path: Path, program: str) -> None:
    if path.name == 'codex':
        # Preserve the synthetic program and its original exception/exit. The
        # owned trace survives production capture cleanup; never dump env/argv.
        program = '''import json as _trace_json, os as _trace_os, stat as _trace_stat, sys as _trace_sys, traceback as _traceback
_original_exception_hook = _trace_sys.excepthook
def _fixture_exception_hook(kind, value, trace):
    descriptor = -1
    try:
        raw = ''.join(_traceback.format_exception(kind, value, trace)).encode('utf-8')
        if len(raw) > 8192:
            marker = b'\\n... fixture traceback truncated ...\\n'
            raw = raw[:2048] + marker + raw[-(8192 - 2048 - len(marker)):]
        text = raw.decode('utf-8', errors='ignore')
        descriptor = _trace_os.open(_trace_os.environ['ENTRY_ROLE_TRACE'] + '.errors',
            _trace_os.O_WRONLY | _trace_os.O_CREAT | _trace_os.O_APPEND | _trace_os.O_NONBLOCK | _trace_os.O_NOFOLLOW, 0o600)
        if not _trace_stat.S_ISREG(_trace_os.fstat(descriptor).st_mode):
            raise ValueError('Synthetic trace is not a regular file')
        with _trace_os.fdopen(descriptor, 'a') as output:
            descriptor = -1
            output.write(_trace_json.dumps({'error_type': kind.__name__, 'traceback': text}) + '\\n')
    except (OSError, KeyError, ValueError):
        pass
    finally:
        if descriptor >= 0:
            try:
                _trace_os.close(descriptor)
            except OSError:
                pass
    _original_exception_hook(kind, value, trace)
_trace_sys.excepthook = _fixture_exception_hook
''' + program
    path.write_text("#!" + sys.executable + "\n" + program)
    path.chmod(0o700)


def _diagnostic_tail(path: Path) -> str:
    """Only the caller's synthetic failure trace, with a fixed read bound."""
    descriptor = -1
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return 'NonRegularFile'
        with os.fdopen(descriptor, 'rb') as stream:
            descriptor = -1
            stream.seek(0, os.SEEK_END)
            stream.seek(max(0, stream.tell() - 8192))
            return stream.read(8192).decode('utf-8', errors='replace')
    except (OSError, ValueError) as error:
        return type(error).__name__
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass  # Diagnostic cleanup cannot replace the caller's failure.


def _managed_diagnostics(database: Path, run_id: str, roles: Path) -> dict:
    """Bounded exact-Run facts on failure; no live probe, retry, raw or mutation."""
    result = {'worker_errors': _diagnostic_tail(Path(str(roles) + '.errors')), 'row_limit': 32}
    try:
        kernel = Kernel(database, run_id=run_id)
        with kernel.storage.transaction(write=False) as tx:
            result['run'] = dict(tx.one('SELECT id,stopped,blocked,epoch,integration_holder,executor,final_action FROM runs WHERE id=?', (run_id,)))
            result['subjects'] = [dict(row) for row in tx.rows(
                'SELECT id,generation,phase,purpose,current_attempt,repair_source FROM subjects WHERE run_id=? ORDER BY id LIMIT 32', (run_id,))]
            result['actions'] = [dict(row) for row in tx.rows(
                'SELECT id,kind,subject,status FROM actions WHERE run_id=? ORDER BY rowid DESC LIMIT 32', (run_id,))]
            result['gates'] = [dict(row) for row in tx.rows(
                'SELECT subject,code FROM gates WHERE run_id=? ORDER BY subject,code LIMIT 32', (run_id,))]
            # accounted is a charging fact, never a general consumption flag.
            result['attempts'] = [dict(row) for row in tx.rows(
                'SELECT id,subject,generation,role,status,result_id,current_invocation,accounted '
                'FROM attempts WHERE run_id=? ORDER BY rowid DESC LIMIT 32', (run_id,))]
            result['outputs'] = [dict(row) for row in tx.rows(
                'SELECT a.subject,a.role,a.accounted,i.id AS invocation,i.thread,t.backend_id,t.profile_revision,i.deadline_at,o.ordinal,o.dispatch_state,'
                'o.failure_kind,o.raw_id,o.started_at,o.returned_at,o.exit_code FROM invocation_outputs o '
                'JOIN invocations i ON i.run_id=o.run_id AND i.id=o.invocation '
                'JOIN attempts a ON a.run_id=i.run_id AND a.id=i.attempt '
                'LEFT JOIN threads t ON t.run_id=i.run_id AND t.id=i.thread '
                'WHERE o.run_id=? ORDER BY i.rowid DESC,o.ordinal DESC LIMIT 32', (run_id,))]
            result['activity'] = [dict(row) for row in tx.rows(
                'SELECT invocation,ordinal,identity,active,observed_at FROM worker_activity WHERE run_id=? ORDER BY rowid DESC LIMIT 32', (run_id,))]
            result['effects'] = [dict(row) for row in tx.rows(
                'SELECT id,subject,operation,status,attempts,control_action FROM effects WHERE run_id=? ORDER BY rowid DESC LIMIT 32', (run_id,))]
            result['waits'] = [dict(row) for row in tx.rows(
                'SELECT subject,kind,scope_id,started_at,deadline,retry_count,next_retry_at FROM waits WHERE run_id=? LIMIT 32', (run_id,))]
            result['leases'] = [dict(row) for row in tx.rows(
                'SELECT id,subject,invocation,executor,epoch FROM leases WHERE run_id=? ORDER BY rowid LIMIT 32', (run_id,))]
            for queue in ('work_queue', 'integration_queue'):
                result[queue] = [dict(row) for row in tx.rows(
                    'SELECT subject,ready FROM ' + queue + ' WHERE run_id=? ORDER BY ready LIMIT 32', (run_id,))]
            result['budgets'] = [dict(row) for row in tx.rows(
                'SELECT subject,purpose,window,developments_used,reviews_used FROM budgets WHERE run_id=? AND current=1 LIMIT 32', (run_id,))]
            result['history_tail'] = [dict(row) for row in tx.rows(
                'SELECT position,subject,kind,identity,phase FROM history WHERE run_id=? ORDER BY position DESC LIMIT 16', (run_id,))]
    except Exception as error:
        result['unavailable'] = {'type': type(error).__name__, 'code': getattr(error, 'code', None)}
    return result


MANAGED_NOTE_BYTES = 24 * 1024


def _resumed_ticket_completion(kernel, subject, resume_action, *, deadline, child, errors, output, on_phase):
    """Sample one resumed Ticket; durable pauses cannot hide between samples."""
    next_read = None

    def completed():
        nonlocal next_read
        now = time.monotonic()
        assert now < deadline, 'Worker did not reach the expected boundary'
        assert not errors, errors
        if child.poll() is None and next_read is not None and now < next_read:
            return False
        with kernel.storage.transaction(write=False) as tx:
            run = kernel.storage.run_id
            phase = tx.one('SELECT phase FROM subjects WHERE run_id=? AND id=?', (run, subject))[0]
            gates = [row['code'] for row in tx.rows(
                'SELECT code FROM gates WHERE run_id=? AND subject=?', (run, subject))]
            # The exact Resume receipt precedes all subsequent target pauses.
            # MAX(position) here would discard events committed before a poll.
            resumed_at = tx.one("SELECT h.position FROM history h JOIN actions a "
                "ON a.run_id=h.run_id AND a.id=h.identity WHERE h.run_id=? AND h.subject=? "
                "AND h.kind='control_applied' AND h.identity=? AND a.kind='resume'", (run, subject, resume_action))[0]
            pauses = tx.rows("SELECT position,identity FROM history WHERE run_id=? AND subject=? "
                "AND position>? AND kind='paused' ORDER BY position LIMIT 1", (run, subject, resumed_at))
        now = time.monotonic()
        assert now < deadline, 'Worker did not reach the expected boundary'
        next_read = now + 0.05
        on_phase(phase)
        assert not gates and not pauses and not errors, (gates, [tuple(row) for row in pauses], errors)
        assert child.poll() is None or phase == 'complete', (child.returncode, phase, output)
        return phase == 'complete'

    return completed


class SidecarStatTrace:
    """Bounded evidence from the original stat calls, never an extra probe."""

    def __init__(self, database: Path) -> None:
        self._paths = {str(database) + suffix: suffix for suffix in ('-wal', '-shm', '-journal')}
        self._rows: deque[dict] = deque(maxlen=16)
        self._lock = threading.Lock()
        self._phase = 'completion-poll'
        self._count = 0

    def phase(self, value: str) -> None:
        with self._lock:
            self._phase = value[:64]

    def _record(self, row: dict) -> None:
        # Diagnostics cannot replace the original stat return or exception.
        try:
            with self._lock:
                self._count += 1
                self._rows.append(row)
        except BaseException:
            pass

    def wrap(self, original):
        def traced(path, *args, **kwargs):
            try:
                suffix = self._paths.get(str(path))
            except BaseException:
                return original(path, *args, **kwargs)
            if suffix is None:
                return original(path, *args, **kwargs)
            try:
                with self._lock:
                    phase = self._phase
                row = {'suffix': suffix, 'phase': phase, 'thread': threading.get_ident(),
                       'monotonic_ns': time.monotonic_ns(),
                       'follow_symlinks': kwargs.get('follow_symlinks', True)}
            except BaseException:
                return original(path, *args, **kwargs)
            try:
                metadata = original(path, *args, **kwargs)
            except BaseException as error:
                try:
                    row.update(outcome='raise', error=type(error).__name__, errno=getattr(error, 'errno', None))
                    self._record(row)
                except BaseException:
                    pass
                raise
            try:
                row.update(outcome='return', dev=metadata.st_dev, ino=metadata.st_ino, mode=metadata.st_mode,
                           nlink=metadata.st_nlink, size=metadata.st_size)
                self._record(row)
            except BaseException:
                pass
            return metadata
        return traced

    def note(self, error: BaseException) -> None:
        try:
            with self._lock:
                facts = {'sidecar_stat_trace': list(self._rows), 'total_calls': self._count,
                         'retained_calls': len(self._rows)}
            payload = json.dumps(facts, separators=(',', ':'))
            if len(payload.encode()) > 8192:
                payload = '{"sidecar_stat_trace":"note_byte_limit"}'
            error.add_note(payload)
        except BaseException:
            pass

    @contextmanager
    def capture(self, monkeypatch, *, finish):
        with monkeypatch.context() as patch:
            patch.setattr(Path, 'stat', self.wrap(Path.stat))
            failure = None
            try:
                yield
            except BaseException as error:
                failure = error
                # Freeze the failing observation before timing cleanup can
                # overwrite the bounded ring with unrelated later reads.
                self.note(error)
                raise
            finally:
                try:
                    self.phase('timing-finally')
                except BaseException:
                    pass
                try:
                    finish()
                except BaseException as cleanup_error:
                    if failure is None:
                        self.note(cleanup_error)
                        raise
                    try:
                        failure.add_note('Managed timing evidence unavailable: ' + type(cleanup_error).__name__)
                    except BaseException:
                        pass


def _managed_failure_note(error: BaseException, *, stage: str, database: Path,
                          run_id: str, roles: Path) -> None:
    """One bounded structured note; never replace the original failure/cleanup."""
    try:
        facts = _managed_diagnostics(database, run_id, roles)
        note = {'stage': stage, 'run_id': run_id, 'facts': facts, 'omitted_rows': {}}
        for limit in (32, 8, 4, 1, 0):
            for key, value in facts.items():
                if isinstance(value, list) and len(value) > limit:
                    note['omitted_rows'][key] = note['omitted_rows'].get(key, 0) + len(value) - limit
                    facts[key] = value[:limit]
            payload = json.dumps(note, ensure_ascii=False, separators=(',', ':'))
            if len(payload.encode('utf-8')) <= MANAGED_NOTE_BYTES:
                break
        else:
            payload = json.dumps({'stage': stage[:128], 'snapshot_omitted': 'note_byte_limit'})
        error.add_note(payload)
    except BaseException as unavailable:
        # Diagnostic failure is secondary, including interruption of the read.
        error.add_note('Managed snapshot unavailable: ' + type(unavailable).__name__)


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
            if isinstance(error, subprocess.TimeoutExpired):
                output.append((error.stdout or b'', error.stderr or b''))
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
    clone = ('import sys\nsys.path.insert(0, ' + repr(str(Path(__file__).parent.parent)) + ')\n'
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
    gh = replace_once(gh, "v['issueId']=='issue-node-2'",
        "v['issueId'] in {f\"issue-node-{issue['number']}\" for issue in "
        "(d['source_graph']['parent'], *d['source_graph']['issues'].values())}")
    gh = replace_once(gh, "'number':17,'state':'open'", "'number':17+len(d['prs']),'state':'open'")
    # The canonical gh fixture already filters exact head/base query fields.
    # Do not silently attempt to replace its retired unfiltered list branch.
    gh = replace_once(gh, "elif endpoint.startswith('repos/example/project/pulls'):\n",
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
    gh = replace_once(gh, closed_event, closed_event + ";d.setdefault('events_by_issue',{}).setdefault(str(number),[]).append(d['events'][-1])")
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
    events=d.setdefault('events_by_issue',{}).setdefault(str(number),[])
    if endpoint.endswith('/events'):result=[events] if '--slurp' in args else events
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
                                approval = tx.rows("SELECT id FROM subjects WHERE run_id=? AND kind='final_run' AND phase='approval'", (databases[0].storage.run_id,))
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


def _hold_host_operation_return(directory: Path, monkeypatch: pytest.MonkeyPatch, *,
                                hold: bool = True, observe_close: bool = False) -> None:
    """Offer committed operation returns to the existing control barrier.

    A quiescent approval needs no extra source poll. Instrument only the actual
    Host process at the public operation seam; keep its command, Core execution
    and returned Step intact. Other Python processes retain their normal entry.
    """
    hook = directory / "host-return-hook"
    hook.mkdir()
    source = """import os, sys
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
"""
    if not hold:
        source = 'import sys\n'
    if observe_close:
        source += ("\nif sys.orig_argv[1:3] == ['-m', 'agent_run.runtime.executor']:\n"
                   f"    sys.path.insert(0, {str(Path(__file__).parent.parent)!r})\n"
                   "    from pathlib import Path\n"
                   "    from support.host_close_trace import install\n"
                   f"    install(Path({str(hook / 'close-trace.jsonl')!r}))\n")
    (hook / "sitecustomize.py").write_text(source)
    monkeypatch.setenv("PYTHONPATH", str(hook) + os.pathsep + os.environ.get("PYTHONPATH", ""))
