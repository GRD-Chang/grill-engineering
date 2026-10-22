"""Canonical decoder facts from unchanged v6 producers, without installing Core."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agent_run_kernel.effects import LocalGit
from agent_run_kernel.migration import capture_v6
from agent_run_kernel.migration_conversion import read_initial_import
from agent_run_kernel.migration_facts import SourceRecord
from agent_run_kernel.model import Profile
from test_kernel_migration import original_v6  # noqa: F401


@pytest.mark.parametrize("numbers", [(2,), (2, 3)])
def test_idle_copy_decodes_source_graph_without_allocating_work(
    tmp_path: Path, git_repo: Path, original_v6: Any, numbers: tuple[int, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The unchanged producer may enumerate persisted mapping keys through a
    # set. Keep this normal accepted-order fixture repeatable; the decoder
    # continues refusing a report/slot order that differs from source scope.
    monkeypatch.setenv("PYTHONHASHSEED", "0")
    old_root = git_repo / ".agent-run"
    fixture = Path(original_v6("fixture", root=old_root,
        fixture_path=git_repo / "github.json", issues={str(number): {
            "number": number, "title": f"Ticket {number}", "body": f"Deliver {number}.",
            "state": "OPEN", "labels": ["ready-for-agent"], "blocked_by": [],
        } for number in numbers}))
    state = original_v6("start", root=old_root, git=git_repo, fixture=fixture)["state"]
    source = old_root / "state.sqlite3"
    before = source.read_bytes()
    snapshot = capture_v6(source, tmp_path / "source.sqlite3")
    git = LocalGit(git_repo)
    work = tmp_path / "work"
    work.mkdir()
    workspaces = {}
    for number in numbers:
        checkout = work / f"ticket-{number}"
        git.run("worktree", "add", "--detach", str(checkout), state["base"]["sha"])
        workspaces[number] = (f"refs/heads/decoded/{number}", checkout)
    refs = git.run("show-ref").stdout
    layout = git.run("worktree", "list", "--porcelain").stdout
    facts = read_initial_import(snapshot, repository_root=git_repo,
        source_ref=workspaces[2][0], checkout=workspaces[2][1], final_checkout=work / "final",
        development_profile=Profile("explicit-development", "high"),
        review_profile=Profile("explicit-review", "medium"),
        publication_profile=Profile("explicit-publication", "low"),
        additional_workspaces={number: item for number, item in workspaces.items() if number != 2})

    assert facts.source.snapshot == snapshot
    assert facts.source.mode == "copied"
    assert facts.source.scope == ("single-ticket" if len(numbers) == 1 else "selected-graph")
    assert facts.source.task is None and facts.source.profile_document is None
    assert facts.source.checkout_uuid == state["checkout_identity"]
    assert facts.source.external_readproof is None
    assert facts.spec.run_id == state["run_id"]
    assert facts.requirements == facts.observed_requirements
    assert facts.requirements.parent.sub_issue_numbers == numbers
    assert [subject.issue for subject in facts.subjects] == list(numbers)
    assert all(subject.kind == "unallocated" and not subject.attempts and not subject.budgets
               and subject.historical_base is None and subject.completion is None
               for subject in facts.subjects)
    assert SourceRecord("work_job_layout", (state["run_id"],)) in facts.subjects[0].aliases
    assert all(subject.record.table == "run_graph_members" for subject in facts.subjects)
    assert all(subject.resources[0].disposition == "new-unused" for subject in facts.subjects)
    assert all(subject.resources[0].expected_oid is None and not subject.resources[0].cleanup_pending
               for subject in facts.subjects)
    assert all(subject.resources[0].before_head == (state["base"]["sha"] + "\n").encode("ascii")
               for subject in facts.subjects)
    assert facts.run_resource.disposition == "existing"
    assert facts.run_resource.expected_oid == state["base"]["sha"]
    assert [event.kind for event in facts.history] == ["v6_run_status", "v6_ticket_phase"]
    assert json.loads(facts.history[1].body)["values"]["value_ticket"] == 2
    assert facts.spec.inputs is not None
    assert facts.spec.policy.review_profile.model == "explicit-review"
    assert source.read_bytes() == before == snapshot.path.read_bytes()
    assert git.run("show-ref").stdout == refs
    assert git.run("worktree", "list", "--porcelain").stdout == layout
    assert not (work / "final").exists()
    assert not list(tmp_path.glob("kernel*.sqlite3"))
