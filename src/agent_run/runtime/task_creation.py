"""Freeze Task creation input and physical workspace resources outside Core."""
from __future__ import annotations
from collections.abc import Mapping
import os
from pathlib import Path
import re
from typing import Any, TYPE_CHECKING
from agent_run.errors import error_message
from agent_run.model import Commit, Delivery, DeliveryKind, Requirements, RunSpec
if TYPE_CHECKING:
    from agent_run.config.defaults import UserDefaultsStore
    from agent_run.contracts.agent_profiles import ProfileOverrides

def _require_github_host() -> None:
    if os.environ.get("GH_HOST", "github.com").lower() not in {"", "github.com"}:
        raise ValueError(error_message("cli.error.github_host"))


def creation_resources(run_id: str, requirements: Requirements, base: Commit,
                       default_ref: str, root: Path) -> tuple[Delivery, ...]:
    """Name exact resources, without creating refs/worktrees or choosing readiness.

    Every native sub-Issue is represented in its original order, including closed
    or blocked Issues. Core alone interprets their source facts and Graph.
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
    from agent_run.adapters.github_transport import GitHubReadTransport
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
    port = GitHubReadTransport(root, workspace.repository, runner_identity=frozen.inputs.runner_identity)
    head = actual_repository.default_head_sha
    if not isinstance(head, str) or not head:
        raise KernelError("missing_target_branch", "The observed default branch has no exact commit")
    base = port.commit(head)[0]
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
                                runner_identity=frozen.inputs.runner_identity, complete_stdout=True)
    if response.returncode:
        raise GitHubReadError("github_read_failed", "Creation resource refs cannot be observed")
    if response.stdout.strip():
        raise KernelError("creation_resource_occupied", "A generated remote ref already exists")
    for job in jobs:
        path = Path(job.checkout)
        if path.exists() or path.is_symlink():
            raise KernelError("creation_resource_occupied", str(path))
    return workspace.state_root / "kernel.sqlite3", replace(frozen, deliveries=jobs), requirements
