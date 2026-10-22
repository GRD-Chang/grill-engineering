"""Canonical decoder facts from unchanged v6 producers, without installing Core."""
from __future__ import annotations

import json
import hashlib
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from agent_run_kernel.effects import LocalGit
from agent_run_kernel.migration import MigrationRefused, capture_v6
from agent_run_kernel.migration_conversion import convert_initial_v6, read_initial_import
from agent_run_kernel.migration_facts import SourceRecord
from agent_run_kernel.model import Profile
from test_kernel_migration import original_v6  # noqa: F401
from support.v6_whole_run_source import whole_run_managed_source


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


@pytest.mark.parametrize('entry', ['decoder', 'converter'])
def test_idle_copy_keeps_original_notification_journal_boundary(
    tmp_path: Path, git_repo: Path, original_v6: Any, entry: str,
) -> None:
    old_root = git_repo / '.agent-run'
    fixture = Path(original_v6('fixture', root=old_root,
        fixture_path=git_repo / 'github.json', issues={'2': {
            'number': 2, 'title': 'Ticket', 'body': 'Deliver.', 'state': 'OPEN',
            'labels': ['ready-for-agent'], 'blocked_by': [],
        }}))
    state = original_v6('start', root=old_root, git=git_repo, fixture=fixture)['state']
    source = old_root / 'state.sqlite3'
    before = source.read_bytes()
    snapshot = capture_v6(source, tmp_path / 'source.sqlite3')
    journal = old_root / 'notifications' / (hashlib.sha256(state['run_id'].encode()).hexdigest()[:32] + '.json')
    journal.parent.mkdir()
    journal.write_text('{"seen":[]}', encoding='utf-8')
    kwargs = dict(repository_root=git_repo, source_ref='refs/heads/decoded/ticket',
        checkout=tmp_path / 'unused-work', final_checkout=tmp_path / 'unused-final',
        development_profile=Profile('explicit-development', 'high'),
        review_profile=Profile('explicit-review', 'medium'),
        publication_profile=Profile('explicit-publication', 'low'))
    with pytest.raises(MigrationRefused, match='^unsupported_source_notification_journal$'):
        if entry == 'decoder':
            read_initial_import(snapshot, **kwargs)
        else:
            convert_initial_v6(snapshot, tmp_path / 'kernel.sqlite3', **kwargs)
    assert source.read_bytes() == before == snapshot.path.read_bytes()
    assert journal.read_text() == '{"seen":[]}'
    assert not (tmp_path / 'kernel.sqlite3').exists()


def test_whole_owner_decoder_keeps_historical_base_versions_and_source_outcomes(
    tmp_path: Path, git_repo: Path,
) -> None:
    from agent_run_kernel.migration_active import _read_profile
    from agent_run_kernel.migration_run_attempts import read_imported_owner_attempts
    from agent_run_kernel.migration_run_facts import (
        read_run_header, read_ticket_budget, read_ticket_owners,
    )

    managed, paths = whole_run_managed_source(tmp_path, git_repo)
    report = json.loads(paths['report'].read_bytes())
    original = paths['database'].read_bytes()
    snapshot = capture_v6(paths['database'], tmp_path / 'source.sqlite3')
    profile, profile_sha = _read_profile(paths['profile'])
    decoded = {}
    with sqlite3.connect(snapshot.path.as_uri() + '?mode=ro&immutable=1', uri=True) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        header = read_run_header(db, profile, profile_sha, snapshot.sha256)
        for owner in read_ticket_owners(db, header):
            if owner.job is None:
                assert owner.ticket == 4
                continue
            job = db.execute('SELECT * FROM work_jobs WHERE run_id=? AND job_id=?', owner.job.key).fetchone()
            calls = read_imported_owner_attempts(db, header, owner, read_ticket_budget(db, header, owner), profile,
                historical_base=job['value_base_sha'], effective_revision=job['value_effective_revision'],
                completed=job['stage'] == 'completed')
            decoded[owner.ticket] = calls
            for call in calls:
                assert call.record.table == 'work_attempts' and call.record.key[0] == report['run_id']
                assert call.raw_record.table == 'work_evidence'
                raw = db.execute('SELECT m.body FROM work_evidence e JOIN work_evidence_materials m '
                    'ON m.material_id=e.material_id WHERE e.run_id=? AND e.evidence_id=?', call.raw_record.key).fetchone()[0]
                assert call.raw_result == raw.encode('utf-8')
                assert call.continuation_basis is None and call.execution is not None
                assert call.execution.record.table == 'work_invocations'
                assert call.execution.record.key[1] == owner.subject
                invocation = json.loads(call.execution.invocation)
                assert invocation['binding_id'] == call.execution.binding_id
                assert invocation['reported_thread_id'] == call.execution.thread_id
                boundary = json.loads(call.allocation_boundary)
                assert boundary['base_sha'] == (report['default_base'] if owner.ticket == 2 else report['a_integrated'])
                assert json.loads(call.bound_version)['version_id'] == invocation['attempt_version_id']
                assert json.loads(call.selected_version)['version_id'] != invocation['attempt_version_id']
    assert [call.outcome for call in decoded[2]] == ['candidate', 'acceptance_artifact', 'publication_artifact']
    assert [call.pending for call in decoded[2]] == [False, False, False]
    assert [call.accounted for call in decoded[2]] == [False, True, False]
    assert [call.window for call in decoded[2]] == [1, 1, None]
    assert [call.outcome for call in decoded[3]] == ['candidate', None]
    assert [call.pending for call in decoded[3]] == [False, True]
    assert [call.accounted for call in decoded[3]] == [False, False]
    assert report['default_base'] != report['a_integrated']
    assert paths['database'].read_bytes() == original == snapshot.path.read_bytes()
    assert managed.open_path() == managed.repository_root
