from __future__ import annotations

import os
import signal
import shutil
import subprocess
import time
from pathlib import Path
from urllib.parse import urlsplit


class WorkerSandboxError(RuntimeError):
    pass


def worker_environment(
    gh_config: Path, github_read_token: str
) -> dict[str, str]:
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
        }
    }
    gh_config.mkdir(parents=True, exist_ok=True)
    environment.update(
        {
            "GH_CONFIG_DIR": str(gh_config),
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


def bubblewrap_command(
    command: list[str],
    *,
    checkout: Path,
    temporary: Path,
    writable_checkout: bool,
    environment: dict[str, str],
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
    arguments.extend(["--chdir", str(checkout), "--", *command])
    return arguments


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
    timeout: int,
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
    try:
        stdout, stderr = process.communicate(input=prompt, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        _terminate_process_group(process)
        raise WorkerSandboxError("Codex worker timed out") from error
    except BaseException:
        _terminate_process_group(process)
        raise
    _terminate_process_group(process)
    return subprocess.CompletedProcess(
        arguments, process.returncode, stdout, stderr
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
