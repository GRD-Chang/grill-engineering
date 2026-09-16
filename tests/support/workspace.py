"""Locations and explicit setup for CLI tests' Runner-owned clones."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

from agent_run.managed_workspace import ManagedWorkspace, read_git_identity


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
    with patch.dict(os.environ, extra_env or {}):
        return ManagedWorkspace.for_repository(repository)


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
    with patch.dict(os.environ, extra_env or {}):
        workspace.ensure(remote_url=str(repo), identity=read_git_identity(repo))
    return workspace
