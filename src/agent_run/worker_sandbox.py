from __future__ import annotations

import os
import signal
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Sequence
from urllib.parse import urlsplit

from agent_run.worker_credentials import WORKER_GH_RESPONSE_TIMEOUT_SECONDS


class WorkerSandboxError(RuntimeError):
    pass


def worker_environment(
    gh_config: Path, github_read_token: str
) -> dict[str, str]:
    _validate_inherited_github_host()
    environment = {
        key: value
        for key, value in os.environ.items()
        if key
        not in {
            "GH_TOKEN",
            "GITHUB_TOKEN",
            "GH_ENTERPRISE_TOKEN",
            "GITHUB_ENTERPRISE_TOKEN",
            "GIT_ASKPASS",
            "SSH_ASKPASS",
            "SSH_AUTH_SOCK",
            "AGENT_RUN_GITHUB_APP_ID",
            "AGENT_RUN_GITHUB_APP_INSTALLATION_ID",
            "AGENT_RUN_GITHUB_APP_PRIVATE_KEY",
            "AGENT_RUN_GITHUB_READ_PERMISSIONS",
            "AGENT_RUN_GITHUB_READ_TOKEN",
            "GH_HOST",
        }
    }
    environment["PATH"] = _without_inherited_gh_adapters(
        environment.get("PATH", os.defpath)
    )
    gh_config.mkdir(parents=True, exist_ok=True)
    environment.update(
        {
            "GH_CONFIG_DIR": str(gh_config),
            "XDG_CONFIG_HOME": str(gh_config.parent),
            "GH_TOKEN": github_read_token,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "/bin/false",
            "SSH_ASKPASS": "/bin/false",
            "GIT_SSH_COMMAND": "ssh -o BatchMode=yes",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "credential.helper",
            "GIT_CONFIG_VALUE_0": "",
        }
    )
    return environment


def _validate_inherited_github_host() -> None:
    host = os.environ.get("GH_HOST", "").strip()
    if host and host.casefold() != "github.com":
        raise WorkerSandboxError("Worker GitHub read host is not authorized")


def _without_inherited_gh_adapters(path: str) -> str:
    """Keep a previous Worker-local `gh` adapter out of a new Worker PATH."""

    return os.pathsep.join(
        entry
        for entry in path.split(os.pathsep)
        if not (
            Path(entry).name == "gh-adapter"
            and Path(entry).parent.name.startswith("agent-run-codex-")
        )
    )


def worker_credential_environment(
    gh_config: Path, adapter_directory: Path
) -> dict[str, str]:
    """Create a Worker environment whose `gh` obtains credentials on demand."""

    environment = worker_environment(gh_config, "placeholder")
    environment.pop("GH_TOKEN", None)
    adapter_directory.mkdir(parents=True, exist_ok=True)
    return environment


def _worker_gh_targets(
    environment: dict[str, str], *, cwd: Path
) -> tuple[Path, ...]:
    """Find the real ``gh`` files that the Worker command environment may use.

    The command tool may put ``CODEX_INSTALL_DIR`` in front of the inherited
    PATH after the Worker starts.  Collecting both locations before creating
    the bubblewrap command lets the namespace cover either resolution without
    changing PATH precedence or probing the command tool at runtime.
    """

    path = environment.get("PATH", os.defpath)
    worker_cwd = cwd.absolute()
    directories = []
    for entry in path.split(os.pathsep):
        directory = Path(entry or ".")
        directories.append(
            directory if directory.is_absolute() else worker_cwd / directory
        )
    codex_install_dir = environment.get("CODEX_INSTALL_DIR")
    if codex_install_dir:
        codex_directory: Path | None
        try:
            codex_directory = Path(codex_install_dir).expanduser()
        except (OSError, RuntimeError):
            codex_directory = None
        if codex_directory is not None:
            directories.append(
                codex_directory
                if codex_directory.is_absolute()
                else worker_cwd / codex_directory
            )
    else:
        directories.append(Path.home() / ".local" / "bin")

    targets: list[Path] = []
    for directory in directories:
        candidate = directory / "gh"
        try:
            if not candidate.is_file() or not os.access(candidate, os.X_OK):
                continue
            target = candidate.resolve(strict=True)
            if not target.is_file() or not os.access(target, os.X_OK):
                continue
        except (OSError, RuntimeError):
            continue
        if target not in targets:
            targets.append(target)
    return tuple(targets)


def create_gh_access_adapter(
    adapter_directory: Path, socket_path: Path, environment: dict[str, str]
) -> Path:
    """Write the temporary Worker-local `gh` adapter without persisting a token."""

    adapter = adapter_directory / "gh"
    adapter.write_text(
        _gh_adapter_script(socket_path), encoding="utf-8"
    )
    adapter.chmod(0o700)
    return adapter


def _gh_adapter_script(socket_path: Path) -> str:
    return f'''#!/usr/bin/env python3
import json
import socket
import sys

SOCKET_PATH = {str(socket_path)!r}

def channel(arguments):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(5)
        connection.connect(SOCKET_PATH)
        payload = json.dumps({{"kind": "run", "arguments": arguments}}).encode()
        if len(payload) > 16 * 1024 * 1024:
            raise RuntimeError("Worker GitHub read request exceeded the size limit")
        connection.sendall(len(payload).to_bytes(4, "big") + payload)
        connection.settimeout({WORKER_GH_RESPONSE_TIMEOUT_SECONDS!r})
        size = int.from_bytes(read_exact(connection, 4), "big")
        if size > 16 * 1024 * 1024:
            raise RuntimeError("Worker GitHub read response exceeded the size limit")
        response = json.loads(read_exact(connection, size))
    if "error" in response:
        raise RuntimeError(response["error"])
    return response

def read_exact(connection, size):
    chunks = []
    remaining = size
    while remaining:
        chunk = connection.recv(remaining)
        if not chunk:
            raise RuntimeError("Worker GitHub credential channel closed early")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)

try:
    result = channel(sys.argv[1:])
    sys.stdout.write(result["stdout"])
    sys.stderr.write(result["stderr"])
    raise SystemExit(result["returncode"])
except (OSError, ValueError, RuntimeError) as error:
    sys.stderr.write("Worker GitHub read credential error: " + str(error) + "\\n")
    raise SystemExit(1)
'''


def bubblewrap_command(
    command: list[str],
    *,
    checkout: Path,
    temporary: Path,
    writable_checkout: bool,
    environment: dict[str, str],
    hidden_paths: Sequence[Path] = (),
    gh_adapter: Path | None = None,
    gh_targets: Sequence[Path] = (),
) -> list[str]:
    _reject_credentialed_http_remotes(checkout, environment)
    executable = shutil.which("bwrap")
    if executable is None:
        raise WorkerSandboxError(
            "bubblewrap is required to enforce the Publisher authority boundary"
        )
    arguments = [
        executable,
        "--die-with-parent",
        "--bind",
        "/",
        "/",
        "--dev",
        "/dev",
        "--unshare-pid",
        "--proc",
        "/proc",
        "--bind",
        str(temporary),
        str(temporary),
    ]
    if writable_checkout:
        arguments.extend(["--bind", str(checkout), str(checkout)])
    else:
        arguments.extend(["--ro-bind", str(checkout), str(checkout)])
    for git_metadata in _git_metadata_paths(checkout):
        arguments.extend(
            ["--ro-bind", str(git_metadata), str(git_metadata)]
        )
    codex_home = Path(
        environment.get("CODEX_HOME", str(Path.home() / ".codex"))
    ).resolve()
    if codex_home.exists():
        arguments.extend(["--bind", str(codex_home), str(codex_home)])
    home = Path.home()
    for secret_directory in (home / ".config" / "gh", home / ".ssh"):
        if secret_directory.exists():
            arguments.extend(["--tmpfs", str(secret_directory)])
    git_credentials = home / ".git-credentials"
    if git_credentials.exists():
        arguments.extend(["--ro-bind", "/dev/null", str(git_credentials)])
    _mask_worker_paths(
        arguments,
        hidden_paths,
        checkout=checkout,
        temporary=temporary,
    )
    if gh_targets:
        if gh_adapter is None:
            raise WorkerSandboxError(
                "worker_gh_binding_failed: adapter is unavailable"
            )
        for target in gh_targets:
            arguments.extend(["--ro-bind", str(gh_adapter), str(target)])
    arguments.extend(["--chdir", str(checkout), "--", *command])
    return arguments


def _mask_worker_paths(
    arguments: list[str],
    hidden_paths: Sequence[Path],
    *,
    checkout: Path,
    temporary: Path,
) -> None:
    checkout_root = checkout.resolve()
    temporary_root = temporary.resolve()
    paths: set[Path] = set()
    for candidate in hidden_paths:
        try:
            resolved = Path(candidate).expanduser().resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if resolved in {checkout_root, temporary_root}:
            continue
        paths.add(resolved)

    directories = sorted(
        (path for path in paths if path.is_dir()),
        key=lambda path: (len(path.parts), str(path)),
    )
    mounted_directories: list[Path] = []
    for directory in directories:
        if any(parent in mounted_directories for parent in directory.parents):
            continue
        arguments.extend(["--tmpfs", str(directory)])
        mounted_directories.append(directory)

    for path in sorted(paths - set(directories), key=str):
        if any(directory in path.parents for directory in mounted_directories):
            continue
        arguments.extend(["--ro-bind", "/dev/null", str(path)])


def _reject_credentialed_http_remotes(
    checkout: Path, environment: dict[str, str]
) -> None:
    repository = subprocess.run(
        ["git", "-C", str(checkout), "rev-parse", "--git-dir"],
        text=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=environment,
        check=False,
    )
    if repository.returncode != 0:
        if (checkout.absolute() / ".git").exists():
            raise WorkerSandboxError(
                "Could not inspect Worker Git remote URLs"
            )
        return
    configured = subprocess.run(
        [
            "git",
            "-C",
            str(checkout),
            "config",
            "--null",
            "--get-regexp",
            r"^remote\..*\.(url|pushurl)$",
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=environment,
        check=False,
    )
    if configured.returncode not in {0, 1}:
        raise WorkerSandboxError("Could not inspect Worker Git remote URLs")
    for record in configured.stdout.split("\0"):
        if not record:
            continue
        _key, separator, remote_url = record.partition("\n")
        if not separator:
            raise WorkerSandboxError("Could not inspect Worker Git remote URLs")
        _reject_credentialed_http_url(remote_url)
    remotes = subprocess.run(
        ["git", "-C", str(checkout), "remote"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=environment,
        check=False,
    )
    if remotes.returncode != 0:
        raise WorkerSandboxError("Could not inspect Worker Git remote URLs")
    for remote in remotes.stdout.splitlines():
        for options in (("--all",), ("--push", "--all")):
            urls = subprocess.run(
                [
                    "git",
                    "-C",
                    str(checkout),
                    "remote",
                    "get-url",
                    *options,
                    remote,
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=environment,
                check=False,
            )
            if urls.returncode != 0:
                raise WorkerSandboxError(
                    "Could not inspect Worker Git remote URLs"
                )
            for remote_url in urls.stdout.splitlines():
                _reject_credentialed_http_url(remote_url)


def _reject_credentialed_http_url(remote_url: str) -> None:
    try:
        parsed = urlsplit(remote_url)
    except ValueError:
        raise WorkerSandboxError(
            "Could not inspect Worker Git remote URLs"
        ) from None
    if parsed.scheme.lower() in {"http", "https"} and (
        parsed.username is not None or parsed.password is not None
    ):
        raise WorkerSandboxError(
            "Worker checkout contains credential-bearing HTTP(S) "
            "Git remote URL"
        )


def _git_metadata_paths(checkout: Path) -> tuple[Path, ...]:
    paths: list[Path] = []
    literal_git = checkout.absolute() / ".git"
    if literal_git.exists():
        paths.append(literal_git)
    resolved = subprocess.run(
        [
            "git",
            "-C",
            str(checkout),
            "rev-parse",
            "--path-format=absolute",
            "--git-dir",
            "--git-common-dir",
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if resolved.returncode == 0:
        paths.extend(
            Path(value).resolve()
            for value in resolved.stdout.splitlines()
            if value
        )
    return tuple(dict.fromkeys(paths))


def run_worker_process(
    arguments: list[str],
    *,
    cwd: Path,
    prompt: str,
    environment: dict[str, str],
    timeout: float,
    on_stdout_line: Callable[[str], None] | None = None,
    abort_event: threading.Event | None = None,
    abort_reason: Callable[[], str] | None = None,
    on_process_started: Callable[[int], None] | None = None,
) -> subprocess.CompletedProcess[str]:
    process = subprocess.Popen(
        arguments,
        cwd=cwd,
        env=environment,
        text=True,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    if on_process_started is not None:
        on_process_started(process.pid)
    stdin = process.stdin
    stdout_pipe = process.stdout
    stderr_pipe = process.stderr
    assert stdin is not None
    assert stdout_pipe is not None
    assert stderr_pipe is not None
    stdout_lines: list[str] = []
    stderr_parts: list[str] = []
    callback_errors: list[BaseException] = []
    reader_errors: list[BaseException] = []
    writer_errors: list[BaseException] = []

    def read_stdout() -> None:
        try:
            for line in stdout_pipe:
                stdout_lines.append(line)
                if on_stdout_line is not None and not callback_errors:
                    try:
                        on_stdout_line(line)
                    except BaseException as error:
                        callback_errors.append(error)
                        _terminate_process_group(process)
        except BaseException as error:
            reader_errors.append(error)

    def read_stderr() -> None:
        stderr_parts.append(stderr_pipe.read())

    def write_stdin() -> None:
        try:
            stdin.write(prompt)
            stdin.close()
        except BrokenPipeError:
            return
        except BaseException as error:
            writer_errors.append(error)

    stdout_reader = threading.Thread(
        target=read_stdout,
        daemon=True,
        name=f"agent-run-worker-{process.pid}-stdout",
    )
    stderr_reader = threading.Thread(
        target=read_stderr,
        daemon=True,
        name=f"agent-run-worker-{process.pid}-stderr",
    )
    stdin_writer = threading.Thread(
        target=write_stdin,
        daemon=True,
        name=f"agent-run-worker-{process.pid}-stdin",
    )
    stdout_reader.start()
    stderr_reader.start()
    stdin_writer.start()
    wait_error: BaseException | None = None
    try:
        deadline = time.monotonic() + timeout
        while process.poll() is None:
            if abort_event is not None and abort_event.is_set():
                wait_error = WorkerSandboxError(
                    abort_reason() if abort_reason is not None else "Worker read credential renewal expired"
                )
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                wait_error = WorkerSandboxError("Codex worker timed out")
                break
            try:
                process.wait(timeout=min(0.1, remaining))
            except subprocess.TimeoutExpired:
                continue
    except BaseException as error:
        wait_error = error
    finally:
        _terminate_process_group(process)
        stdin_writer.join(timeout=1)
        stdout_reader.join(timeout=1)
        stderr_reader.join(timeout=1)
        if stdout_reader.is_alive():
            stdout_pipe.close()
            stdout_reader.join(timeout=1)
        if stderr_reader.is_alive():
            stderr_pipe.close()
            stderr_reader.join(timeout=1)
    if callback_errors:
        raise callback_errors[0]
    if wait_error is not None:
        raise wait_error
    if reader_errors:
        raise reader_errors[0]
    if writer_errors:
        raise writer_errors[0]
    return subprocess.CompletedProcess(
        arguments,
        process.returncode,
        "".join(stdout_lines),
        "".join(stderr_parts),
    )


def _terminate_process_group(process: subprocess.Popen[str]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 0.2
    while time.monotonic() < deadline:
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.01)
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.poll() is None:
        process.wait()
