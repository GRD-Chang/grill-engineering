"""Real read/commit interleavings cannot publish mixed source observations."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from threading import Event

import pytest

from agent_run_kernel import Kernel, KernelError
from agent_run_kernel.model import Blocker, Issue, ParentIssue, Requirements
from support.offline import GraphScenario
from agent_run_kernel.operation_runner import ReadUnavailable
from support.core_operations import CoreOperations


def observation_run(path: Path) -> tuple[GraphScenario, Requirements, Requirements]:
    old = Requirements(ParentIssue(1, "Parent", "Requirement", (2,)), (
        Issue(2, "Ticket", "Work", "OPEN", frozenset({"ready-for-agent"}), (Blocker(9, "CLOSED"),)),
    ))
    new = replace(old, issues=(replace(old.issues[0], blocked_by=(Blocker(9, "OPEN"),)),))
    scenario = GraphScenario(path.parent / (path.stem + "-source"), old)
    return scenario, old, new


def test_late_source_read_cannot_reopen_a_newly_observed_external_blocker(tmp_path: Path, monkeypatch) -> None:
    old = Requirements(ParentIssue(1, "Parent", "Requirement", (2,)), (
        Issue(2, "Ticket", "Work", "OPEN", frozenset({"ready-for-agent"}), (Blocker(9, "CLOSED"),)),
    ))
    new = replace(old, issues=(replace(old.issues[0], blocked_by=(Blocker(9, "OPEN"),)),))
    scenario = GraphScenario(tmp_path / "source", old)
    kernel = scenario.kernel
    operations = CoreOperations(kernel, scenario.execution, scenario.make_ports)
    reading, release = Event(), Event()

    def read_old(parent):
        assert parent == 1
        reading.set()
        assert release.wait(10), "source read barrier was not released"
        return old

    monkeypatch.setattr(operations.runner.ports, "source", read_old)
    with ThreadPoolExecutor(max_workers=1) as workers:
        late = workers.submit(operations.runner.observe, "ticket:2")
        try:
            assert reading.wait(10), "original source read never started"
            scenario.requirements = new
            fresh_runner = kernel.operation_runner(scenario.execution, scenario.make_ports)
            fresh_runner.observe("ticket:2")
            fresh = kernel.source_status()
            assert fresh["remaining"] == {"ticket:2": "blocked_by_open_issues"}
            release.set()
            error = late.exception(timeout=10)
            assert isinstance(error, KernelError) and error.code == "automatic_step_superseded"
        finally:
            release.set()
    assert kernel.source_status() == fresh
    assert fresh_runner.run_once().call is None
    assert kernel.status("ticket:2")["pending_attempt"] is None
    history = kernel.history("ticket:2")

    def failed_read(parent):
        raise ReadUnavailable("github_read_failed", "external source unavailable")

    monkeypatch.setattr(operations.runner.ports, "source", failed_read)
    with pytest.raises(ReadUnavailable, match="external source unavailable"):
        operations.runner.observe("ticket:2")
    assert kernel.source_status() == fresh
    assert kernel.history("ticket:2") == history


def test_refresh_returns_its_own_receipt_when_another_read_commits_after_it(tmp_path: Path, monkeypatch) -> None:
    scenario, old, new = observation_run(tmp_path / "kernel.sqlite3")
    # The runner now owns the source receipt and rejects an operation superseded
    # after its commit; it must never return a World assembled from two reads.
    kernel = scenario.kernel
    original = CoreOperations(kernel, scenario.execution, scenario.make_ports).runner
    scenario.requirements = replace(old, issues=(replace(old.issues[0], labels=old.issues[0].labels | {"triaged"}),))
    committed, release = Event(), Event()
    transaction = kernel.storage.transaction

    @contextmanager
    def after_commit(*, write=True):
        with transaction(write=write) as tx:
            yield tx
        if write and kernel.source_status()["observation"] > 1:
            committed.set()
            assert release.wait(10), "receipt barrier was not released"

    monkeypatch.setattr(kernel.storage, "transaction", after_commit)
    with ThreadPoolExecutor(max_workers=1) as workers:
        first = workers.submit(original.observe, "ticket:2")
        try:
            assert committed.wait(10), "original source observation never committed"
            first_receipt = Kernel(kernel.storage.path).source_status()
            assert first_receipt["frontier"] == ["ticket:2"] and first_receipt["remaining"] == {}
            scenario.requirements = new
            other = Kernel(kernel.storage.path).operation_runner(scenario.execution, scenario.make_ports)
            other.observe("ticket:2")
            newer = Kernel(kernel.storage.path).source_status()
            release.set()
            error = first.exception(timeout=10)
            assert isinstance(error, KernelError) and error.code == "automatic_step_superseded"
        finally:
            release.set()
    assert first_receipt["observation"] < newer["observation"]
    assert Kernel(kernel.storage.path).source_status() == newer


def test_graph_world_keeps_the_receipt_from_its_own_source_read(tmp_path: Path, monkeypatch) -> None:
    source = Requirements(ParentIssue(1, "Parent", "Original requirement", (2,)), (
        Issue(2, "Ticket", "Original work", "OPEN", frozenset({"ready-for-agent"})),
    ))
    scenario = GraphScenario(tmp_path / "scenario", source)
    runner = CoreOperations(scenario.kernel, scenario.execution, scenario.make_ports).runner
    world = runner.observe("ticket:2")
    receipt = scenario.kernel.source_status()
    assert (world.parent_revision, world.graph_revision, world.revision) == (
        receipt["observed_parent_revision"], receipt["observed_graph_revision"], receipt["observed_issues"][0]["revision"])
    assert world.completion_revision == receipt["completion_revisions"][0]["completion_revision"]
    newer = replace(source, parent=replace(source.parent, body="New requirement"),
                    issues=(replace(source.issues[0], body="New work", blocked_by=(Blocker(9, "OPEN"),)),))
    delivery = runner.ports.delivery

    def another_source_during_delivery(request):
        scenario.requirements = newer
        other = scenario.kernel.operation_runner(scenario.execution, scenario.make_ports)
        other.observe("ticket:2")
        return delivery(request)

    monkeypatch.setattr(runner.ports, "delivery", another_source_during_delivery)
    with pytest.raises(KernelError, match="automatic_step_superseded"):
        runner.observe("ticket:2")
    assert scenario.kernel.source_status()["observation"] > receipt["observation"]
