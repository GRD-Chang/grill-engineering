"""Resolve a CLI repository selector without writing its source checkout."""
from __future__ import annotations

import os
from pathlib import Path

from agent_run.messages import error_message
from agent_run.git import GitError, GitRepository
from agent_run.github import GhGitHubReader
from agent_run.github_fixture import FixtureGitHubReader
from agent_run.managed_workspace import ManagedWorkspace, read_git_identity
from agent_run.run_locator import RunLocatorError


def selected_workspace(
    repository: str | None,
    fixture: str | None = None,
) -> ManagedWorkspace:
    if os.environ.get("GH_HOST", "github.com").lower() not in {"", "github.com"}:
        raise GitError(error_message("cli.error.github_host"))
    reader = (
        FixtureGitHubReader(Path(fixture))
        if fixture else GhGitHubReader(working_directory=Path.cwd())
    )
    identity = repository or reader.repository_hint()
    if not identity:
        raise RunLocatorError(
            "run_selector_context",
            error_message("cli.error.workspace_repository"),
        )
    return ManagedWorkspace.for_repository(identity)


def open_workspace(
    repository: str | None,
    fixture: str | None,
    *,
    create: bool,
) -> GitRepository:
    workspace = selected_workspace(repository, fixture)
    if create:
        # Fixture mode substitutes only the remote transport. It still creates
        # an independent clone and exercises the same ownership boundary.
        remote = None
        if fixture and not workspace.repository_root.exists():
            source = GitRepository.discover(Path.cwd())
            remote = str(source.root)
        identity = read_git_identity(Path.cwd()) if not workspace.root.exists() else None
        return workspace.ensure(remote_url=remote, identity=identity)
    if not workspace.root.exists():
        raise RunLocatorError(
            "run_selector_not_found", error_message("cli.error.workspace_missing")
        )
    return workspace.open()


def selected_repository_root(
    repository: str | None, fixture: str | None = None,
) -> Path | None:
    """Return a deterministic location; reads never create a workspace."""
    try:
        return selected_workspace(repository, fixture).repository_root
    except (GitError, RunLocatorError):
        return None
