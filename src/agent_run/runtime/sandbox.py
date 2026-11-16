from __future__ import annotations

import codecs
import shutil
import subprocess
from pathlib import Path
from typing import Callable, Sequence
from urllib.parse import urlsplit


_JSONL_LINE_BYTES = 64 * 1024


class WorkerSandboxError(RuntimeError):
    pass


class _BoundedJsonlStream:
    """Deliver complete bounded JSONL lines to the invocation capture."""

    def __init__(
        self,
        *,
        line_bytes: int = _JSONL_LINE_BYTES,
        on_line: Callable[[str], None] | None = None,
    ) -> None:
        self.line_bytes = line_bytes
        self.on_line = on_line
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._line = ""
        self._discarding_line = False

    def feed(self, chunk: bytes) -> None:
        self._consume(self._decoder.decode(chunk))

    def finish(self) -> None:
        self._consume(self._decoder.decode(b"", final=True), final=True)

    def _consume(self, value: str, *, final: bool = False) -> None:
        for part in value.splitlines(keepends=True):
            terminated = part.endswith(("\n", "\r"))
            content = part.rstrip("\r\n") if terminated else part
            if not self._discarding_line:
                candidate = self._line + content
                if len(candidate.encode("utf-8")) <= self.line_bytes:
                    self._line = candidate
                else:
                    self._line = ""
                    self._discarding_line = True
            if terminated:
                if not self._discarding_line:
                    self._handle_line(self._line)
                self._line = ""
                self._discarding_line = False
        if final and self._line and not self._discarding_line:
            self._handle_line(self._line)
            self._line = ""

    def _handle_line(self, line: str) -> None:
        if self.on_line is not None:
            self.on_line(line)


def native_worker_environment(
    environment: dict[str, str], gh_config: Path, *, host: bool
) -> dict[str, str]:
    """Pass the selected native identity, without Runner control secrets."""
    result = {
        key: value for key, value in environment.items()
        if not key.startswith(("AGENT_RUN_EXECUTOR_", "AGENT_RUN_INTERNAL_"))
        and key not in {
            "AGENT_RUN_GITHUB_APP_ID", "AGENT_RUN_GITHUB_APP_INSTALLATION_ID",
            "AGENT_RUN_GITHUB_APP_PRIVATE_KEY", "AGENT_RUN_GITHUB_READ_PERMISSIONS",
            "AGENT_RUN_GITHUB_READ_TOKEN", "DBUS_SESSION_BUS_ADDRESS",
        }
    }
    if not host:
        for key in ("SSH_AUTH_SOCK", "SSH_AGENT_PID", "GIT_ASKPASS", "SSH_ASKPASS"):
            result.pop(key, None)
        gh_config.mkdir(parents=True, exist_ok=True)
        result["GH_CONFIG_DIR"] = str(gh_config)
        result["GH_PROMPT_DISABLED"] = "1"
    return result


def bubblewrap_command(
    command: list[str],
    *,
    checkout: Path,
    temporary: Path,
    writable_checkout: bool,
    environment: dict[str, str],
    hidden_paths: Sequence[Path] = (),
    inherit_host_auth: bool = False,
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
    git_metadata_paths = _git_metadata_paths(checkout)
    for git_metadata in git_metadata_paths:
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
        if not inherit_host_auth and secret_directory.exists():
            arguments.extend(["--tmpfs", str(secret_directory)])
    git_credentials = home / ".git-credentials"
    if not inherit_host_auth and git_credentials.exists():
        arguments.extend(["--ro-bind", "/dev/null", str(git_credentials)])
    hidden = _mask_worker_paths(
        arguments,
        hidden_paths,
        checkout=checkout,
        temporary=temporary,
    )
    # Managed checkouts live below the hidden Runner data root. Reopen only
    # this invocation's paths, then restore any narrower credential/state masks.
    reopened: list[Path] = []
    for path, binding in (
        (temporary, "--bind"),
        (checkout, "--bind" if writable_checkout else "--ro-bind"),
        *((path, "--ro-bind") for path in git_metadata_paths),
    ):
        resolved = path.resolve()
        if any(parent in hidden for parent in resolved.parents):
            arguments.extend([binding, str(resolved), str(resolved)])
            reopened.append(resolved)
    if reopened:
        _mask_worker_paths(
            arguments,
            [path for path in hidden if any(root in path.parents for root in reopened)],
            checkout=checkout,
            temporary=temporary,
        )
    arguments.extend(["--chdir", str(checkout), "--", *command])
    return arguments


def _mask_worker_paths(
    arguments: list[str],
    hidden_paths: Sequence[Path],
    *,
    checkout: Path,
    temporary: Path,
) -> set[Path]:
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
        # Keep explicitly masked subdirectories available as empty private
        # mountpoints even when their parent state root is already hidden.
        arguments.extend(["--tmpfs", str(directory)])
        mounted_directories.append(directory)

    for path in sorted(paths - set(directories), key=str):
        if path.name != ".lock" and any(directory in path.parents for directory in mounted_directories):
            continue
        arguments.extend(["--ro-bind", "/dev/null", str(path)])
    return paths


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
