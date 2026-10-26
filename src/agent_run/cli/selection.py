"""Exact, read-only CLI Run selection without importing command dispatchers."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import subprocess
from typing import Any
from collections.abc import Sequence

from agent_run.errors import GitError
from agent_run.adapters.workspace import ManagedWorkspace, normalize_repository
from agent_run.config.paths import app_data_root
from agent_run.contracts.task import TaskKey

from agent_run.queries.read_facts import QueryError
from agent_run.queries.run import discover, task_binding, task_published
from agent_run.storage.tasks import read_task_bindings
from agent_run.storage.routing import RepositoryRoute, RunLocation


def _repository(value: str) -> str:
    try:
        return normalize_repository(value)
    except GitError as error:
        raise QueryError("run_locator_invalid", "Repository must be OWNER/NAME") from error


def control_selection(parsed: argparse.Namespace, *, language: str) -> argparse.Namespace:
    """The existing public numeric-Parent/exact-Run convention, without I/O."""
    from agent_run.cli.settings import _positive
    selection = argparse.Namespace(**vars(parsed))
    selection.parent = None
    if selection.run_id.isdecimal():
        selection.parent = _positive(selection.run_id, language=language)
        selection.run_id = None
    return selection


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


def repository_candidates(parsed: argparse.Namespace, routes: Sequence[RepositoryRoute]) -> list[RunLocation]:
    """Canonical metadata discovery for the inactive repository dispatcher.

    Routes come from trusted workspace assembly, never a directory-named Run
    or a JSON publication. Missing/unreadable storage propagates; this reader
    cannot initialize, create or reconcile a proposal. Public default dispatch
    still uses the current protocol until the coordinated one-way switch.
    """
    repository = _repository(parsed.repo) if parsed.repo else None
    exact = getattr(parsed, "run_id", None)
    parent = getattr(parsed, "parent", None)
    if (exact is not None and (type(exact) is not str or not exact)
            or parent is not None and (type(parent) is not int or parent < 1)
            or exact is not None and parent is not None):
        raise QueryError("run_locator_invalid", "Choose a positive Parent or one exact Run ID")
    state = getattr(parsed, "state_dir", None)
    data = getattr(parsed, "data_root", None)
    if len(routes) > 4096:
        raise QueryError("query_limit", "Too many repository routes")
    found: dict[tuple[Path, str], RunLocation] = {}
    seen: set[Path] = set()
    for route in routes:
        if repository is not None and route.repository != repository:
            continue
        if state is not None and route.database.parent.resolve() != state.resolve():
            continue
        if data is not None and not route.database.resolve().is_relative_to(data.resolve()):
            continue
        database = route.database.resolve()
        if database in seen:
            # Do not silently select between contradictory namespace claims.
            raise QueryError("run_locator_conflict", "Duplicate repository database route")
        seen.add(database)
        after = ""
        while True:
            page = route.discover(after=after, limit=128, run_id=exact, parent=parent)
            for item in page.items:
                if (exact is not None and item.run_id != exact
                        or parent is not None and item.task.parent_number != parent):
                    continue
                found[(database, item.run_id)] = item
                if len(found) > 256:
                    raise QueryError("query_limit", "Too many Runs; select an exact repository, Parent or Run")
            if not page.has_more:
                break
            if page.next_after is None or page.next_after <= after:
                raise QueryError("run_locator_conflict", "Repository discovery did not advance")
            after = page.next_after
    return list(found.values())


def select_repository(parsed: argparse.Namespace, candidates: Sequence[RunLocation]) -> list[RunLocation]:
    """Select typed metadata; every later mutation rechecks currentness in SQL."""
    exact = getattr(parsed, "run_id", None)
    parent = getattr(parsed, "parent", None)
    repository = _repository(parsed.repo) if parsed.repo else None
    if (exact is not None and (type(exact) is not str or not exact)
            or parent is not None and (type(parent) is not int or parent < 1)
            or exact is not None and parent is not None):
        raise QueryError("run_locator_invalid", "Choose a positive Parent or one exact Run ID")
    if parsed.command != "runs" and exact is None and parent is None:
        raise QueryError("run_locator_invalid", "Select an exact Run ID or Parent")
    matches = [item for item in candidates
               if (exact is None or item.run_id == exact)
               and (repository is None or item.task.repository == repository)
               and (parent is None or item.task.parent_number == parent)]
    if parsed.command == "runs":
        return sorted(matches, key=lambda item: (item.task.repository, item.task.parent_number, item.run_id))
    if exact is None:
        matches = [item for item in matches if item.current]
    if not matches:
        raise QueryError("run_locator_missing", "No matching published Run")
    if len(matches) != 1:
        raise QueryError("run_locator_conflict", "Multiple repository Runs match; select an exact repository and Run")
    return matches


def repository_route_for(location: RunLocation, routes: Sequence[RepositoryRoute]) -> RepositoryRoute:
    """Match typed identity, never a display body or a directory-named Run."""
    matches = [route for route in routes
               if route.database.resolve() == location.database.resolve()
               and (route.repository, route.repository_root) == (location.task.repository, location.task.workspace)]
    if len(matches) != 1:
        raise QueryError("run_locator_conflict", "Run metadata has no unique repository route")
    return matches[0]


def repository_target(parsed: argparse.Namespace, routes: Sequence[RepositoryRoute]) -> tuple[RepositoryRoute, RunLocation]:
    selected = select_repository(parsed, repository_candidates(parsed, routes))
    if len(selected) != 1:
        raise QueryError("run_locator_conflict", "Select one exact repository Run")
    return repository_route_for(selected[0], routes), selected[0]


def managed_repository_routes(parsed: argparse.Namespace) -> tuple[RepositoryRoute, ...]:
    """Read-only default route assembly, not yet selected by public dispatch.

    Enumerate only the existing bounded managed-repository registry. No Run
    directories, JSON publications, credentials or checkout contents are read.
    Explicit state directories use the admitted database namespace instead of
    deriving repository/Run identity from their names. Failed/old state is never
    replaced, initialized or silently treated as an empty repository.
    """
    repository = _repository(parsed.repo) if parsed.repo else None
    state = getattr(parsed, "state_dir", None)
    data = getattr(parsed, "data_root", None)
    if state is not None:
        route = RepositoryRoute.from_database(state.absolute() / "kernel.sqlite3")
        if repository is not None and route.repository != repository:
            raise QueryError("run_locator_conflict", "Selected state belongs to another repository")
        return (route,)
    if data is not None and ((data / "kernel.sqlite3").exists() or (data / "kernel.sqlite3").is_symlink()):
        route = RepositoryRoute.from_database(data.absolute() / "kernel.sqlite3")
        if repository is not None and route.repository != repository:
            raise QueryError("run_locator_conflict", "Selected state belongs to another repository")
        return (route,)
    if repository is None and data is None and not getattr(parsed, "run_id", None):
        repository = _cwd_repository()
    routes = []
    base = app_data_root() / "repositories"
    for root in _managed_state_roots(repository):
        if data is not None and not root.absolute().is_relative_to(data.absolute()):
            continue
        relative = root.relative_to(base)
        if len(relative.parts) != 3 or relative.parts[2] != "state":
            raise QueryError("run_locator_conflict", "Invalid managed repository state root")
        namespace = _repository(relative.parts[0] + "/" + relative.parts[1])
        routes.append(RepositoryRoute.for_workspace(ManagedWorkspace.for_repository(namespace)))
    if data is not None and not routes:
        # An occupied explicit root outside the known registry is not proof of
        # an empty repository. Preserve it and report refusal, never hide old
        # per-Run databases merely because the new canonical file is absent.
        RepositoryRoute._require_vacant_root(data.absolute() / "kernel.sqlite3")
    return tuple(routes)
