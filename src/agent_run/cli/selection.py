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
        for path in paths[:-1]:
            if not directory(path):
                return []
        # An existing repository with lost state is still an unreadable
        # authority. Only an absent repository is an empty discovery result.
        directory(paths[-1])
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






def repository_candidates(parsed: argparse.Namespace, routes: Sequence[RepositoryRoute]) -> list[RunLocation]:
    """Canonical metadata discovery for the sole public repository dispatcher.

    Routes come from trusted workspace assembly, never a directory-named Run
    or a JSON publication. Missing/unreadable storage propagates; this reader
    cannot initialize, create or reconcile a proposal. Public default dispatch
    uses these same typed identities.
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
        if state is not None and state.resolve() not in {route.database.resolve(), route.database.parent.resolve()}:
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
    if parsed.command != "runs" and exact is None and parent is None and repository is not None:
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
        raise QueryError("run_locator_conflict", "Multiple repository Runs match; select an exact repository and Run",
            candidates=[{"run_id": item.run_id, "database": str(item.database), "state_dir": str(item.database.parent),
                "repository": item.task.repository, "parent": item.task.parent_number,
                "repository_root": str(item.task.workspace)} for item in matches])
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
    """Read-only default route assembly for public dispatch.

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
        selected = state.absolute()
        if not selected.exists() and not selected.is_symlink():
            return ()
        database = selected if selected.is_file() or selected.is_symlink() else selected / "kernel.sqlite3"
        if not database.exists() and not database.is_symlink():
            RepositoryRoute._require_vacant_root(database)
            return ()
        route = RepositoryRoute.from_database(database)
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
