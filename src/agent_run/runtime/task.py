"""Application composition for Task admission and exact Run opening.

Storage routes only locate facts. This entry chooses the default physical
workspace; callers adding an equivalent workspace can pass its explicit route
without changing the Task transaction rules.
"""
from pathlib import Path

from agent_run.adapters.workspace import ManagedWorkspace
from agent_run.contracts.task import TaskKey
from agent_run.core.kernel import Kernel
from agent_run.core.run_creation import creation_profiles
from agent_run.core.task_creation import (
    TaskRun, capture_binding, create_repository_task, ensure_idle_task,
    reconcile_repository_task, replaceable_in,
)
from agent_run.errors import KernelError
from agent_run.model import Requirements, RunSpec
from agent_run.storage.routing import RepositoryRoute
from agent_run.storage.tasks import read_repository_task, task_lock


def selected_kernel(route: RepositoryRoute, run_id: str) -> Kernel:
    return Kernel._from_storage(route.at_run(run_id))


def open_task(route: RepositoryRoute, task: TaskKey) -> Kernel | None:
    """Keep admission serialized while observing one canonical binding."""
    with task_lock(task):
        return _open_task(route, task)


def _open_task(route: RepositoryRoute, task: TaskKey) -> Kernel | None:
    """Observe replaceability in one snapshot; creation later rechecks it."""
    route._task(task)
    if not route.database.exists() and not route.database.is_symlink():
        route._require_vacant_root(route.database)
        return None
    metadata = route.metadata()
    with metadata.transaction(write=False) as owner:
        row = read_repository_task(owner, task)
        if row is None or row["current_run"] is None:
            return None
        run_id = row["current_run"]
        selected = capture_binding(owner, task, run_id)
        if replaceable_in(selected, task, run_id):
            return None
    return Kernel._from_storage(metadata.at_run(run_id))


def idle_task(route: RepositoryRoute, task: TaskKey) -> None:
    route._task(task)
    with route.metadata().transaction(ordered=False) as owner:
        ensure_idle_task(owner, task)


def create_task(route: RepositoryRoute, task: TaskKey, spec: RunSpec,
                requirements: Requirements) -> TaskRun:
    route._task(task)
    return create_repository_task(route.metadata(), task, spec, requirements)


def reconcile_task(route: RepositoryRoute, task: TaskKey, proposal_run_id: str, *,
                   deadline: float | None = None) -> TaskRun:
    route._task(task)
    return reconcile_repository_task(route.metadata(), task, proposal_run_id, deadline=deadline)


def create_managed_task(path: Path, spec: RunSpec, *, requirements: Requirements) -> Kernel:
    if spec.repository_root is None:
        raise KernelError("task_workspace_unknown")
    route = RepositoryRoute(path, Path(spec.repository_root), spec.repository)
    task = TaskKey(route.repository_root, route.repository, spec.parent_issue)
    workspace = ManagedWorkspace.for_repository(task.repository)
    if route != RepositoryRoute.for_workspace(workspace):
        raise KernelError("managed_state_identity")
    creation_profiles(spec)
    with task_lock(task):
        if not path.exists() and not path.is_symlink():
            route.initialize()
        result = create_task(route, task, spec, requirements)
        return selected_kernel(route, result.run_id)


def open_managed_task(task: TaskKey) -> Kernel | None:
    workspace = ManagedWorkspace.for_repository(task.repository)
    if workspace.repository_root.resolve() != task.workspace:
        raise KernelError("task_location_identity")
    return open_task(RepositoryRoute.for_workspace(workspace), task)
