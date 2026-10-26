"""Public managed creation and lifecycle transport, without delivery decisions.

Kernel owns Task selection, control, Host admission and every delivery transition.
This module freezes input and resource identities and performs the Host handoff.
"""
from __future__ import annotations

import argparse
from collections.abc import Mapping
from dataclasses import replace
from functools import partial
import os
import stat
from pathlib import Path
import re
from typing import Any, TYPE_CHECKING

from agent_run.storage.run_scope import selected_statement
from agent_run.errors import error_message, KernelError
from agent_run.contracts.copy import text

from agent_run.cli import settings as personal_settings
from agent_run.core.operation_runner import observe_control
from agent_run.runtime.foreground_host import ForegroundHost
from agent_run.model import Commit, Delivery, DeliveryKind, HostObservation, Requirements, ResourceInspection, RunSpec, World

if TYPE_CHECKING:
    from agent_run.storage.routing import RepositoryRoute
    from agent_run.core.kernel import Kernel
    from agent_run.adapters.systemd import SystemdHost
    from agent_run.config.defaults import UserDefaultsStore
    from agent_run.contracts.agent_profiles import ProfileOverrides



def _require_github_host() -> None:
    if os.environ.get("GH_HOST", "github.com").lower() not in {"", "github.com"}:
        raise ValueError(error_message("cli.error.github_host"))


def creation_resources(run_id: str, requirements: Requirements, base: Commit,
                       default_ref: str, root: Path) -> tuple[Delivery, ...]:
    """Name exact resources, without creating refs/worktrees or choosing readiness.

    Every native sub-Issue is represented in its original order, including closed
    or blocked Issues. Kernel.create alone interprets their source facts and Graph.
    """
    if not re.fullmatch(r"[A-Za-z0-9_-]+", run_id) or not root.is_absolute():
        raise ValueError("Creation requires an exact Run ID and absolute managed root")
    if not default_ref.startswith("refs/heads/"):
        raise ValueError("Creation requires the actual default branch ref")
    prefix = f"refs/heads/agent-run/{run_id}/"
    owned_root = root.parent / "worktrees" / run_id
    common = str(root / ".git")
    parent = requirements.parent.number
    if not requirements.parent.sub_issue_numbers:
        return (Delivery(f"parent:{parent}", DeliveryKind.PARENT_ONLY, parent, "unobserved", base,
                         prefix + "parent", default_ref, checkout=str(owned_root / "parent"),
                         owned_root=str(owned_root), common_git_dir=common),)
    run_ref = prefix + "run"
    jobs = tuple(Delivery(f"ticket:{number}", DeliveryKind.TICKET, number, "unobserved", base,
                         prefix + f"ticket-{number}", run_ref, checkout=str(owned_root / f"ticket-{number}"),
                         owned_root=str(owned_root), common_git_dir=common)
                 for number in requirements.parent.sub_issue_numbers)
    final = Delivery(f"final:{parent}", DeliveryKind.FINAL_RUN, parent, "unobserved", base,
                     run_ref, default_ref, checkout=str(owned_root / "run-publication"),
                     owned_root=str(owned_root), common_git_dir=common)
    return (*jobs, final)


def add_parsers(commands: Any, language: str) -> None:
    """Retain the established public arguments; unsupported actions stay explicit."""
    for command in ("run", "resume", "approve", "requeue", "revise", "stop", "abandon"):
        child = commands.add_parser(command, language=language, help=text(f"cli.{command}_help", language=language))
        child.add_argument("parent" if command == "run" else "run_id",
                           type=partial(personal_settings._positive, language=language) if command == "run" else str)
        child.add_argument("--repo", help=text("cli.repo_help", language=language))
        child.add_argument("--state-dir", type=Path, help=text("cli.state_dir_help", language=language))
        child.add_argument("--json", action="store_true", dest="as_json")
        if command != "stop":
            child.add_argument("--foreground", action="store_true", default=None,
                               help="Run the admitted Host in this terminal; never fall back from systemd")
        if command in {"resume", "stop"}:
            child.add_argument("--ticket", type=partial(personal_settings._positive, language=language),
                               help=text(f"cli.{command}_ticket_help", language=language))
        if command in {"resume", "revise"}:
            child.add_argument("--message", required=command == "revise",
                               help=text("cli.response_help" if command == "resume" else "cli.feedback_help", language=language))
        if command == "resume":
            child.add_argument("--new-thread", action="store_true", help=text("cli.new_thread_help", language=language))
            personal_settings._policy_options(child, language, allow_thread_policy=False)
        if command == "abandon":
            child.add_argument("--discard-worktree", action="store_true", help=text("cli.discard_help", language=language))
        if command == "run":
            personal_settings._policy_options(child, language)
            personal_settings._profile_options(child, language)
            from agent_run.cli.identity_options import add_identity_options
            add_identity_options(child, language)
            notify = child.add_mutually_exclusive_group()
            notify.add_argument("--notification-mode", choices=("concise", "detailed"), help=text("cli.notifications_help", language=language))
            notify.add_argument("--no-notifications", action="store_true", help=text("cli.no_notifications_help", language=language))


def _runtime_directory(action_id: str, environment: Mapping[str, str], *, run_partition: str | None = None) -> Path:
    from agent_run.config.paths import app_runtime_root
    if not re.fullmatch(r"[A-Za-z0-9_-]+", action_id):
        raise ValueError("Expected an exact Action identity")
    if run_partition is not None and not re.fullmatch(r"[a-f0-9]{64}", run_partition):
        raise ValueError("Expected an exact Run carrier partition")
    root = app_runtime_root(environment)
    host_root = root / "native-host"
    directories = [root, host_root]
    if run_partition is not None:
        host_root = host_root / run_partition
        directories.append(host_root)
    for directory in directories:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = directory.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077):
            raise ValueError("Host runtime directory must be private and owned")
    directory = host_root / action_id
    directory.mkdir(mode=0o700)
    return directory


def submit(kernel: Kernel, kind: str, *, action_id: str, command: tuple[str, ...],
           manager: SystemdHost | ForegroundHost, environment: Mapping[str, str], runner_lock: Path,
           subject: str | None = None, world: World | None = None,
           message: str | None = None, new_thread: bool = False,
           policy_overrides: Mapping[str, Any] | None = None,
           replacement: Delivery | None = None,
           resource_inspections: tuple[ResourceInspection, ...] | None = None, discard_worktree: bool = False,
           continuation_of: str | None = None) -> dict[str, Any]:
    """Submit one explicit Action through the existing managed Host protocol.

    The caller supplies the exact command constructed by the selected native
    Executor, including the original loader environment needed before imports.
    Unknown launch remains unknown; no replacement executor or retry is started.
    """
    from agent_run.runtime.package_lease import runner_usage_lease
    from agent_run.runtime.handoff import handoff_scope, launch_handoff
    with runner_usage_lease(runner_lock) as terminal:
        manager.preflight()  # Before creating a carrier directory or accepting Action.
        locations = kernel.run_paths()
        directory = _runtime_directory(action_id, environment, run_partition=locations.host_partition)
        carrier = directory / "environment.json"
        request = kernel.admit_host(action_id, kind=kind, command=command, environment_carrier=carrier,
                                    preflight=manager.preflight, subject=subject, world=world,
                                    message=message, new_thread=new_thread, policy_overrides=policy_overrides,
                                    replacement=replacement,
                                    resource_inspections=resource_inspections, discard_worktree=discard_worktree,
                                    continuation_of=continuation_of,
                                    backend="foreground" if isinstance(manager, ForegroundHost) else "systemd")
        observed: HostObservation | None
        if isinstance(manager, ForegroundHost):
            observed = manager.launch(kernel, request, lock=runner_lock, lease=terminal)
        else:
            from agent_run.cli.host_exit import exit_properties
            properties = exit_properties(kernel.storage.path, action_id, request, runner_lock, environment,
                **({"storage": kernel.storage} if getattr(kernel.storage, "run_id", None) is not None else {}))
            with handoff_scope(request, environment, runner_lock=runner_lock, terminal_lease=terminal) as handoff:
                observed = launch_handoff(kernel, action_id, manager, handoff, exit_properties=properties)
        with kernel.storage.transaction(write=False) as tx:
            action_status = tx.one(*selected_statement(tx, 'SELECT status FROM actions WHERE id=?',
                                'SELECT status FROM actions WHERE run_id=? AND id=?', (action_id,)))[0]
        return {"result": "accepted", "status": "action_" + action_status, "run_id": request.run_id,
                "action_id": action_id, "action": kind,
                "host": "unknown" if observed is None else observed.disposition,
                "host_unit": request.unit_name, "host_backend": request.backend}



def attach(kernel: Kernel, *, requested_action: str, manager: SystemdHost | ForegroundHost,
           environment: Mapping[str, str], runner_lock: Path) -> dict[str, Any] | None:
    """Observe an original Host, or continue only its still-unsent launch grant.

    Attaching does not apply the caller's new control request. A durable dispatch
    claim is never converted into permission to prepare another handoff or start.
    """
    from agent_run.runtime.package_lease import runner_usage_lease
    from agent_run.runtime.handoff import handoff_scope, launch_handoff, reconcile_handoff
    with runner_usage_lease(runner_lock) as terminal:
        if isinstance(manager, ForegroundHost):
            manager.reconcile_closed(kernel, runner_lock)
        action_id = kernel.active_host_action()
        if action_id is None:
            return None
        manager.preflight()
        request = kernel.host_request(action_id)
        observed: HostObservation | None
        if (request.backend == "foreground") != isinstance(manager, ForegroundHost):
            raise KernelError("host_backend_changed")
        if isinstance(manager, ForegroundHost):
            observed = (manager.observe(kernel, request, runner_lock) if kernel.host_dispatched(action_id)
                        else manager.launch(kernel, request, lock=runner_lock, lease=terminal))
        elif kernel.host_dispatched(action_id):
            observed = reconcile_handoff(kernel, action_id, manager)
        else:
            from agent_run.cli.host_exit import exit_properties
            properties = exit_properties(kernel.storage.path, action_id, request, runner_lock, environment,
                **({"storage": kernel.storage} if getattr(kernel.storage, "run_id", None) is not None else {}))
            with handoff_scope(request, environment, runner_lock=runner_lock, terminal_lease=terminal) as handoff:
                observed = launch_handoff(kernel, action_id, manager, handoff, exit_properties=properties)
        with kernel.storage.transaction(write=False) as tx:
            action = tx.one(*selected_statement(tx, 'SELECT kind,status FROM actions WHERE id=?',
                         'SELECT kind,status FROM actions WHERE run_id=? AND id=?', (action_id,)))
        if observed is not None and observed.disposition == "exited" and observed.usage_released:
            from agent_run.notifications.sender import notify_committed
            notification_options: dict[str, Any] = {"storage": kernel.storage} if getattr(kernel.storage, "run_id", None) is not None else {}
            notify_committed(kernel.storage.path, **notification_options)
        return {"result": "attached", "status": "action_" + action["status"], "run_id": request.run_id,
                "action_id": action_id, "action": action["kind"],
                "requested_action": requested_action, "requested_action_applied": False,
                "host": "unknown" if observed is None else observed.disposition,
                "host_unit": request.unit_name, "host_backend": request.backend}

def _manager_for(kernel: Kernel | None, parsed: argparse.Namespace,
                 environment: Mapping[str, str]) -> SystemdHost | ForegroundHost:
    import shutil
    from agent_run.adapters.systemd import SystemdHost
    requested = getattr(parsed, "foreground", None)
    backend = "foreground" if requested else "systemd"
    if kernel is not None:
        action = kernel.active_host_action()
        if action is not None:
            original = kernel.host_request(action).backend
            if requested and original != "foreground":
                raise KernelError("host_backend_changed")
            backend = original
    if backend == "foreground":
        return ForegroundHost(environment=environment)
    return SystemdHost(systemctl=shutil.which("systemctl") or "systemctl",
                       systemd_run=shutil.which("systemd-run") or "systemd-run", environment=environment)


def _active_runner() -> bool:
    from agent_run.config.paths import app_data_root
    active = (app_data_root() / "active" / "current").resolve()
    return Path(__file__).resolve().is_relative_to(active)


def _run(parsed: argparse.Namespace, *, manager: SystemdHost | ForegroundHost,
         environment: Mapping[str, str], runner_lock: Path,
         repository_route: RepositoryRoute | None = None) -> dict[str, Any]:
    """Freeze creation inputs outside the sole Core Task creation transaction."""
    from uuid import uuid4
    from agent_run.adapters.github import GhGitHubReader
    from agent_run.cli.identity_options import identity_overrides
    from agent_run.adapters.workspace import ManagedWorkspace
    from agent_run.contracts.task import TaskKey
    from agent_run.config.defaults import UserDefaultsStore
    from agent_run.core.kernel import Kernel
    from agent_run.errors import KernelError
    from agent_run.runtime.executor import command
    from agent_run.storage.routing import RepositoryRoute
    from agent_run.storage import routing

    if repository_route is not None and not routing.REPOSITORY_PUBLIC_READY:
        raise KernelError("repository_activation_incomplete")

    directory = Path.cwd()
    repository = parsed.repo or GhGitHubReader(working_directory=directory).repository_hint()
    if repository is None:
        raise KernelError("workspace_required", "Select --repo OWNER/NAME or a Git checkout")
    workspace = ManagedWorkspace.for_repository(repository)
    if parsed.state_dir is not None and parsed.state_dir.resolve() != workspace.state_root.resolve():
        raise KernelError("managed_state_identity", "run requires the repository's managed state directory")
    if repository_route is not None and repository_route != RepositoryRoute.for_workspace(workspace):
        raise KernelError("managed_state_identity")
    task = TaskKey(workspace.repository_root, workspace.repository, parsed.parent)
    kernel = Kernel.open_task(task) if repository_route is None else repository_route.open_task(task)
    if kernel is None:
        manager.preflight()
        overrides: dict[str, str | bool | None] = {
            f"{role}_{field}": getattr(parsed, f"{role}_{field}")
            for role in ("development", "review", "publication") for field in ("model", "effort")
            if getattr(parsed, f"{role}_{field}") is not None}
        if parsed.publication_from_development or parsed.publication_reference == "development":
            overrides["publication_from_development"] = True
        database, spec, requirements = prepare_creation(parsed.parent, workspace.repository,
            source_directory=directory, defaults=UserDefaultsStore(), preset=parsed.profile_preset,
            profile_overrides=overrides, policy_overrides=personal_settings._policy_values(parsed),
            identity_overrides=identity_overrides(parsed), disable_notifications=parsed.no_notifications,
            notification_mode=parsed.notification_mode)
        # A competing creator can win during external preparation. Use only
        # Core's returned Run, including its frozen identity and language.
        if repository_route is None:
            kernel = Kernel.create_task(database, spec, requirements=requirements)
        else:
            created = repository_route.create(task, spec, requirements)
            kernel = repository_route.kernel(created.run_id)
    with kernel.storage.transaction(write=False) as tx:
        run = tx.one(*selected_statement(tx, 'SELECT id,language,repository_root FROM runs',
                  'SELECT id,language,repository_root FROM runs WHERE id=?', ()))
        if run["language"] not in {"en", "zh"} or run["repository_root"] != str(workspace.repository_root):
            raise KernelError("creation_metadata_missing")
        parsed._run_language = run["language"]
    manager = _manager_for(kernel, parsed, environment)
    manager.preflight()
    workspace.open_path()
    original = attach(kernel, requested_action="run", manager=manager, environment=environment, runner_lock=runner_lock)
    if original is not None:
        original["language"] = run["language"]
        return original
    action_id = uuid4().hex
    result = submit(kernel, "run", action_id=action_id,
        command=command(kernel.storage.path, action_id, runner_lock, environment,
            **({"run_id": kernel.storage.run_id} if getattr(kernel.storage, "run_id", None) is not None else {})),
        manager=manager, environment=environment, runner_lock=runner_lock)
    result["language"] = run["language"]
    return result


def execute(parsed: argparse.Namespace, *, language: str,
            repository_route: RepositoryRoute | None = None) -> dict[str, Any]:
    """Locate an exact managed Run and submit through sole Kernel admission.

    repository_route is the inactive internal assembly seam, never a parser or
    environment option. Public dispatch still supplies None until every Core
    publication guard, selector and protocol gate is switched together.
    """
    import json
    import os
    import shutil
    from uuid import uuid4
    from agent_run.adapters.workspace import ManagedWorkspace, workspace_for_root
    from agent_run.contracts.repository import normalize_repository
    from agent_run.contracts.task import TaskKey
    from agent_run.storage.routing import RepositoryRoute
    from agent_run.storage import routing
    from agent_run.runtime.package_lease import default_runner_lock_path, runner_usage_lease
    from agent_run.adapters.systemd import SystemdHost, HostUnavailable
    from agent_run.core.kernel import Kernel
    from agent_run.errors import KernelError
    from agent_run.cli.selection import _candidates, _select
    from agent_run.adapters.remote import RemoteEffects

    if parsed.command not in {"run", "resume", "approve", "stop", "revise", "requeue", "abandon"}:
        raise KernelError("lifecycle_not_connected", parsed.command)
    if repository_route is not None and not routing.REPOSITORY_PUBLIC_READY:
        raise KernelError("repository_activation_incomplete")
    _require_github_host()
    policy_overrides = personal_settings._policy_values(parsed)
    environment = dict(os.environ)
    runner_lock = default_runner_lock_path(environment)
    with runner_usage_lease(runner_lock):
        if parsed.command == "run":
            if not _active_runner():
                raise HostUnavailable("Lifecycle commands require the installed Active Runner")
            manager = _manager_for(None, parsed, environment)
            options: dict[str, Any] = {} if repository_route is None else {"repository_route": repository_route}
            return _run(parsed, manager=manager, environment=environment, runner_lock=runner_lock, **options)
        selection = argparse.Namespace(**vars(parsed))
        selection.parent = None
        if selection.run_id.isdecimal():
            selection.parent = personal_settings._positive(selection.run_id, language=language)
            selection.run_id = None
        if repository_route is None:
            candidate = _select(selection, _candidates(selection))[0]
            database = Path(candidate["database"]).resolve()
            kernel = Kernel(database, run_id=candidate["run_id"])
        else:
            # Only canonical metadata selects control work. No status JSON is
            # interpreted as a Task pointer and no directory supplies a Run ID.
            workspace = ManagedWorkspace.for_repository(repository_route.repository)
            if (repository_route != RepositoryRoute.for_workspace(workspace)
                    or parsed.repo is not None and normalize_repository(parsed.repo) != repository_route.repository
                    or parsed.state_dir is not None and parsed.state_dir.resolve() != workspace.state_root.resolve()):
                raise KernelError("managed_state_identity")
            run_id = selection.run_id
            if run_id is None:
                if selection.parent is None:
                    raise KernelError("control_subject_ambiguous", "Select an exact Run or Parent")
                location = repository_route.task(TaskKey(workspace.repository_root, workspace.repository, selection.parent))
                if location is None or location.current_run is None:
                    raise KernelError("task_run_unpublished")
                run_id = location.current_run
            database = repository_route.database
            kernel = repository_route.kernel(run_id)
        with kernel.storage.transaction(write=False) as tx:
            run = tx.one(*selected_statement(tx, 'SELECT * FROM runs',
                      'SELECT * FROM runs WHERE id=?', ()))
            if run["language"] not in {"zh", "en"} or run["repository_root"] is None:
                raise KernelError("creation_metadata_missing")
            parsed._run_language = run["language"]
            workspace = workspace_for_root(Path(run["repository_root"]))
            if not database.is_relative_to(workspace.state_root.resolve()):
                raise KernelError("managed_state_identity")
            from agent_run.storage.tasks import run_is_current
            if not run_is_current(tx, database):
                raise KernelError("task_run_unpublished", "This database is not the published Run for its Task")
            stored = tx.one(*selected_statement(tx, 'SELECT subject,material_id FROM run_inputs',
                         'SELECT subject,material_id FROM run_inputs WHERE run_id=?', ()))
            inputs = json.loads(tx.get(stored["material_id"], stored["subject"]))
            abandoned = tx.rows(*selected_statement(tx, "SELECT id,status FROM actions WHERE kind='abandon'",
                            "SELECT id,status FROM actions WHERE run_id=? AND kind='abandon'", ()), limit=1) if parsed.command == "abandon" else []
            abandonment = dict(abandoned[0]) if abandoned else None
            unknown_host = bool(parsed.command == "abandon" and tx.rows(*selected_statement(tx, "SELECT owner FROM hosts WHERE (status!='exited' OR usage_released=0) AND status!='running' LIMIT 1",
                                                                    "SELECT owner FROM hosts WHERE run_id=? AND (status!='exited' OR usage_released=0) AND status!='running' LIMIT 1", ())))
        if parsed.command == "stop":
            subject = None
            if parsed.ticket is not None:
                with kernel.storage.transaction(write=False) as tx:
                    targets = tx.all(*selected_statement(tx, "SELECT id FROM subjects WHERE kind='ticket' AND issue=?",
                                  "SELECT id FROM subjects WHERE run_id=? AND kind='ticket' AND issue=?", (parsed.ticket,)))
                    if len(targets) != 1:
                        raise KernelError("control_subject_ambiguous", "Select an exact Ticket")
                    subject = str(targets[0]["id"])
            action_id = uuid4().hex
            action_status = kernel.stop_task(action_id, subject=subject)
            if action_status == "complete":
                return {"result": "completed", "status": "completed", "run_id": run["id"],
                        "action_id": None, "action": "stop", "host": "exited", "language": run["language"]}
            if action_status != "complete":
                from agent_run.notifications.sender import notify_committed
                notify_committed(database, storage=kernel.storage)
            return {"result": "accepted", "status": "action_" + action_status, "run_id": run["id"],
                    "action_id": action_id, "action": "stop", "host": "unknown", "language": run["language"]}
        inspections = None
        if parsed.command == "abandon":
            from agent_run.adapters.git_objects import LocalGit
            from agent_run.adapters.local_git import LocalEffects
            inspections = LocalEffects(LocalGit(workspace.repository_root)).inspect_resources(kernel.generation_resources())

            def abandon_receipt(host: str = "unknown") -> dict[str, Any]:
                if abandonment is None:
                    action_id = uuid4().hex
                    assert inspections is not None
                    status = kernel.abandon_task(action_id, resource_inspections=inspections,
                                                 discard_worktree=parsed.discard_worktree)
                else:
                    action_id, status = abandonment["id"], abandonment["status"]
                result: dict[str, Any] = {"result": "accepted" if abandonment is None else "attached", "status": "action_" + status,
                          "run_id": run["id"], "action_id": action_id, "action": "abandon", "host": host,
                          "language": run["language"]}
                if abandonment is not None:
                    result.update(requested_action="abandon", requested_action_applied=False)
                return result

            if (unknown_host and abandonment is None) or (abandonment is not None and abandonment["status"] == "complete"):
                return abandon_receipt()
        if not _active_runner():
            raise HostUnavailable("Lifecycle commands require the installed Active Runner")
        manager = _manager_for(kernel, parsed, environment)
        manager.preflight()
        workspace.open_path()
        original = attach(kernel, requested_action=parsed.command, manager=manager,
                          environment=environment, runner_lock=runner_lock)
        if original is not None and original["host"] == "exited" and kernel.active_host_action() is None:
            # Complete release permits this request to reach Core admission in
            # the same invocation, regardless of the original Host backend.
            original = None
        if parsed.command == "abandon":
            if original is not None and original["host"] != "running":
                if original["host"] == "exited" and kernel.active_host_action() is None and abandonment is not None:
                    original = None  # Exact exit and usage proof permit the original cleanup continuation.
                else:
                    return abandon_receipt()
            if original is not None and abandonment is not None:
                return abandon_receipt("running")
        if original is not None and original["host"] != "running":
            original["language"] = run["language"]
            return original
        if parsed.command == "requeue":
            from agent_run.adapters.github import GhGitHubReader
            from agent_run.adapters.source import read_requirements
            reader = GhGitHubReader(workspace.repository, working_directory=workspace.repository_root,
                                    runner_identity=inputs["runner_identity"])
            kernel.refresh(lambda: read_requirements(reader, run["parent_issue"]))
            source = kernel.source_status()
        if parsed.command == "resume":
            subject = kernel.resume_subject(ticket=parsed.ticket)
        else:
            with kernel.storage.transaction(write=False) as tx:
                if parsed.command == "abandon":
                    targets = []
                elif parsed.command == "requeue":
                    if source["blocked"] == "final_refresh" and source["final_subject"] is not None:
                        targets = tx.all(*selected_statement(tx, 'SELECT id FROM subjects WHERE id=?',
                                      'SELECT id FROM subjects WHERE run_id=? AND id=?', (source["final_subject"],)))
                    else:
                        targets = tx.all(*selected_statement(tx, "SELECT DISTINCT s.id FROM subjects s JOIN gates g ON g.subject=s.id WHERE g.code='requeue_required' ORDER BY s.id",
                                      "SELECT DISTINCT s.id FROM subjects s JOIN gates g ON g.run_id=s.run_id AND g.subject=s.id WHERE s.run_id=? AND g.code='requeue_required' ORDER BY s.id", ()))
                elif parsed.command == "approve":
                    targets = tx.all(*selected_statement(tx, "SELECT id FROM subjects WHERE kind!='ticket' AND phase='approval'",
                                  "SELECT id FROM subjects WHERE run_id=? AND kind!='ticket' AND phase='approval'", ()))
                elif parsed.command == "revise":
                    targets = tx.all(*selected_statement(tx, "SELECT id FROM subjects WHERE kind='final_run' AND phase='approval'",
                                  "SELECT id FROM subjects WHERE run_id=? AND kind='final_run' AND phase='approval'", ()))
                else:
                    targets = tx.all(*selected_statement(tx, 'SELECT DISTINCT s.id FROM subjects s JOIN gates g ON g.subject=s.id ORDER BY s.id',
                                  'SELECT DISTINCT s.id FROM subjects s JOIN gates g ON g.run_id=s.run_id AND g.subject=s.id WHERE s.run_id=? ORDER BY s.id', ()))
                if len(targets) > 1 or (not targets and parsed.command != "abandon"):
                    raise KernelError("control_subject_ambiguous", "Select an exact Ticket or current Parent boundary")
                subject = None if not targets else str(targets[0]["id"])
        world = None
        replacement = None
        if parsed.command in {"approve", "revise", "requeue"}:
            assert subject is not None
            remote = inputs.get("repository_remote")
            if not isinstance(remote, str) or not remote:
                raise KernelError("creation_repository_remote_required")
            world = observe_control(kernel, subject, lambda binding: RemoteEffects(
                binding.repository_root, binding.repository,
                runner_identity=binding.inputs.runner_identity, remote=remote))
            if parsed.command == "requeue":
                original_delivery = kernel.delivery(subject)
                if original_delivery.kind != DeliveryKind.FINAL_RUN:
                    if world.target is None:
                        raise KernelError("target_branch_missing")
                    generation = kernel.status(subject)["generation"] + 1
                    name = (f"ticket-{original_delivery.issue}" if original_delivery.kind == DeliveryKind.TICKET else "parent") + f"-g{generation}"
                    owned_root = workspace.repository_root.parent / "worktrees" / run["id"]
                    replacement = replace(original_delivery, revision=world.revision, base=world.target,
                        source_ref=f"refs/heads/agent-run/{run['id']}/{name}", checkout=str(owned_root / name),
                        owned_root=str(owned_root), common_git_dir=str(workspace.repository_root / ".git"))
        elif parsed.command == "resume":
            remote = inputs.get("repository_remote")
            if not isinstance(remote, str) or not remote:
                raise KernelError("creation_repository_remote_required")
            from agent_run.adapters.github import GhGitHubReader
            from agent_run.adapters.source import read_requirements
            reader = GhGitHubReader(workspace.repository, working_directory=workspace.repository_root,
                                    runner_identity=inputs["runner_identity"])
            kernel.refresh(lambda: read_requirements(reader, run["parent_issue"]))
        # Root owns the selected Executor command, including sealed loader facts.
        from agent_run.runtime.executor import command
        action_id = uuid4().hex
        if original is not None:
            request = kernel.host_request(original["action_id"])
            kernel.control_host(request, parsed.command, action_id, subject=subject, world=world,
                message=getattr(parsed, "message", None), new_thread=getattr(parsed, "new_thread", False),
                policy_overrides=policy_overrides, resource_inspections=inspections,
                replacement=replacement,
                discard_worktree=getattr(parsed, "discard_worktree", False))
            with kernel.storage.transaction(write=False) as tx:
                action_status = tx.one(*selected_statement(tx, 'SELECT status FROM actions WHERE id=?',
                                    'SELECT status FROM actions WHERE run_id=? AND id=?', (action_id,)))[0]
            result = {"result": "accepted", "status": "action_" + action_status, "run_id": run["id"],
                "action_id": action_id, "action": parsed.command, "host": "running", "host_unit": request.unit_name, "host_backend": request.backend}
        else:
            result = submit(kernel, parsed.command, action_id=action_id,
                            command=command(database, action_id, runner_lock, environment,
                                run_id=kernel.storage.run_id), manager=manager,
                            environment=environment, runner_lock=runner_lock, subject=subject, world=world,
                            message=getattr(parsed, "message", None), new_thread=getattr(parsed, "new_thread", False),
                            policy_overrides=policy_overrides, resource_inspections=inspections,
                            replacement=replacement,
                            discard_worktree=getattr(parsed, "discard_worktree", False),
                            continuation_of=abandonment["id"] if abandonment is not None else None)
        result["language"] = run["language"]
        return result


def prepare_creation(parent: int, repository: str | None, *, source_directory: Path,
                     defaults: UserDefaultsStore, preset: str | None = None,
                     profile_overrides: ProfileOverrides | None = None,
                     policy_overrides: Mapping[str, Any] | None = None,
                     identity_overrides: Mapping[str, Any] | None = None,
                     disable_notifications: bool = False, notification_mode: str | None = None,
                     remote_url: str | None = None) -> tuple[Path, RunSpec, Requirements]:
    """Freeze original input, clone independently and return creation facts.

    This does not create a database, source ref or worktree. Kernel creates the
    source scope and its first workspace/Run-branch intent after Task selection.
    remote_url is the transport seam for synthetic bare Git, never a CLI override.
    """
    from dataclasses import replace
    from uuid import uuid4
    from agent_run.adapters.github import GhGitHubReader
    from agent_run.errors import GitHubReadError
    from agent_run.adapters.github_commands import run_read_command
    from agent_run.runtime.git_command import run_git
    from agent_run.adapters.workspace import ManagedWorkspace, read_git_identity
    from agent_run.config.creation import freeze_creation
    from agent_run.model import RunSpec
    from agent_run.errors import KernelError
    from agent_run.adapters.remote import RemoteEffects
    from agent_run.adapters.source import read_requirements
    _require_github_host()
    selected = repository or GhGitHubReader(working_directory=source_directory).repository_hint()
    if selected is None:
        raise ValueError("Select a repository with --repo OWNER/NAME or from its Git checkout")
    workspace = ManagedWorkspace.for_repository(selected)
    run_id = uuid4().hex
    initial = RunSpec(run_id, workspace.repository, parent, "unobserved", "unobserved", (),
                      repository_root=str(workspace.repository_root))
    frozen = freeze_creation(initial, defaults, preset=preset, profile_overrides=profile_overrides,
                             policy_overrides=policy_overrides, identity_overrides=identity_overrides,
                             disable_notifications=disable_notifications, notification_mode=notification_mode)
    assert frozen.inputs is not None
    # ManagedWorkspace's single clone transport is the validated origin remote.
    frozen = replace(frozen, inputs=replace(frozen.inputs, repository_remote="origin"))
    assert frozen.inputs is not None
    identity = read_git_identity(source_directory) if not workspace.root.exists() else None
    root = workspace.ensure_path(remote_url, identity=identity, runner_identity=frozen.inputs.runner_identity)
    reader = GhGitHubReader(workspace.repository, working_directory=root,
                            runner_identity=frozen.inputs.runner_identity)
    requirements = read_requirements(reader, parent)
    actual_repository = reader.repository()
    if actual_repository.name_with_owner.lower() != workspace.repository:
        raise KernelError("repository_identity_changed")
    port = RemoteEffects(root, workspace.repository, runner_identity=frozen.inputs.runner_identity)
    head = actual_repository.default_head_sha
    if not isinstance(head, str) or not head:
        raise KernelError("missing_target_branch", "The observed default branch has no exact commit")
    base = port._commit(head)[0]
    jobs = creation_resources(run_id, requirements, base, "refs/heads/" + actual_repository.default_branch, root)
    refs = sorted({job.source_ref for job in jobs})
    for ref in refs:
        if run_git(["git", "check-ref-format", ref], cwd=root).returncode:
            raise ValueError("Invalid generated resource ref")
        observed = run_git(["git", "for-each-ref", "--format=%(refname)", ref], cwd=root)
        if observed.returncode:
            raise KernelError("creation_resource_unreadable", ref)
        if observed.stdout.strip():
            raise KernelError("creation_resource_occupied", ref)
    response = run_read_command(["git", "ls-remote", "--refs", "--", "origin", *refs], cwd=root,
                                runner_identity=frozen.inputs.runner_identity)
    if response.returncode:
        raise GitHubReadError("github_read_failed", "Creation resource refs cannot be observed")
    if response.stdout.strip():
        raise KernelError("creation_resource_occupied", "A generated remote ref already exists")
    for job in jobs:
        path = Path(job.checkout)
        if path.exists() or path.is_symlink():
            raise KernelError("creation_resource_occupied", str(path))
    return workspace.state_root / run_id / "kernel.sqlite3", replace(frozen, deliveries=jobs), requirements
