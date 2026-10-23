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

from agent_run.messages import error_message, text

from . import personal_settings
from .operation_runner import observe_control
from .model import Commit, Delivery, DeliveryKind, HostObservation, Requirements, ResourceInspection, RunSpec, World

if TYPE_CHECKING:
    from .kernel import Kernel
    from .host_adapter import SystemdHost
    from agent_run.user_defaults import UserDefaultsStore
    from agent_run.agent_profiles import ProfileOverrides



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
            from agent_run.github_identity import add_identity_options
            add_identity_options(child, language)
            notify = child.add_mutually_exclusive_group()
            notify.add_argument("--notification-mode", choices=("concise", "detailed"), help=text("cli.notifications_help", language=language))
            notify.add_argument("--no-notifications", action="store_true", help=text("cli.no_notifications_help", language=language))


def _runtime_directory(action_id: str, environment: Mapping[str, str]) -> Path:
    from agent_run.paths import app_runtime_root
    if not re.fullmatch(r"[A-Za-z0-9_-]+", action_id):
        raise ValueError("Expected an exact Action identity")
    root = app_runtime_root(environment)
    for directory in (root, root / "native-host"):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = directory.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077):
            raise ValueError("Host runtime directory must be private and owned")
    directory = root / "native-host" / action_id
    directory.mkdir(mode=0o700)
    return directory


def submit(kernel: Kernel, kind: str, *, action_id: str, command: tuple[str, ...],
           manager: SystemdHost, environment: Mapping[str, str], runner_lock: Path,
           subject: str | None = None, world: World | None = None,
           message: str | None = None, new_thread: bool = False,
           policy_overrides: Mapping[str, Any] | None = None,
           replacement: Delivery | None = None,
           resource_inspections: tuple[ResourceInspection, ...] | None = None, discard_worktree: bool = False,
           continuation_of: str | None = None) -> dict[str, Any]:
    """Submit one explicit Action through the existing managed Host protocol.

    The caller supplies the exact command constructed by the selected native
    Executor, including the original loader environment needed before imports.
    Unknown launch remains unknown; no foreground executor or retry is started.
    """
    from agent_run.runner_lease import runner_usage_lease
    from .native_host import prepare_handoff, launch_handoff
    with runner_usage_lease(runner_lock) as terminal:
        manager.preflight()  # Before creating a carrier directory or accepting Action.
        directory = _runtime_directory(action_id, environment)
        carrier = directory / "environment.json"
        request = kernel.admit_host(action_id, kind=kind, command=command, environment_carrier=carrier,
                                    preflight=manager.preflight, subject=subject, world=world,
                                    message=message, new_thread=new_thread, policy_overrides=policy_overrides,
                                    replacement=replacement,
                                    resource_inspections=resource_inspections, discard_worktree=discard_worktree,
                                    continuation_of=continuation_of)
        from .host_exit_notifications import exit_properties
        properties = exit_properties(kernel.storage.path, action_id, request, runner_lock, environment)
        handoff = prepare_handoff(request, environment, runner_lock=runner_lock, terminal_lease=terminal)
        observed = launch_handoff(kernel, action_id, manager, handoff, exit_properties=properties)
        with kernel.storage.transaction(write=False) as tx:
            action_status = tx.one("SELECT status FROM actions WHERE id=?", (action_id,))[0]
        return {"result": "accepted", "status": "action_" + action_status, "run_id": request.run_id,
                "action_id": action_id, "action": kind,
                "host": "unknown" if observed is None else observed.disposition,
                "host_unit": request.unit_name}



def attach(kernel: Kernel, *, requested_action: str, manager: SystemdHost,
           environment: Mapping[str, str], runner_lock: Path) -> dict[str, Any] | None:
    """Observe an original Host, or continue only its still-unsent launch grant.

    Attaching does not apply the caller's new control request. A durable dispatch
    claim is never converted into permission to prepare another handoff or start.
    """
    from agent_run.runner_lease import runner_usage_lease
    from .native_host import prepare_handoff, launch_handoff, reconcile_handoff
    with runner_usage_lease(runner_lock) as terminal:
        action_id = kernel.active_host_action()
        if action_id is None:
            return None
        manager.preflight()
        request = kernel.host_request(action_id)
        observed: HostObservation | None
        if kernel.host_dispatched(action_id):
            observed = reconcile_handoff(kernel, action_id, manager)
        else:
            from .host_exit_notifications import exit_properties
            properties = exit_properties(kernel.storage.path, action_id, request, runner_lock, environment)
            handoff = prepare_handoff(request, environment, runner_lock=runner_lock, terminal_lease=terminal)
            observed = launch_handoff(kernel, action_id, manager, handoff, exit_properties=properties)
        with kernel.storage.transaction(write=False) as tx:
            action = tx.one("SELECT kind,status FROM actions WHERE id=?", (action_id,))
        return {"result": "attached", "status": "action_" + action["status"], "run_id": request.run_id,
                "action_id": action_id, "action": action["kind"],
                "requested_action": requested_action, "requested_action_applied": False,
                "host": "unknown" if observed is None else observed.disposition,
                "host_unit": request.unit_name}

def _active_runner() -> bool:
    from agent_run.paths import app_data_root
    active = (app_data_root() / "active" / "current").resolve()
    return Path(__file__).resolve().is_relative_to(active)


def _run(parsed: argparse.Namespace, *, manager: SystemdHost,
         environment: Mapping[str, str], runner_lock: Path) -> dict[str, Any]:
    """Freeze creation inputs outside the sole Core Task creation transaction."""
    from uuid import uuid4
    from agent_run.github import GhGitHubReader
    from agent_run.github_identity import identity_overrides
    from agent_run.managed_workspace import ManagedWorkspace
    from agent_run.task_identity import TaskKey
    from agent_run.user_defaults import UserDefaultsStore
    from .kernel import Kernel
    from .model import KernelError
    from .executor import command

    directory = Path.cwd()
    repository = parsed.repo or GhGitHubReader(working_directory=directory).repository_hint()
    if repository is None:
        raise KernelError("workspace_required", "Select --repo OWNER/NAME or a Git checkout")
    workspace = ManagedWorkspace.for_repository(repository)
    if parsed.state_dir is not None and parsed.state_dir.resolve() != workspace.state_root.resolve():
        raise KernelError("managed_state_identity", "run requires the repository's managed state directory")
    kernel = Kernel.open_task(TaskKey(workspace.repository_root, workspace.repository, parsed.parent))
    if kernel is None:
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
        kernel = Kernel.create_task(database, spec, requirements=requirements)
    with kernel.storage.transaction(write=False) as tx:
        run = tx.one("SELECT id,language,repository_root FROM runs")
        if run["language"] not in {"en", "zh"} or run["repository_root"] != str(workspace.repository_root):
            raise KernelError("creation_metadata_missing")
        parsed._run_language = run["language"]
    workspace.open_path()
    original = attach(kernel, requested_action="run", manager=manager, environment=environment, runner_lock=runner_lock)
    if original is not None:
        original["language"] = run["language"]
        return original
    action_id = uuid4().hex
    result = submit(kernel, "run", action_id=action_id,
        command=command(kernel.storage.path, action_id, runner_lock, environment),
        manager=manager, environment=environment, runner_lock=runner_lock)
    result["language"] = run["language"]
    return result


def execute(parsed: argparse.Namespace, *, language: str) -> dict[str, Any]:
    """Locate an exact managed Run and submit through sole Kernel admission."""
    import json
    import os
    import shutil
    from uuid import uuid4
    from agent_run.managed_workspace import workspace_for_root
    from agent_run.runner_lease import default_runner_lock_path, runner_usage_lease
    from .host_adapter import SystemdHost, HostUnavailable
    from .kernel import Kernel
    from .model import KernelError
    from .cli_selection import _candidates, _select
    from .remote_effects import RemoteEffects

    if parsed.command not in {"run", "resume", "approve", "stop", "revise", "requeue", "abandon"}:
        raise KernelError("lifecycle_not_connected", parsed.command)
    _require_github_host()
    policy_overrides = personal_settings._policy_values(parsed)
    environment = dict(os.environ)
    runner_lock = default_runner_lock_path(environment)
    with runner_usage_lease(runner_lock):
        if parsed.command == "run":
            if not _active_runner():
                raise HostUnavailable("Lifecycle commands require the installed Active Runner")
            manager = SystemdHost(systemctl=shutil.which("systemctl") or "systemctl",
                systemd_run=shutil.which("systemd-run") or "systemd-run", environment=environment)
            manager.preflight()
            return _run(parsed, manager=manager, environment=environment, runner_lock=runner_lock)
        selection = argparse.Namespace(**vars(parsed))
        selection.parent = None
        if selection.run_id.isdecimal():
            selection.parent = personal_settings._positive(selection.run_id, language=language)
            selection.run_id = None
        candidate = _select(selection, _candidates(selection))[0]
        database = Path(candidate["database"]).resolve()
        kernel = Kernel(database)
        with kernel.storage.transaction(write=False) as tx:
            run = tx.one("SELECT * FROM runs")
            if run["language"] not in {"zh", "en"} or run["repository_root"] is None:
                raise KernelError("creation_metadata_missing")
            parsed._run_language = run["language"]
            workspace = workspace_for_root(Path(run["repository_root"]))
            if not database.is_relative_to(workspace.state_root.resolve()):
                raise KernelError("managed_state_identity")
            from .task_location import read_task, task_path
            from agent_run.task_identity import TaskKey
            task = TaskKey(Path(run["repository_root"]), run["repository"], run["parent_issue"])
            if read_task(task_path(task)) != (run["id"], database):
                raise KernelError("task_run_unpublished", "This database is not the published Run for its Task")
            stored = tx.one("SELECT subject,material_id FROM run_inputs")
            inputs = json.loads(tx.get(stored["material_id"], stored["subject"]))
            abandoned = tx.rows("SELECT id,status FROM actions WHERE kind='abandon'", limit=1) if parsed.command == "abandon" else []
            abandonment = dict(abandoned[0]) if abandoned else None
            unknown_host = bool(parsed.command == "abandon" and tx.rows(
                "SELECT owner FROM hosts WHERE (status!='exited' OR usage_released=0) AND status!='running' LIMIT 1"))
        if parsed.command == "stop":
            subject = None
            if parsed.ticket is not None:
                with kernel.storage.transaction(write=False) as tx:
                    targets = tx.all("SELECT id FROM subjects WHERE kind='ticket' AND issue=?", (parsed.ticket,))
                    if len(targets) != 1:
                        raise KernelError("control_subject_ambiguous", "Select an exact Ticket")
                    subject = str(targets[0]["id"])
            action_id = uuid4().hex
            action_status = kernel.stop_task(action_id, subject=subject)
            return {"result": "accepted", "status": "action_" + action_status, "run_id": run["id"],
                    "action_id": action_id, "action": "stop", "host": "unknown", "language": run["language"]}
        inspections = None
        if parsed.command == "abandon":
            from .effects import LocalGit
            from .local_effects import LocalEffects
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
        manager = SystemdHost(systemctl=shutil.which("systemctl") or "systemctl",
                              systemd_run=shutil.which("systemd-run") or "systemd-run", environment=environment)
        manager.preflight()
        workspace.open_path()
        original = attach(kernel, requested_action=parsed.command, manager=manager,
                          environment=environment, runner_lock=runner_lock)
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
            from agent_run.github import GhGitHubReader
            from .source_reading import read_requirements
            reader = GhGitHubReader(workspace.repository, working_directory=workspace.repository_root,
                                    runner_identity=inputs["runner_identity"])
            kernel.refresh(lambda: read_requirements(reader, run["parent_issue"]))
            source = kernel.source_status()
        with kernel.storage.transaction(write=False) as tx:
            if parsed.command == "abandon":
                targets = []
            elif parsed.command == "requeue":
                if source["blocked"] == "final_refresh" and source["final_subject"] is not None:
                    targets = tx.all("SELECT id FROM subjects WHERE id=?", (source["final_subject"],))
                else:
                    targets = tx.all("SELECT DISTINCT s.id FROM subjects s JOIN gates g ON g.subject=s.id "
                                     "WHERE g.code='requeue_required' ORDER BY s.id")
            elif parsed.command == "approve":
                targets = tx.all("SELECT id FROM subjects WHERE kind!='ticket' AND phase='approval'")
            elif parsed.command == "revise":
                targets = tx.all("SELECT id FROM subjects WHERE kind='final_run' AND phase='approval'")
            elif getattr(parsed, "ticket", None) is not None:
                targets = tx.all("SELECT id FROM subjects WHERE kind='ticket' AND issue=?", (parsed.ticket,))
            elif run["stopped"]:
                targets = []
            else:
                targets = tx.all("SELECT DISTINCT s.id FROM subjects s JOIN gates g ON g.subject=s.id ORDER BY s.id")
            if len(targets) > 1 or (not targets and not (parsed.command == "abandon" or parsed.command == "resume" and run["stopped"])):
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
            from agent_run.github import GhGitHubReader
            from .source_reading import read_requirements
            reader = GhGitHubReader(workspace.repository, working_directory=workspace.repository_root,
                                    runner_identity=inputs["runner_identity"])
            kernel.refresh(lambda: read_requirements(reader, run["parent_issue"]))
        # Root owns the selected Executor command, including sealed loader facts.
        from .executor import command
        action_id = uuid4().hex
        if original is not None:
            request = kernel.host_request(original["action_id"])
            kernel.control_host(request, parsed.command, action_id, subject=subject, world=world,
                message=getattr(parsed, "message", None), new_thread=getattr(parsed, "new_thread", False),
                policy_overrides=policy_overrides, resource_inspections=inspections,
                replacement=replacement,
                discard_worktree=getattr(parsed, "discard_worktree", False))
            with kernel.storage.transaction(write=False) as tx:
                action_status = tx.one("SELECT status FROM actions WHERE id=?", (action_id,))[0]
            result = {"result": "accepted", "status": "action_" + action_status, "run_id": run["id"],
                "action_id": action_id, "action": parsed.command, "host": "running", "host_unit": request.unit_name}
        else:
            result = submit(kernel, parsed.command, action_id=action_id,
                            command=command(database, action_id, runner_lock, environment), manager=manager,
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
    from agent_run.github import GhGitHubReader
    from agent_run.git_errors import GitHubReadError
    from agent_run.github_retry import run_read_command
    from agent_run.git_output import run_git
    from agent_run.managed_workspace import ManagedWorkspace, read_git_identity
    from .configuration import freeze_creation
    from .model import RunSpec, KernelError
    from .remote_effects import RemoteEffects
    from .source_reading import read_requirements
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
    base, _ = port._commit(head)
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
