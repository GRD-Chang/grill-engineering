"""Run identity and bounded, read-only discovery shared by CLI consumers.

This module never admits an Action or repairs state. Command readiness and
accepted-Action attachment belong to the lifecycle caller.
"""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from agent_run.error_safety import bounded_error
from agent_run.git import GitError, GitRepository
from agent_run.managed_workspace import workspace_for_root, workspace_state_root
from agent_run.messages import error_message
from agent_run.models import same_repository
from agent_run.run_locator import MAX_LOCATOR_ENTRIES, LocatorEntry, RunLocatorError, RunLocatorIndex
from agent_run.state import StateStore
from agent_run.state_contract import IncompatibleRunStateError, require_current_run_state
from agent_run.task_control import TASK_CONTROL_PROTOCOL
from agent_run.workspace_cli import open_workspace, selected_repository_root


def load_selected_state(
    states: StateStore, run_id: str, *, repository: str | None,
    read_only: bool = False,
) -> dict[str, Any] | None:
    state = states.load_run(run_id)
    if state is None or state.get("run_id") != run_id:
        return None
    _require_supported_state(state, read_only=read_only)
    validate_repository_selector(repository, state, run_id, str(states.root))
    return state


def _require_supported_state(state: dict[str, Any], *, read_only: bool) -> None:
    try:
        require_current_run_state(state)
    except IncompatibleRunStateError:
        legacy = (
            "schema_version" in state
            or state.get("lifecycle_action_protocol") != TASK_CONTROL_PROTOCOL
        )
        if not (read_only and legacy):
            raise


def selector_records(
    *,
    repository: str | None,
    state_dir: str | None,
    fixture: str | None,
    current_root: Path | None = None,
    read_only: bool = False,
    local_only: bool = False,
) -> list[tuple[dict[str, object], dict[str, Any] | None]]:
    """Load selector candidates from the bounded index or an explicit state dir."""

    if repository is not None:
        validate_repository_name(repository)

    if state_dir:
        root_path = Path(state_dir).resolve()
        root = _repository_root_for_state_dir(root_path)
        records = read_state_directory(root_path, root, read_only=read_only)
        if repository is not None:
            records = [
                record
                for record in records
                if same_repository(record[0].get("repository"), repository)
                or record[0].get("repository") is None
            ]
        return records

    if current_root is None:
        current_root = selected_repository_root(repository, fixture)
        if current_root is None and repository is None:
            raise selector_error(
                "run_selector_context",
                error_message('cli.error.repository_required'),
                [],
            )

    locator = RunLocatorIndex.default()
    entries = locator.entries()
    if local_only and current_root is not None:
        expected_root = current_root.resolve()
        entries = [
            entry
            for entry in entries
            if Path(entry["repository_root"]).resolve() == expected_root
        ]
    records = [_read_locator_entry(entry, read_only=read_only) for entry in entries]

    # A current checkout is an already-known, bounded location.  It remains a
    # useful fallback when an older/newly interrupted Run has not completed its
    # locator registration, but it never replaces an indexed entry.
    if current_root is not None and current_root.is_dir():
        local_root = workspace_state_root(current_root.resolve())
        indexed_runs = {
            (entry["run_id"], Path(entry["state_dir"]).resolve())
            for entry in entries
        }
        local_records = read_state_directory(
            local_root, current_root.resolve(), read_only=read_only
        )
        records.extend(
            record
            for record in local_records
            if (
                str(record[0]["run_id"]),
                Path(str(record[0]["state_dir"])).resolve(),
            )
            not in indexed_runs
        )
        # Resolve local identity only after the bounded fallback is included.
        # Otherwise an unregistered local Run can silently lose other clones.
        if repository is None:
            repository = workspace_for_root(current_root.resolve()).repository
    if repository is not None:
        records = [
            record
            for record in records
            if (record[0].get("repository") is None or same_repository(record[0].get("repository"), repository))
        ]
    elif current_root is not None:
        # With no Repository identity, only this checkout is a known target.
        # Unreadable identities still need explicit disambiguation.
        records = [
            record for record in records
            if record[0].get("repository_root") == str(current_root.resolve())
            or record[1] is None
        ]
    return records


def _repository_root_for_state_dir(state_dir: Path) -> Path | None:
    """Resolve a state directory to a verified checkout when possible."""

    if state_dir.name == "state":
        root = state_dir.parent / "repository"
        try:
            if workspace_state_root(root) == state_dir:
                return root
        except (GitError, OSError):
            pass

    try:
        entries = RunLocatorIndex.default().entries()
    except RunLocatorError:
        return None
    roots = {
        Path(entry["repository_root"]).resolve()
        for entry in entries
        if Path(entry["state_dir"]).resolve() == state_dir
    }
    if len(roots) != 1:
        return None
    recorded_root = next(iter(roots))
    try:
        root = GitRepository.discover(recorded_root).root
    except (GitError, OSError):
        return None
    return root if root == recorded_root else None


def _verified_locator_checkout(
    entry: LocatorEntry,
) -> tuple[GitRepository, str | None]:
    workspace = workspace_for_root(Path(entry["repository_root"]).resolve())
    return workspace.open(), workspace.repository


def read_state_directory(
    state_dir: Path, repository_root: Path | None, *, read_only: bool = False
) -> list[tuple[dict[str, object], dict[str, Any] | None]]:
    try:
        run_ids = StateStore(state_dir).list_run_ids(limit=MAX_LOCATOR_ENTRIES)
    except ValueError as error:
        raise RunLocatorError(
            "run_locator_invalid",
            error_message('cli.error.too_many_runs' ,value0=MAX_LOCATOR_ENTRIES),
        ) from error
    repository_root_value = (
        str(repository_root.resolve()) if repository_root is not None else "unavailable"
    )
    records: list[tuple[dict[str, object], dict[str, Any] | None]] = []
    for run_id in run_ids:
        entry: LocatorEntry = {
            "run_id": run_id,
            "repository_root": repository_root_value,
            "state_dir": str(state_dir.resolve()),
            "updated_at": "",
        }
        public, state = _read_locator_entry(
            entry,
            verify_checkout=repository_root is not None,
            read_only=read_only,
        )
        records.append((public, state if repository_root is not None else None))
    return records


def _read_locator_entry(
    entry: LocatorEntry,
    *,
    verify_checkout: bool = True,
    read_only: bool = False,
) -> tuple[dict[str, object], dict[str, Any] | None]:
    run_id = entry["run_id"]
    if Path(run_id).name != run_id:
        return _candidate(entry, error=error_message("cli.error.unsafe_run_id")), None
    state_dir = Path(entry["state_dir"])
    states = StateStore(state_dir)
    state: dict[str, Any] | None = None
    state_error: str | None = None
    try:
        if not states.has_run(run_id):
            state_error = error_message("cli.error.state_missing")
        else:
            # Identity must be checked before validating the lifecycle payload:
            # a broken payload cannot hide a contradictory routing identity.
            state = states.load_run(run_id)
        if state is None:
            state_error = state_error or error_message("cli.error.run_id_mismatch")
    except (OSError, ValueError) as error:
        state_error = bounded_error(str(error))
    if state is not None and state.get("run_id") != run_id:
        return _candidate(
            entry,
            error=error_message("cli.error.run_id_mismatch"),
            identity_conflict=True,
        ), None
    if state is not None:
        repository = state.get("repository")
        parent = state.get("parent")
        parent_number = parent.get("number") if isinstance(parent, dict) else None
        if (
            (
                repository is not None
                and (
                    not isinstance(repository, str)
                    or len(repository.split("/")) != 2
                    or not all(repository.split("/"))
                    or any(character.isspace() for character in repository)
                )
            )
            or (
                parent_number is not None
                and (type(parent_number) is not int or parent_number <= 0)
            )
        ):
            return _candidate(
                entry,
                error=error_message("cli.error.identity_format"),
                identity_conflict=True,
            ), None
        if repository is None or parent_number is None:
            state_error = error_message("cli.error.identity_missing")
    if state is not None and "repository" in entry:
        parent = state.get("parent")
        if (
            not same_repository(state.get("repository"), entry["repository"])
            or not isinstance(parent, dict)
            or parent.get("number") != entry["parent_number"]
        ):
            return _candidate(
                entry,
                error=error_message("cli.error.identity_mismatch"),
                identity_conflict=True,
            ), None
    if state is not None:
        try:
            _require_supported_state(state, read_only=read_only)
        except IncompatibleRunStateError as error:
            state_error = bounded_error(str(error))
    if state is None and state_error is not None:
        if not verify_checkout:
            return _candidate(entry, error=state_error), None
        try:
            _checkout, checkout_repository = _verified_locator_checkout(entry)
        except (GitError, OSError) as error:
            unavailable = entry.copy()
            unavailable["repository_root"] = "unavailable"
            return (
                _candidate(
                    unavailable,
                    error=error_message("cli.error.checkout_unavailable", reason=bounded_error(str(error))),
                ),
                None,
            )
        candidate = _candidate(entry, error=state_error)
        if checkout_repository is not None:
            if "repository" in entry and not same_repository(entry["repository"], checkout_repository):
                return _candidate(
                    entry,
                    error=error_message("cli.error.checkout_repository"),
                    identity_conflict=True,
                ), None
            candidate["repository"] = checkout_repository
        return candidate, None
    if state is None:  # pragma: no cover - state errors return above
        return _candidate(entry, error=error_message("cli.error.state_unreadable")), None
    if verify_checkout:
        try:
            checkout, checkout_repository = _verified_locator_checkout(entry)
        except (GitError, OSError) as error:
            unavailable = entry.copy()
            unavailable["repository_root"] = "unavailable"
            return (
                _candidate(
                    unavailable,
                    state=state,
                    error=error_message("cli.error.checkout_unavailable", reason=bounded_error(str(error))),
                ),
                None,
            )
        state_repository = state.get("repository")
        if checkout_repository is not None and not same_repository(state_repository, checkout_repository):
            return _candidate(
                entry,
                state=state,
                error=error_message("cli.error.checkout_run"),
                identity_conflict=True,
            ), None
        checkout_identity = checkout.checkout_identity()
        state_identity = state.get("checkout_identity")
        if (
            not isinstance(state_identity, str)
            or checkout_identity is None
            or checkout_identity != state_identity
        ):
            unavailable = entry.copy()
            unavailable["repository_root"] = "unavailable"
            return (
                _candidate(
                    unavailable,
                    state=state,
                    error=error_message("cli.error.checkout_identity"),
                    identity_conflict=state_identity is not None,
                ),
                None,
            )
    return _candidate(entry, state=state, error=state_error), (
        state if state_error is None else None
    )


def _candidate(
    entry: LocatorEntry,
    *,
    state: dict[str, Any] | None = None,
    error: str | None = None,
    identity_conflict: bool = False,
) -> dict[str, object]:
    parent: object = entry.get("parent_number")
    repository: object = entry.get("repository")
    status: object = "unavailable"
    started_at: object = None
    if state is not None:
        raw_parent = state.get("parent")
        if isinstance(raw_parent, dict) and isinstance(raw_parent.get("number"), int):
            parent = raw_parent["number"]
        repository = state.get("repository")
        status = state.get("status")
        started_at = state.get("created_at", state.get("started_at"))
    candidate: dict[str, object] = {
        "parent": parent,
        "repository": repository,
        "repository_root": entry["repository_root"],
        "run_id": entry["run_id"],
        "started_at": started_at,
        "state_dir": entry["state_dir"],
        "status": status,
    }
    if error is not None:
        candidate["error"] = error
    if identity_conflict:
        candidate.update(repository=None, parent=None, repository_root="unavailable")
    return candidate


def selector_error(
    code: str, message: str, candidates: list[dict[str, object]]
) -> RunLocatorError:
    details = message
    if candidates:
        details = error_message("cli.error.candidates", reason=message, candidates="\n".join(
            _candidate_line(candidate) for candidate in candidates
        ))
    return RunLocatorError(code, details, candidates=candidates)


def _candidate_line(candidate: dict[str, object]) -> str:
    error = candidate.get("error")
    error_detail = f" error={error}" if isinstance(error, str) else ""
    return (
        f"- repository={candidate.get('repository') or 'unknown'} "
        f"Parent=#{candidate.get('parent') or 'unknown'} "
        f"status={candidate.get('status') or 'unknown'} "
        f"started_at={candidate.get('started_at') or 'unknown'} "
        f"worktree={candidate.get('repository_root')} "
        f"state_dir={candidate.get('state_dir')}{error_detail}"
    )


def validate_repository_selector(
    repository: str | None, state: dict[str, Any], run_id: str,
    state_dir: str | None = None,
) -> None:
    if repository is not None:
        validate_repository_name(repository)
        if not same_repository(state.get("repository"), repository):
            parent = state.get("parent")
            candidate = {
                "parent": (
                    parent.get("number")
                    if isinstance(parent, Mapping)
                    else None
                ),
                "repository": state.get("repository"),
                "repository_root": "当前 checkout",
                "run_id": run_id,
                "started_at": state.get("created_at"),
                "state_dir": state_dir or "Runner 统一状态目录",
                "status": state.get("status"),
            }
            raise selector_error(
                "run_selector_repository_mismatch",
                error_message('cli.error.repository_mismatch' ,value0=run_id, value1=repository),
                [candidate],
            )


def validate_repository_name(repository: str) -> None:
    owner, separator, name = repository.partition("/")
    if not separator or not owner or not name or "/" in name:
        raise RunLocatorError(
            "run_selector_invalid_repository",
            error_message('cli.error.repository_format'),
        )


def load_exact_run(
    run_id: str, *, repository: str | None, state_dir: str | None,
    fixture: str | None,
) -> tuple[dict[str, Any], Path | None, Path | None]:
    repository_root: Path | None = None
    state_root: Path | None = None
    if state_dir:
        state_root = Path(state_dir).resolve()
        states = StateStore(state_root)
        repository_root = _repository_root_for_state_dir(state_root)
        state = load_selected_state(states, run_id, repository=repository, read_only=True)
    else:
        try:
            git = open_workspace(repository, fixture, create=False)
        except (GitError, RunLocatorError):
            git = None
        state = None
        if git is not None:
            state = load_selected_state(StateStore(workspace_state_root(git.root)), run_id, repository=repository, read_only=True)
            if state is not None:
                repository_root = git.root
                state_root = workspace_state_root(git.root)
        if state is None:
            locator = RunLocatorIndex.default()
            state_root = locator.resolve_state_dir(run_id)
            state = load_selected_state(StateStore(state_root), run_id, repository=repository, read_only=True)
            if state is None:
                raise RunLocatorError(
                    "run_locator_stale",
                    error_message('cli.error.locator_invalid_state' ,value0=run_id),
                )
            roots = {
                Path(entry["repository_root"]).resolve()
                for entry in locator.entries()
                if entry["run_id"] == run_id
                and Path(entry["state_dir"]).resolve() == state_root.resolve()
            }
            if len(roots) == 1:
                repository_root = next(iter(roots))
    if state is None:  # pragma: no cover - exact loader raises before this point
        raise ValueError(f"unknown Delivery Run: {run_id}")
    return state, repository_root, state_root


def selected_local_run_id(
    selected: dict[str, object],
    *,
    repository_root: Path,
    state_dir: str | None,
    command: str,
) -> str:
    selected_run_id = selected.get("run_id")
    if not isinstance(selected_run_id, str):  # pragma: no cover - candidate contract
        raise ValueError("selected Delivery Run is missing its Run ID")
    selected_root = selected.get("repository_root")
    if (
        not isinstance(selected_root, str)
        or Path(selected_root).resolve() != repository_root.resolve()
    ):
        raise selector_error(
            "run_selector_requires_checkout",
            error_message('cli.error.workspace_mismatch' ,value0=command),
            [selected],
        )
    selected_state_dir = selected.get("state_dir")
    expected_state_dir = (
        Path(state_dir).resolve()
        if state_dir
        else (workspace_state_root(repository_root)).resolve()
    )
    if (
        not isinstance(selected_state_dir, str)
        or Path(selected_state_dir).resolve() != expected_state_dir
    ):
        raise selector_error(
            "run_selector_requires_state_dir",
            error_message('cli.error.state_directory_mismatch' ,value0=command),
            [selected],
        )
    return selected_run_id
