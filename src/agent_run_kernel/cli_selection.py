"""Exact, read-only CLI Run selection without importing command dispatchers."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import subprocess
from typing import Any

from agent_run.git_errors import GitError
from agent_run.managed_workspace import ManagedWorkspace, normalize_repository
from agent_run.paths import app_data_root
from agent_run.task_identity import TaskKey

from .query import QueryError, discover, task_binding, task_published
from .task_location import read_task_bindings


def _repository(value: str) -> str:
    try:
        return normalize_repository(value)
    except GitError as error:
        raise QueryError("run_locator_invalid", "Repository must be OWNER/NAME") from error


def _cwd_repository() -> str | None:
    try:
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0"})
        result = subprocess.run(["git", "remote", "get-url", "origin"], capture_output=True, text=True,
                                timeout=3, check=False, env=env)
        if result.returncode or len(result.stdout) > 4096:
            return None
        url = result.stdout.strip().removesuffix(".git")
        match = re.fullmatch(r"(?:https?://[^/]+/|ssh://[^/]+/|[^/@]+@[^:]+:)([^/]+/[^/]+)", url)
        return _repository(match[1]) if match else None
    except (OSError, subprocess.SubprocessError, QueryError):
        return None


def _managed_state_roots(repository: str | None) -> list[Path]:
    """Locate durable state without walking clones, worktrees or installations."""
    base = app_data_root() / "repositories"

    def directory(path: Path) -> bool:
        if path.is_symlink():
            raise QueryError("run_locator_invalid", f"Symbolic managed state path: {path}")
        if not path.exists():
            return False
        if not path.is_dir():
            raise QueryError("run_locator_invalid", f"Managed state path is not a directory: {path}")
        return True

    if repository is not None:
        owner_name, repository_name = repository.split("/")
        paths = (base, base / owner_name, base / owner_name / repository_name,
                 base / owner_name / repository_name / "state")
        for path in paths:
            directory(path)
        return [paths[-1]]
    if not directory(base):
        return []
    roots = []
    visited = 0
    for owner in base.iterdir():
        if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_-]*", owner.name):
            continue
        if not directory(owner):
            continue
        for checkout in owner.iterdir():
            visited += 1
            if visited > 4096:
                raise QueryError("query_limit", "Too many managed repositories; provide --repo or --state-dir")
            try:
                _repository(owner.name + "/" + checkout.name)
            except QueryError:
                continue
            if directory(checkout):
                directory(checkout / "state")
                roots.append(checkout / "state")
    return roots


def _candidates(parsed: argparse.Namespace) -> list[dict[str, Any]]:
    published: dict[Path, str] | None = None
    repository: str | None = None
    selected_task: TaskKey | None = None
    if parsed.state_dir is not None:
        roots = [parsed.state_dir]
    elif getattr(parsed, "data_root", None) is not None:
        roots = [parsed.data_root]
    else:
        repository = _repository(parsed.repo) if parsed.repo else None
        if repository is None and not getattr(parsed, "run_id", None):
            repository = _cwd_repository()
        roots = _managed_state_roots(repository)
        published = {}
        base = app_data_root() / "repositories"
        if repository is not None and parsed.parent is not None:
            workspace = ManagedWorkspace.for_repository(repository)
            selected_task = TaskKey(workspace.repository_root, repository, parsed.parent)
            binding = task_binding(selected_task)
            bindings = iter(()) if binding is None else iter((binding,))
        else:
            bindings = read_task_bindings()
        for run_id, database in bindings:
            exact = getattr(parsed, "run_id", None)
            if exact and run_id != exact:
                continue
            try:
                relative = database.relative_to(base)
            except ValueError:
                if selected_task is not None:
                    raise QueryError("run_locator_invalid", "Task publication is outside managed state")
                continue
            if len(relative.parts) < 4 or relative.parts[2] != "state":
                if selected_task is not None:
                    raise QueryError("run_locator_invalid", "Task publication is outside managed state")
                continue
            try:
                bound_repository = _repository(relative.parts[0] + "/" + relative.parts[1])
            except QueryError:
                continue
            if repository is not None and bound_repository != repository:
                if selected_task is not None:
                    raise QueryError("run_locator_invalid", "Task publication names another managed repository")
                continue
            scoped_root = _managed_state_roots(bound_repository)[0]
            if scoped_root not in roots:
                roots.append(scoped_root)
            path = database.resolve()
            if not path.is_relative_to(scoped_root.resolve()):
                raise QueryError("run_locator_conflict", f"Task database escaped its managed state directory: {database}")
            if path in published and published[path] != run_id:
                raise QueryError("run_locator_conflict", f"Conflicting Task publications: {path}")
            published[path] = run_id
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for root in roots:
        for candidate in discover(root, published=published, run_id=getattr(parsed, "run_id", None),
                                  repository=repository or (_repository(parsed.repo) if parsed.repo else None),
                                  parent=parsed.parent):
            if published is not None:
                exact = getattr(parsed, "run_id", None)
                if (selected_task is not None and Path(candidate["database"]).resolve() in published
                        and (candidate["repository"].lower() != selected_task.repository
                             or candidate["parent"] != selected_task.parent_number
                             or candidate["repository_root"] != str(selected_task.workspace))):
                    raise QueryError("run_locator_conflict", "Task publication and database identities differ")
                if ((repository is not None and candidate["repository"].lower() != repository)
                        or (parsed.parent is not None and candidate["parent"] != parsed.parent)
                        or (exact and candidate["run_id"] != exact)):
                    continue
                if not task_published(candidate) and not candidate["is_terminal"]:
                    continue
            result[(str(Path(candidate["database"]).resolve()), candidate["run_id"])] = candidate
            if len(result) > 256:
                raise QueryError("query_limit", "Too many local candidates; provide --state-dir")
    return list(result.values())


def _select(parsed: argparse.Namespace, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    exact = getattr(parsed, "run_id", None)
    if (parsed.parent is not None and parsed.parent < 1) or (exact and parsed.parent is not None):
        raise QueryError("run_locator_invalid", "Choose a positive --parent or an exact Run ID")
    repository = _repository(parsed.repo) if parsed.repo else None
    if repository and parsed.command != "runs" and not (exact or parsed.parent):
        raise QueryError("run_locator_invalid", "--repo requires --parent or an exact Run ID")
    if not exact and repository is None:
        repository = _cwd_repository()
        if repository is None:
            raise QueryError("workspace_required", "Use --repo OWNER/NAME with --parent, or an exact Run ID with --state-dir")
    matches = [item for item in candidates if (not exact or item["run_id"] == exact)
               and (repository is None or item["repository"].lower() == repository)
               and (parsed.parent is None or item["parent"] == parsed.parent)]
    if parsed.command == "runs":
        return sorted(matches, key=lambda item: (item["repository"], item["parent"], item["run_id"]))
    if (not exact and parsed.parent is not None and parsed.state_dir is None
            and getattr(parsed, "data_root", None) is None):
        current = [item for item in matches if task_published(item)]
        if current:
            matches = current
    if not exact and parsed.parent is None:
        matches = [item for item in matches if not item["is_terminal"]]
    if not matches:
        raise QueryError("run_locator_missing", "No matching Run; use runs to inspect candidates")
    if len(matches) != 1:
        raise QueryError("run_locator_conflict", "Multiple Runs or clones match; provide an exact Run ID and state directory", candidates=matches)
    return matches
