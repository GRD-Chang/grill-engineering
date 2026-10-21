"""Real read/commit interleavings cannot change source authority or receipts."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from threading import Event
from typing import Iterator

import pytest

from agent_run_kernel import Commit, Delivery, DeliveryKind, Kernel, KernelError, RunSpec
from agent_run_kernel.model import Blocker, Issue, ParentIssue, Requirements, World
from agent_run_kernel.storage import Transaction


def observation_run(path: Path) -> tuple[Kernel, Requirements, Requirements]:
    old = Requirements(ParentIssue(1, "Parent", "Requirement", (2,)), (
        Issue(2, "Ticket", "Work", "OPEN", frozenset({"ready-for-agent"}), (Blocker(9, "CLOSED"),)),
    ))
    new = replace(old, issues=(replace(old.issues[0], blocked_by=(Blocker(9, "OPEN"),)),))
    base = Commit("base", "tree")
    delivery = Delivery("ticket:2", DeliveryKind.TICKET, 2, "untrusted", base, "ticket", "run")
    final = Delivery("final:1", DeliveryKind.FINAL_RUN, 1, "untrusted", base, "run", "main")
    kernel = Kernel.create(path, RunSpec("run", "example/repository", 1, "untrusted", "untrusted", (delivery, final)), requirements=old)
    return kernel, old, new


def test_late_source_read_cannot_reopen_a_newly_observed_external_blocker(tmp_path: Path) -> None:
    kernel, old, new = observation_run(tmp_path / "kernel.sqlite3")
    reading, release = Event(), Event()

    def read_old() -> Requirements:
        reading.set()
        assert release.wait(10), "source read barrier was not released"
        return old

    with ThreadPoolExecutor(max_workers=1) as workers:
        late = workers.submit(kernel.refresh, read_old)
        try:
            assert reading.wait(10), "original source read never started"
            fresh = Kernel(kernel.storage.path).refresh(lambda: new)
            assert fresh["remaining"] == {"ticket:2": "blocked_by_open_issues"}
            release.set()
            error = late.exception(timeout=10)
            assert isinstance(error, KernelError) and error.code == "source_observation_changed"
        finally:
            release.set()
    assert kernel.source_status() == fresh
    world = World(fresh["parent_revision"], fresh["graph_revision"], kernel.delivery("ticket:2").revision, Commit("base", "tree"))
    assert kernel.advance("ticket:2", kernel.claim_executor("owner"), world).reason == "blocked_by_open_issues"
    history = kernel.history("ticket:2")

    def failed_read() -> Requirements:
        raise OSError("external source unavailable")

    with pytest.raises(OSError, match="external source unavailable"):
        kernel.refresh(failed_read)
    assert kernel.source_status() == fresh
    assert kernel.history("ticket:2") == history


def test_refresh_returns_its_own_receipt_when_another_read_commits_after_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel, old, new = observation_run(tmp_path / "kernel.sqlite3")
    committed, release = Event(), Event()
    transaction = kernel.storage.transaction

    @contextmanager
    def after_commit(*, write: bool = True) -> Iterator[Transaction]:
        with transaction(write=write) as tx:
            yield tx
        if write:
            committed.set()
            assert release.wait(10), "receipt barrier was not released"

    monkeypatch.setattr(kernel.storage, "transaction", after_commit)
    with ThreadPoolExecutor(max_workers=1) as workers:
        original = workers.submit(kernel.refresh, lambda: old)
        try:
            assert committed.wait(10), "original source observation never committed"
            newer = Kernel(kernel.storage.path).refresh(lambda: new)
            release.set()
            receipt = original.result(timeout=10)
        finally:
            release.set()
    assert receipt["frontier"] == ["ticket:2"]
    assert receipt["remaining"] == {}
    assert receipt["observation"] < newer["observation"]
    assert Kernel(kernel.storage.path).source_status() == newer
