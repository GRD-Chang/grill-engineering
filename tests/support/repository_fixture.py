"""Explicit final-protocol fixture producer, never an old-state adapter.

Small domain tests supply synthetic source/configuration here; the real atomic
Task/Run producer still validates and commits every relation. Managed CLI tests
use their actual workspace bootstrap instead. No guessing or Run reopening is
provided by this module.
"""
from dataclasses import replace
from pathlib import Path

from agent_run.contracts.task import TaskKey
from agent_run.core.kernel import Kernel
from agent_run.model import Blocker, Delivery, DeliveryKind, Issue, ParentIssue, Profile, Requirements, Role, RunSpec
from agent_run.storage.routing import RepositoryRoute
from agent_run.storage.sqlite import Storage


def create_kernel(path: Path, spec: RunSpec, *, requirements: Requirements | None = None) -> Kernel:
    from support.offline import fixture_inputs

    inputs = spec.inputs
    if inputs is None:
        inputs = fixture_inputs()
        profiles = {'preset': None, 'profiles': {
            role.value: {'model': getattr(spec.policy, role.value + '_profile').model,
                         'reasoning_effort': (inputs.profiles['profiles'][role.value]['reasoning_effort']
                             if getattr(spec.policy, role.value + '_profile').effort == Profile().effort
                             else getattr(spec.policy, role.value + '_profile').effort),
                         'reference': None, 'provenance': {'source': 'explicit-fixture-policy'}} for role in Role}}
        inputs = replace(inputs, profiles=profiles)
    spec = replace(spec, inputs=inputs, language=spec.language or 'zh',
                   repository_root=spec.repository_root or str(path.parent.resolve()))
    if requirements is None:
        tickets = tuple(item for item in spec.deliveries if item.kind == DeliveryKind.TICKET)
        numbers = {item.key: item.issue for item in tickets}
        requirements = Requirements(ParentIssue(spec.parent_issue, 'Parent', 'Explicit synthetic requirement',
            tuple(item.issue for item in tickets)), tuple(Issue(item.issue, 'Ticket', item.revision,
                'OPEN', frozenset({'ready-for-agent'}),
                tuple(Blocker(numbers[key], 'OPEN') for key in item.dependencies)) for item in tickets))
        if tickets and not any(item.kind == DeliveryKind.FINAL_RUN for item in spec.deliveries):
            first = tickets[0]
            spec = replace(spec, deliveries=(*spec.deliveries, Delivery('fixture-final', DeliveryKind.FINAL_RUN,
                spec.parent_issue, 'unobserved', first.base, first.target_ref, 'refs/heads/fixture-main')))
    route = RepositoryRoute(path.absolute(), Path(spec.repository_root), spec.repository)
    # Explicit fixture file creation is exclusive; it does not discover/adopt
    # neighboring fixture files or relax managed-workspace refusal at the CLI.
    if not path.exists() and not path.is_symlink():
        Storage.create(path, namespace=(route.repository, str(route.repository_root)))
    selected = route.create(TaskKey(route.repository_root, route.repository, spec.parent_issue), spec, requirements)
    return route.kernel(selected.run_id)


def read_binding(task: TaskKey, database: Path) -> tuple[str, Path] | None:
    """Read one exact canonical Task binding, without a locator or file mutation."""
    if not database.exists() and not database.is_symlink():
        return None
    route = RepositoryRoute.from_database(database)
    location = route.task(task)
    if location is None or location.current_run is None:
        return None
    return location.current_run, database.resolve()


def managed_bindings() -> list[tuple[str, Path]]:
    """Read the product's bounded managed registry in an isolated test HOME."""
    from types import SimpleNamespace
    from agent_run.cli.selection import managed_repository_routes
    routes = managed_repository_routes(SimpleNamespace(repo=None, state_dir=None, data_root=None,
        run_id='explicit-test-registry-enumeration'))
    result = []
    for route in routes:
        after = ''
        while True:
            page = route.discover(after=after)
            result.extend((item.run_id, item.database.resolve()) for item in page.items if item.current)
            if not page.has_more:
                break
            assert page.next_after is not None and page.next_after > after
            after = page.next_after
    return result


def foreign_task_run(kernel: Kernel, spec: RunSpec, requirements: Requirements) -> Kernel:
    """A legitimate sibling Task with colliding subject labels, never a rebound pointer."""
    parent = spec.parent_issue + 10000
    deliveries = tuple(replace(item, issue=parent) if item.kind in {
        DeliveryKind.PARENT_ONLY, DeliveryKind.FINAL_RUN} else item for item in spec.deliveries)
    source = replace(requirements, parent=replace(requirements.parent, number=parent))
    return create_kernel(kernel.storage.path, replace(spec, run_id='other-run', parent_issue=parent,
        deliveries=deliveries), requirements=source)
