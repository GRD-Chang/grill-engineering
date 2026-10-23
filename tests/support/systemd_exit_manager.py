"""Isolated service-manager substitute: real waitpid and the registered exit hook.

Never contacts user systemd. The controlled Executor has no delivery authority.
Only its main command is substituted; the production adapter's credential and
post-exit command are consumed as registered, after the launcher has returned.
"""
from __future__ import annotations

import json
import array
import os
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path


def main() -> None:
    launch = json.loads(Path(sys.argv[1]).read_text())
    root = Path(launch["root"])
    properties = dict(item.split("=", 1) for item in launch["properties"])
    credentials = root / "credentials" / "executor.service"
    hook = properties.get("ExecStopPost")
    if hook:
        credentials.mkdir(mode=0o700, parents=True)
        name, source = properties["LoadCredential"].split(":", 1)
        shutil.copyfile(source.replace("%%", "%"), credentials / name)
        # Expiry belongs to startup consumption; the manager's bound copy must
        # remain usable after a long Session without waiting thirty seconds.
        material = json.loads((credentials / name).read_text())
        material["expires_at"] = 0
        (credentials / name).write_text(json.dumps(material))
        (credentials / name).chmod(0o400)
    notify = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    notify.settimeout(5)
    # Linux assigns a short, unique abstract address, independent of TMPDIR.
    # Closing the socket releases the address without a filesystem artifact.
    notify.bind("")
    notify_path = "@" + notify.getsockname()[1:].decode("ascii")
    (root / "notify-address").write_text(notify_path)
    retained_fd = None
    # Run the real environment/lease entrypoint with a controlled CLI body.
    # Readiness follows credential consumption and the fd-store barrier.
    command = [arg.replace("${NOTIFY_SOCKET}", notify_path)
               .replace("${CREDENTIALS_DIRECTORY}", str(credentials))
               .replace("${XDG_RUNTIME_DIR}", str(root))
               for arg in launch["command"]]
    module = command.index("-m")
    command[module:module + 2] = ["-c", f"EXPECTED_CREDENTIALS = {str(credentials)!r}\n" + """import sys
from agent_run.executor_bootstrap import reexec_for_runtime
reexec_for_runtime('agent_run.executor')
import json, os, sqlite3, subprocess, tempfile
from contextlib import closing
from pathlib import Path
import agent_run.cli as cli
import agent_run.executor as executor
from agent_run.codex import _worker_hidden_paths
from agent_run.executor_exit import CREDENTIAL
from agent_run.runtime.sandbox import bubblewrap_command, worker_environment
from agent_run.storage.sqlite_runtime import require_wal_runtime
require_wal_runtime()
with closing(sqlite3.connect(':memory:')):
    pass
def controlled(args):
    credentials = Path(EXPECTED_CREDENTIALS)
    assert os.environ['AGENT_RUN_INTERNAL_SYSTEMD_RUNTIME_DIRECTORY'] == str(credentials.parent.parent)
    if credentials.exists():
        assert os.environ['AGENT_RUN_INTERNAL_CREDENTIALS_DIRECTORY'] == str(credentials)
        secret = credentials / CREDENTIAL
        assert json.loads(secret.read_text())['environment']['GH_TOKEN'] == 'fixture-runner-secret'
        with tempfile.TemporaryDirectory(dir=Path.cwd(), prefix='worker-') as temporary:
            environment = worker_environment(Path(temporary) / 'gh', 'fixture-worker-token')
            assert 'AGENT_RUN_INTERNAL_CREDENTIALS_DIRECTORY' not in environment
            assert 'AGENT_RUN_INTERNAL_SYSTEMD_RUNTIME_DIRECTORY' not in environment
            probe = 'from pathlib import Path; import sys; sys.exit(Path(sys.argv[1]).exists())'
            command = bubblewrap_command(
                [sys.executable, '-c', probe, str(secret)], checkout=Path.cwd(),
                temporary=Path(temporary), writable_checkout=True, environment=environment,
                hidden_paths=_worker_hidden_paths(None, checkout=Path.cwd()))
            subprocess.run(command, env=environment, check=True, timeout=5)
        (credentials.parent.parent / 'worker-isolated').touch()
    else:
        assert 'AGENT_RUN_INTERNAL_CREDENTIALS_DIRECTORY' not in os.environ
    print('ready', flush=True)
    sys.stdin.buffer.read()
    return 0
cli.main = controlled
raise SystemExit(executor.main(sys.argv[1:]))
"""]
    executor = subprocess.Popen(
        command,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
    )
    try:
        if hook:
            for expected in (b"FDSTORE=1", b"BARRIER=1"):
                payload, ancillary, _, _ = notify.recvmsg(4096, socket.CMSG_SPACE(array.array("i").itemsize))
                assert payload.startswith(expected)
                descriptors = array.array("i")
                descriptors.frombytes(ancillary[0][2])
                descriptor, = descriptors
                if expected == b"FDSTORE=1":
                    retained_fd = descriptor
                else:
                    os.close(descriptor)
        assert executor.stdout is not None and executor.stdout.readline() == b"ready\n"
        # The parent registers the controlled Executor's fixture identity after
        # this real credential/fd-store barrier. The isolated manager needs no
        # business-state imports before observing the process and exit hook.
        pending = root / "ready.pending"
        pending.write_text(str(executor.pid))
        pending.replace(root / "ready")
        code = executor.wait()
        (root / "exit-code").write_text(str(code))
        if hook:
            deadline = time.monotonic() + 5
            while not (root / "allow-hook").exists():
                assert time.monotonic() < deadline
                time.sleep(0.01)
            native = {"SERVICE_RESULT": "signal" if code < 0 else "success" if code == 0 else "exit-code",
                      "EXIT_CODE": "killed" if code < 0 else "exited",
                      "EXIT_STATUS": signal.Signals(-code).name.removeprefix("SIG") if code < 0 else str(code),
                      "CREDENTIALS_DIRECTORY": str(credentials)}
            command = shlex.split(hook.removeprefix("-:").replace("%%", "%"))
            hook_environment = {key: value for key, value in os.environ.items() if key != 'LD_LIBRARY_PATH'}
            result = subprocess.run(command, env={**hook_environment, **native}, timeout=70, check=False)
            (root / "hook-code").write_text(str(result.returncode))
    finally:
        if executor.poll() is None:
            executor.kill()
        executor.wait(timeout=3)
        assert executor.stdin is not None and executor.stdout is not None
        executor.stdin.close()
        executor.stdout.close()
        if retained_fd is not None:
            os.close(retained_fd)
        notify.close()
        if credentials.exists():
            shutil.rmtree(credentials.parent)


if __name__ == "__main__":
    main()
