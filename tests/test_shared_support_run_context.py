"""Shared fact consumers over normal same-database Task/Run producers.

Only real Core control and read-only projection are exercised. No Agent,
credential preparation, Host, process receipt or physical exit is invented.
"""
from dataclasses import replace

import pytest

from agent_run.core.kernel import Kernel
from agent_run.errors import KernelError
from agent_run.model import Commit, Delivery, DeliveryKind, ParentIssue, Requirements, RunSpec
from agent_run.storage.routing import RepositoryRoute
from support import notification
from support.offline import fixture_inputs
from support.repository_fixture import create_kernel
from support.sqlite_faults import logical_state


@pytest.fixture
def runs(tmp_path):
    database = tmp_path / 'state' / 'kernel.sqlite3'
    delivery = Delivery('shared-subject', DeliveryKind.PARENT_ONLY, 1, 'frozen',
        Commit('a' * 40, 'b' * 40), 'refs/heads/A', 'refs/heads/main')
    spec = RunSpec('A', 'example/project', 1, 'r', 'g', (delivery,), language='en',
        repository_root=str(tmp_path / 'repository'), inputs=fixture_inputs())
    kernels = {}
    for name, parent in (('A', 1), ('B', 2)):
        current = replace(spec, run_id=name, parent_issue=parent,
            deliveries=(replace(delivery, issue=parent, source_ref='refs/heads/' + name),))
        kernels[name] = create_kernel(database, current,
            requirements=Requirements(ParentIssue(parent, 'Parent ' + name, 'Body ' + name), ()))
        assert kernels[name].control('stop', 'same-action', subject='shared-subject') == 'applied'
    return kernels


@pytest.mark.parametrize('mode', ['concise', 'detailed'])
def test_shared_notification_projection_selects_only_its_exact_run(runs, mode, monkeypatch):
    observed = []
    original = notification.project_event
    def project(tx, row, state, selected_mode):
        observed.append((row['run_id'], state['run_id'], state['parent']))
        return original(tx, row, state, selected_mode)
    monkeypatch.setattr(notification, 'project_event', project)
    before = logical_state(runs['A'].storage.path)
    projected = {}
    for name, parent in (('A', 1), ('B', 2)):
        observed.clear()
        projected[name] = notification._project(runs[name], mode)
        assert observed and all(row_run == state_run == name and source == {
            'number': parent, 'title': 'Parent ' + name} for row_run, state_run, source in observed)
    # Stop alone is not a Worker/Human Blocker event; do not invent activity.
    assert projected == {'A': [], 'B': []}
    assert logical_state(runs['A'].storage.path) == before
    assert runs['B'].control('resume', 'B-only', subject='shared-subject', message='Original B response') == 'applied'
    after_control = logical_state(runs['A'].storage.path)
    observed.clear()
    assert notification._project(runs['A'], mode) == projected['A']
    assert observed and all(row_run == state_run == 'A' for row_run, state_run, _ in observed)
    observed.clear()
    assert notification._project(runs['B'], mode) == []
    assert observed and all(row_run == state_run == 'B' for row_run, state_run, _ in observed)
    assert logical_state(runs['A'].storage.path) == after_control


def test_shared_notification_projection_never_selects_a_run_from_metadata(runs):
    route = RepositoryRoute.from_database(runs['A'].storage.path)
    metadata = Kernel._from_storage(route.metadata())
    before = logical_state(route.database)
    with pytest.raises(KernelError, match='run_scope_required'):
        notification._project(metadata, 'detailed')
    assert logical_state(route.database) == before


def test_shared_completion_projection_reads_only_the_original_run_history(runs, monkeypatch):
    observed = []
    original = notification.project_event
    def project(tx, row, state, mode):
        observed.append((row["run_id"], state["run_id"], mode))
        return original(tx, row, state, mode)
    monkeypatch.setattr(notification, "project_event", project)
    before = logical_state(runs["A"].storage.path)
    for selected in ("A", "B"):
        observed.clear()
        assert notification._completions(runs[selected]) == []
        # Core Stop produces genuine history, but no invented Completion.
        assert observed and all(row_run == state_run == selected and mode == "concise"
            for row_run, state_run, mode in observed)
    assert logical_state(runs["A"].storage.path) == before
    assert runs["B"].control("resume", "B-only-completion", subject="shared-subject",
        message="Keep the original B response") == "applied"
    after_control = logical_state(runs["A"].storage.path)
    observed.clear()
    assert notification._completions(runs["A"]) == []
    assert observed and all(row_run == state_run == "A" for row_run, state_run, _ in observed)
    assert logical_state(runs["A"].storage.path) == after_control


@pytest.mark.parametrize("selected", ["A", "B", "metadata"])
def test_shared_budget_reader_cannot_collapse_sibling_purpose_rows(runs, selected):
    from types import SimpleNamespace
    from support.core_operations import budgets
    kernel = (Kernel._from_storage(RepositoryRoute.from_database(runs["A"].storage.path).metadata())
              if selected == "metadata" else runs[selected])
    scenario = SimpleNamespace(kernel=kernel, delivery=SimpleNamespace(key="shared-subject"))
    before = logical_state(runs["A"].storage.path)
    if selected == "metadata":
        with pytest.raises(KernelError, match="run_scope_required"):
            budgets(scenario)
    else:
        observed = budgets(scenario)
        assert set(observed) == {"requirements", "integration", "ci"}
        assert all(row["run_id"] == selected and row["subject"] == "shared-subject" and row["current"] == 1
            for row in observed.values())
        assert all(row["window"] == 1 and row["developments_used"] == row["reviews_used"] == 0
            for row in observed.values())
        assert observed == budgets(scenario)
    assert logical_state(runs["A"].storage.path) == before
