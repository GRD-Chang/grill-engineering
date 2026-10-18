"""Locations and explicit setup for CLI tests' Runner-owned clones."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

from agent_run.managed_workspace import ManagedWorkspace, normalize_repository, read_git_identity
from agent_run.messages import error_message
from agent_run.paths import app_data_root
from agent_run.process_cleanup import terminate_process_group


def managed_workspace(
    repo: Path,
    *,
    extra_env: dict[str, str] | None = None,
    repository: str | None = None,
) -> ManagedWorkspace:
    if repository is None:
        fixture = repo / "github.json"
        repository = (
            str(json.loads(fixture.read_text(encoding="utf-8"))["repository"])
            if fixture.is_file() else "example/project"
        )
    environment = {**os.environ, **(extra_env or {})}
    # Path.expanduser otherwise consults the process HOME even with an explicit
    # environment. Resolve the current-user prefix and fallback in that mapping.
    data = environment.get("XDG_DATA_HOME")
    if not data or data == "~" or data.startswith("~/"):
        home = Path(environment["HOME"].rstrip("/") or "/") if "HOME" in environment else Path.home()
        if not home.is_absolute():
            raise ValueError(error_message("defaults.absolute_path", variable="XDG_DATA_HOME"))
        environment["XDG_DATA_HOME"] = str(home / ".local" / "share") if not data else str(home) + data[1:]
    canonical = normalize_repository(repository)
    return ManagedWorkspace(canonical, app_data_root(environment) / "repositories" / canonical)


def managed_state(repo: Path, *, extra_env: dict[str, str] | None = None) -> Path:
    return managed_workspace(repo, extra_env=extra_env).state_root


def managed_repo(repo: Path, *, extra_env: dict[str, str] | None = None) -> Path:
    return managed_workspace(repo, extra_env=extra_env).repository_root


def prepare_workspace(
    repo: Path,
    *,
    extra_env: dict[str, str] | None = None,
    repository: str | None = None,
) -> ManagedWorkspace:
    workspace = managed_workspace(repo, extra_env=extra_env, repository=repository)
    if not extra_env:
        workspace.ensure(remote_url=str(repo), identity=read_git_identity(repo))
    else:
        # Creation validates the environment's managed ancestors. Keep that
        # production boundary in an isolated process when overrides are needed.
        source = str(Path(__file__).resolve().parents[2] / "src")
        environment = {**os.environ, **extra_env}
        environment["PYTHONPATH"] = os.pathsep.join(filter(None, (source, environment.get("PYTHONPATH"))))
        command = [sys.executable, "-c",
                   "import sys; from pathlib import Path; "
                   "from agent_run.managed_workspace import ManagedWorkspace, read_git_identity; "
                   "repo = Path(sys.argv[2]); "
                   "ManagedWorkspace.for_repository(sys.argv[1]).ensure(remote_url=str(repo), identity=read_git_identity(repo))",
                   workspace.repository, str(repo)]
        process = subprocess.Popen(
            command, cwd=repo, env=environment, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, start_new_session=True,
        )
        try:
            stdout, stderr = process.communicate(timeout=30)
            if process.returncode:
                raise subprocess.CalledProcessError(process.returncode, command, stdout, stderr)
        finally:
            terminate_process_group(process)
            assert process.stdout is not None and process.stderr is not None
            process.stdout.close()
            process.stderr.close()
    return workspace
