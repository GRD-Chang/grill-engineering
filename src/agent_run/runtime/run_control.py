"""Application control entry: select the Task and coordinate its original Host."""
from __future__ import annotations
import argparse
from collections.abc import Mapping
from dataclasses import replace
import os
from pathlib import Path
from typing import Any, TYPE_CHECKING
from agent_run.storage.run_scope import run_statement
from agent_run.errors import KernelError
from agent_run.core.operation_runner import observe_control
from agent_run.runtime.foreground_host import ForegroundHost
from agent_run.runtime import host_control as hosts
from agent_run.runtime.task_creation import prepare_creation, _require_github_host
from agent_run.runtime.task import create_managed_task
from agent_run.model import DeliveryKind, HostLaunch, HostObservation
if TYPE_CHECKING:
    from agent_run.core.kernel import Kernel
    from agent_run.adapters.systemd import SystemdHost
    from agent_run.storage.routing import RepositoryRoute

def _active_runner() -> bool:
    from agent_run.config.paths import app_data_root
    active = (app_data_root() / "active" / "current").resolve()
    return Path(__file__).resolve().is_relative_to(active)


def _run(parsed: argparse.Namespace, *, manager: SystemdHost | ForegroundHost,
         environment: Mapping[str, str], runner_lock: Path,
         repository_route: RepositoryRoute | None = None) -> Kernel:
    """Freeze creation inputs outside the sole Core Task creation transaction."""
    from agent_run.adapters.github import GhGitHubReader
    from agent_run.adapters.workspace import ManagedWorkspace
    from agent_run.contracts.task import TaskKey
    from agent_run.config.defaults import UserDefaultsStore
    from agent_run.errors import KernelError
    from agent_run.storage.routing import RepositoryRoute


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
    if repository_route is None:
        repository_route = RepositoryRoute.for_workspace(workspace)
    from agent_run.runtime.task import open_task
    kernel = open_task(repository_route, task)
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
            profile_overrides=overrides, policy_overrides=getattr(parsed, "_policy_overrides", {}),
            identity_overrides=getattr(parsed, "_identity_overrides", {}), disable_notifications=parsed.no_notifications,
            notification_mode=parsed.notification_mode)
        # A competing creator can win during external preparation. Use only
        # Core's returned Run, including its frozen identity and language.
        if database != repository_route.database:
            raise KernelError("managed_state_identity")
        kernel = create_managed_task(database, spec, requirements=requirements)
    with kernel.storage.transaction(write=False) as tx:
        run = tx.one(*run_statement(tx, 'SELECT id,language,repository_root FROM runs WHERE id=?', ()))
        if run["language"] not in {"en", "zh"} or run["repository_root"] != str(workspace.repository_root):
            raise KernelError("creation_metadata_missing")
        parsed._run_language = run["language"]
    return kernel


def execute(parsed: argparse.Namespace, *, language: str,
            repository_route: RepositoryRoute | None = None) -> dict[str, Any]:
    """Locate an exact managed Run and submit through sole Kernel admission.

    Routes carry validated repository metadata. A supplied route uses the same
    sole protocol as default managed discovery; neither path reads display JSON.
    """
    import json
    import os
    from uuid import uuid4
    from agent_run.adapters.workspace import ManagedWorkspace, workspace_for_root
    from agent_run.contracts.repository import normalize_repository
    from agent_run.storage.routing import RepositoryRoute
    from agent_run.runtime.package_lease import default_runner_lock_path, runner_usage_lease
    from agent_run.adapters.systemd import HostUnavailable
    from agent_run.errors import KernelError
    from agent_run.runtime.run_selection import control_selection, repository_target, managed_repository_routes
    from agent_run.adapters.remote_observation import RemoteObservation

    if parsed.command not in {"run", "resume", "approve", "stop", "revise", "requeue", "abandon"}:
        raise KernelError("lifecycle_not_connected", parsed.command)
    _require_github_host()
    policy_overrides = getattr(parsed, "_policy_overrides", {})
    environment = dict(os.environ)
    runner_lock = default_runner_lock_path(environment)
    # Refuse old/unreadable authority before acquiring a physical Runner lease,
    # probing a Host, resolving credentials or preparing another proposal.
    from agent_run.storage.tasks import refuse_retired_task
    if parsed.command == "run":
        from agent_run.adapters.github import GhGitHubReader
        from agent_run.contracts.task import TaskKey
        repository = parsed.repo or GhGitHubReader(working_directory=Path.cwd()).repository_hint()
        if repository is None:
            raise KernelError("workspace_required", "Select --repo OWNER/NAME or a Git checkout")
        workspace = ManagedWorkspace.for_repository(repository)
        route = repository_route or RepositoryRoute.for_workspace(workspace)
        refuse_retired_task(TaskKey(workspace.repository_root, workspace.repository, parsed.parent))
        if route.database.exists() or route.database.is_symlink():
            route.metadata()
        else:
            route._require_vacant_root(route.database)
    else:
        selection = control_selection(parsed, language=language)
        routes = (repository_route,) if repository_route is not None else managed_repository_routes(selection)
        selected_route, location = repository_target(selection, routes)
        refuse_retired_task(location.task)
    kernel: Kernel | None = None
    notification_kernel: Kernel | None = None
    request: HostLaunch | None = None
    observed: HostObservation | None = None
    try:
        with runner_usage_lease(runner_lock) as terminal:
            if parsed.command == "run":
                if not _active_runner():
                    raise HostUnavailable("Lifecycle commands require the installed Active Runner")
                manager = hosts.manager_for(None, parsed, environment)
                options: dict[str, Any] = {} if repository_route is None else {"repository_route": repository_route}
                kernel = _run(parsed, manager=manager, environment=environment, runner_lock=runner_lock, **options)
                manager = hosts.manager_for(kernel, parsed, environment)
                manager.preflight()
                workspace.open_path()
                selected = hosts.select_existing(kernel, manager, runner_lock)
                if selected is None and isinstance(manager, ForegroundHost):
                    notification_kernel = kernel
                request = None if selected is None else selected[1]
                original, observed = hosts.attach_selected(kernel, selected, requested_action="run", manager=manager,
                    environment=environment, runner_lock=runner_lock, terminal=terminal)
                if observed is not None and observed.disposition == "exited" and observed.usage_released:
                    notification_kernel = kernel
                if original is not None:
                    original["language"] = parsed._run_language
                    return original
                action_id = uuid4().hex
                from agent_run.runtime.executor import command
                request = hosts.admit(kernel, "run", action_id=action_id,
                    command=command(kernel.storage.path, action_id, runner_lock, environment,
                        run_id=kernel.run_paths().run_id), manager=manager, environment=environment)
                notification_kernel = None  # A new Host fences any earlier empty-attach recovery.
                result, observed = hosts.launch_admitted(kernel, request, manager=manager, environment=environment,
                    runner_lock=runner_lock, terminal=terminal, action_id=action_id, kind="run")
                result["language"] = parsed._run_language
                return result
            workspace = ManagedWorkspace.for_repository(selected_route.repository)
            if (selected_route != RepositoryRoute.for_workspace(workspace)
                    or parsed.repo is not None and normalize_repository(parsed.repo) != selected_route.repository
                    or parsed.state_dir is not None and parsed.state_dir.resolve() not in {workspace.state_root.resolve(), selected_route.database.resolve()}):
                raise KernelError("managed_state_identity")
            database = location.database
            from agent_run.runtime.task import selected_kernel
            kernel = selected_kernel(selected_route, location.run_id)
            with kernel.storage.transaction(write=False) as tx:
                run = tx.one(*run_statement(tx, 'SELECT * FROM runs WHERE id=?', ()))
                if run["language"] not in {"zh", "en"} or run["repository_root"] is None:
                    raise KernelError("creation_metadata_missing")
                parsed._run_language = run["language"]
                workspace = workspace_for_root(Path(run["repository_root"]))
                if not database.is_relative_to(workspace.state_root.resolve()):
                    raise KernelError("managed_state_identity")
                from agent_run.storage.tasks import run_is_current
                if not run_is_current(tx):
                    raise KernelError("task_run_unpublished", "This database is not the published Run for its Task")
                stored = tx.one(*run_statement(tx, 'SELECT subject,material_id FROM run_inputs WHERE run_id=?', ()))
                inputs = json.loads(tx.get(stored["material_id"], stored["subject"]))
                abandoned = tx.rows(*run_statement(tx, "SELECT id,status FROM actions WHERE run_id=? AND kind='abandon'", ()), limit=1) if parsed.command == "abandon" else []
                abandonment = dict(abandoned[0]) if abandoned else None
                unknown_host = parsed.command == "abandon" and hosts.has_unknown_host(tx)
            if parsed.command == "stop":
                subject = None
                if parsed.ticket is not None:
                    with kernel.storage.transaction(write=False) as tx:
                        targets = tx.all(*run_statement(tx, "SELECT id FROM subjects WHERE run_id=? AND kind='ticket' AND issue=?", (parsed.ticket,)))
                        if len(targets) != 1:
                            raise KernelError("control_subject_ambiguous", "Select an exact Ticket")
                        subject = str(targets[0]["id"])
                action_id = uuid4().hex
                action_status = kernel.stop_task(action_id, subject=subject)
                if action_status == "not_running":
                    return {"result": "no_op", "status": "not_running", "run_id": run["id"],
                            "action_id": None, "action": "stop", "host": "exited", "language": run["language"]}
                if action_status == "complete":
                    return {"result": "completed", "status": "completed", "run_id": run["id"],
                            "action_id": None, "action": "stop", "host": "exited", "language": run["language"]}
                notification_kernel = kernel
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
            manager = hosts.manager_for(kernel, parsed, environment)
            manager.preflight()
            workspace.open_path()
            selected = hosts.select_existing(kernel, manager, runner_lock)
            if selected is None and isinstance(manager, ForegroundHost):
                notification_kernel = kernel
            request = None if selected is None else selected[1]
            original, observed = hosts.attach_selected(kernel, selected, requested_action=parsed.command, manager=manager,
                environment=environment, runner_lock=runner_lock, terminal=terminal)
            if observed is not None and observed.disposition == "exited" and observed.usage_released:
                notification_kernel = kernel
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
                            targets = tx.all(*run_statement(tx, 'SELECT id FROM subjects WHERE run_id=? AND id=?', (source["final_subject"],)))
                        else:
                            targets = tx.all(*run_statement(tx, "SELECT DISTINCT s.id FROM subjects s JOIN gates g ON g.run_id=s.run_id AND g.subject=s.id WHERE s.run_id=? AND g.code='requeue_required' ORDER BY s.id", ()))
                    elif parsed.command == "approve":
                        targets = tx.all(*run_statement(tx, "SELECT id FROM subjects WHERE run_id=? AND kind!='ticket' AND phase='approval'", ()))
                    elif parsed.command == "revise":
                        targets = tx.all(*run_statement(tx, "SELECT id FROM subjects WHERE run_id=? AND kind='final_run' AND phase='approval'", ()))
                    else:
                        targets = tx.all(*run_statement(tx, 'SELECT DISTINCT s.id FROM subjects s JOIN gates g ON g.run_id=s.run_id AND g.subject=s.id WHERE s.run_id=? ORDER BY s.id', ()))
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
                world = observe_control(kernel, subject, lambda binding: RemoteObservation(
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
                    action_status = tx.one(*run_statement(tx, 'SELECT status FROM actions WHERE run_id=? AND id=?', (action_id,)))[0]
                result = {"result": "accepted", "status": "action_" + action_status, "run_id": run["id"],
                    "action_id": action_id, "action": parsed.command, "host": "running", "host_unit": request.unit_name, "host_backend": request.backend}
            else:
                request = hosts.admit(kernel, parsed.command, action_id=action_id,
                                command=command(database, action_id, runner_lock, environment,
                                    run_id=kernel.run_paths().run_id), manager=manager,
                                environment=environment, subject=subject, world=world,
                                message=getattr(parsed, "message", None), new_thread=getattr(parsed, "new_thread", False),
                                policy_overrides=policy_overrides, resource_inspections=inspections,
                                replacement=replacement,
                                discard_worktree=getattr(parsed, "discard_worktree", False),
                                continuation_of=abandonment["id"] if abandonment is not None else None)
                notification_kernel = None  # New admission supersedes the retired Host's notice.
                result, observed = hosts.launch_admitted(kernel, request, manager=manager, environment=environment,
                    runner_lock=runner_lock, terminal=terminal, action_id=action_id, kind=parsed.command)
            result["language"] = run["language"]
            return result

    finally:
        if kernel is not None:
            if notification_kernel is not None:
                hosts.notify_committed(notification_kernel)
            elif request is not None and request.backend == "foreground":
                hosts.notify_after_usage(kernel, observed)
