"""Canonical decoder facts from unchanged v6 producers, without installing Core."""
from __future__ import annotations

import json
import hashlib
from contextlib import closing
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


@pytest.mark.parametrize('role', ['development', 'review', 'publication'])
def test_original_returned_decoder_preserves_exact_source_and_owned_resources(
    tmp_path: Path, git_repo: Path, monkeypatch: pytest.MonkeyPatch, role: str,
) -> None:
    import os
    import shutil
    from agent_run_kernel.migration_active import read_original_returned_import
    from agent_run_kernel.migration_cli import _collect_original_v6
    from support.v6_returned_source import returned_managed_source
    from test_kernel_migration_original_source import _git_bytes

    managed, paths = returned_managed_source(tmp_path, git_repo, role)
    before = paths['database'].read_bytes(), _git_bytes(managed.repository_root)
    # Supported inspection writes only evidence. All external reads use the
    # existing synthetic service; no real gh, Agent or source retirement.
    for key, value in {'PATH': str(tmp_path / 'close-service-bin') + os.pathsep + os.environ['PATH'],
        'V6_CLOSE_SERVICE': str(tmp_path / 'close-service.json'),
        'V6_CLOSE_CALLS': str(tmp_path / 'close-service-calls.jsonl'),
        'V6_CLOSE_REAL_GIT': str(shutil.which('git'))}.items():
        monkeypatch.setenv(key, value)
    evidence = tmp_path / 'evidence'
    _, snapshot = _collect_original_v6('example/project', 1, evidence, external=True)
    report = json.loads(paths['report'].read_bytes())
    facts = read_original_returned_import(snapshot, profile_snapshot=evidence / 'profile.json',
        external_snapshot=evidence / 'external-readproof.json', checkout_archive=evidence / 'checkout.tar')
    assert facts.source.mode == 'original' and facts.source.scope == 'single-ticket'
    assert facts.source.profile_document == paths['profile'].read_bytes()
    assert facts.source.external_readproof == (evidence / 'external-readproof.json').read_bytes()
    assert facts.source.task is not None and facts.source.task.workspace == managed.repository_root
    assert len(facts.subjects) == 1
    subject = facts.subjects[0]
    assert subject.subject == 'ticket:2' and subject.kind == 'returned'
    assert subject.record == SourceRecord('work_jobs', (report['run_id'], 'ticket:2:generation:1'))
    assert subject.completion is None and subject.historical_base is not None
    assert subject.budgets[0].developments_used == 1
    assert subject.budgets[0].reviews_used == int(role == 'publication')
    assert subject.attempts[-1].role.value == role and subject.attempts[-1].pending
    assert all(not attempt.pending for attempt in subject.attempts[:-1])
    assert all(attempt.execution is not None and attempt.continuation_basis is None for attempt in subject.attempts)
    assert len(subject.attempts) == {'development': 1, 'review': 2, 'publication': 3}[role]
    assert subject.resources[0].disposition == 'existing'
    assert subject.resources[0].checkout_archive_sha256 == hashlib.sha256((evidence / 'checkout.tar').read_bytes()).hexdigest()
    assert subject.resources[0].checkout_index_sha256 == hashlib.sha256((evidence / 'checkout-index').read_bytes()).hexdigest()
    assert subject.resources[0].before_head == ('ref: refs/heads/' + report['ticket_branch'] + '\n').encode()
    assert facts.run_resource.head_evidence == (SourceRecord('run_condition_base', (report['run_id'],)),)
    documents = dict(facts.source.oracle_documents)
    if role == 'development':
        intent = json.loads(documents['source:work_job_candidate_commit_intent'])
        assert intent['run_id'] == report['run_id'] and intent['job_id'] == 'ticket:2:generation:1'
        assert intent['value_kind'] == 'candidate_commit' and intent['value_attempt'] == 1
        assert intent['value_expected_head'] == subject.historical_base.sha
    else:
        assert 'source:work_job_candidate_commit_intent' not in documents
    assert before == (paths['database'].read_bytes(), _git_bytes(managed.repository_root))
    assert not (managed.state_root / report['run_id'] / 'kernel.sqlite3').exists()


@pytest.mark.parametrize('profile_backend', [False, True])
def test_completed_decoder_keeps_optional_execution_and_real_pending_cleanup(
    tmp_path: Path, git_repo: Path, monkeypatch: pytest.MonkeyPatch, profile_backend: bool,
) -> None:
    import os
    import shutil
    import subprocess
    import sys
    from agent_run.managed_workspace import ManagedWorkspace
    from agent_run_kernel.migration_completed import read_original_completed_import
    from agent_run_kernel.migration_cli import _collect_original_v6
    from test_kernel_migration_completed_history import _completed_source
    from test_kernel_migration_original_source import _git_bytes

    managed = ManagedWorkspace.for_repository('example/project')
    managed.root.mkdir(parents=True)
    git_repo.rename(managed.repository_root)
    managed.state_root.mkdir()
    LocalGit(managed.repository_root).run('remote', 'add', 'origin', 'https://github.com/example/project.git')
    (managed.root / '.agent-run-workspace.json').write_text(json.dumps({
        'repository': managed.repository, 'remote_url': 'https://github.com/example/project.git'}))
    paths = _completed_source(tmp_path, managed.repository_root, 2, state_root=managed.state_root,
        controlled=True, actual_github_close=True, profile_backend=profile_backend)
    before = paths['database'].read_bytes(), _git_bytes(managed.repository_root)
    with closing(sqlite3.connect(paths['database'])) as db:
        assert db.execute('SELECT status FROM runs').fetchone()[0] == 'waiting_external'
        assert db.execute("SELECT stage FROM work_jobs WHERE stage IS NOT NULL").fetchone()[0] == 'merged'
        assert db.execute('SELECT count(*) FROM work_invocations').fetchone()[0] == (5 if profile_backend else 0)
        assert db.execute('SELECT count(*) FROM work_job_ticket_close_ownership').fetchone()[0] == 0
    for key, value in {'PATH': str(tmp_path / 'close-service-bin') + os.pathsep + os.environ['PATH'],
        'V6_CLOSE_SERVICE': str(tmp_path / 'close-service.json'),
        'V6_CLOSE_CALLS': str(tmp_path / 'close-service-calls.jsonl'),
        'V6_CLOSE_REAL_GIT': str(shutil.which('git'))}.items():
        monkeypatch.setenv(key, value)
    evidence = tmp_path / 'evidence'
    _, snapshot = _collect_original_v6('example/project', 1, evidence, external=True)
    facts = read_original_completed_import(snapshot, profile_snapshot=evidence / 'profile.json',
        external_snapshot=evidence / 'external-readproof.json')
    subject = facts.subjects[0]
    assert subject.kind == 'completed' and subject.completion is not None
    assert facts.requirements.issues[0].state == 'OPEN'
    assert facts.observed_requirements.issues[0].state == 'CLOSED'
    assert facts.observed_requirements.issues[0].body == facts.requirements.issues[0].body
    assert len(subject.attempts) == 3  # Existing Completion support, not all retained history.
    assert [attempt.role_ordinal for attempt in subject.attempts] == [2, 2, 1]
    assert all(not attempt.pending and attempt.continuation_basis is None for attempt in subject.attempts)
    assert all((attempt.execution is not None) == profile_backend for attempt in subject.attempts)
    assert all((attempt.bound_version is not None) == profile_backend for attempt in subject.attempts)
    if not profile_backend:
        assert json.loads(facts.source.profile_document)['bindings'] == []
        assert json.loads(subject.attempts[-1].raw_result)['thread_id'] == 'history-development'
    assert (subject.budgets[0].developments_used, subject.budgets[0].reviews_used) == (2, 2)
    assert subject.completion.close_ownership is None  # No invented source ownership row.
    assert subject.completion.close_readproof is not None
    assert subject.resources[0].disposition == 'existing' and subject.resources[0].cleanup_pending
    assert subject.resources[0].expected_oid == subject.completion.publication.sha
    assert subject.resources[0].checkout is not None and subject.resources[0].checkout.is_dir()
    assert subject.resources[0].checkout_archive_sha256 is None
    assert facts.run_resource.expected_oid == subject.completion.integrated.sha
    assert facts.run_resource.head_evidence == (subject.record,)
    assert before == (paths['database'].read_bytes(), _git_bytes(managed.repository_root))
    assert not (managed.state_root / facts.spec.run_id / 'kernel.sqlite3').exists()
    # The same untouched source must also pass the pre-existing formal path.
    # This is a normal migration countercheck, not decoder self-certification.
    formal = subprocess.run([sys.executable, '-m', 'agent_run_kernel.migration_cutover',
        '--repo', 'example/project', '--parent', '1', '--evidence-dir', str(tmp_path / 'formal-countercheck')],
        env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1',
             'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')},
        capture_output=True, text=True, timeout=30)
    assert formal.returncode == 0, formal.stdout + formal.stderr
    target = Path(json.loads(formal.stdout)['target']['database'])
    with closing(sqlite3.connect(target)) as db:
        assert db.execute('SELECT count(*) FROM invocations').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM threads').fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM resource_cleanup WHERE state='pending'").fetchone()[0] == 1


@pytest.mark.parametrize('second_returned', [False, True])
def test_whole_top_decoder_keeps_one_source_scope_and_each_owners_historical_base(
    tmp_path: Path, git_repo: Path, monkeypatch: pytest.MonkeyPatch, second_returned: bool,
) -> None:
    import os
    import shutil
    from agent_run_kernel.migration_cli import _collect_original_v6
    from agent_run_kernel.migration_run import read_original_run_import
    from test_kernel_migration_original_source import _git_bytes

    managed, paths = whole_run_managed_source(tmp_path, git_repo, second_returned=second_returned)
    report = json.loads(paths['report'].read_bytes())
    before = paths['database'].read_bytes(), _git_bytes(managed.repository_root)
    for key, value in {'PATH': str(tmp_path / 'close-service-bin') + os.pathsep + os.environ['PATH'],
        'V6_CLOSE_SERVICE': str(tmp_path / 'close-service.json'),
        'V6_CLOSE_CALLS': str(tmp_path / 'close-service-calls.jsonl'),
        'V6_CLOSE_REAL_GIT': str(shutil.which('git'))}.items():
        monkeypatch.setenv(key, value)
    evidence = tmp_path / 'evidence'
    _, snapshot = _collect_original_v6('example/project', 1, evidence, external=True)
    facts = read_original_run_import(snapshot, evidence / 'profile.json',
        external_snapshot=evidence / 'external-readproof.json', checkouts_snapshot=evidence / 'checkouts.json')
    assert facts.source.mode == 'original' and facts.source.scope == 'whole-run'
    assert facts.source.task is not None and facts.source.task.workspace == managed.repository_root
    assert facts.source.profile_document == paths['profile'].read_bytes()
    assert facts.source.external_readproof == (evidence / 'external-readproof.json').read_bytes()
    a, b, c = facts.subjects
    assert [(subject.issue, subject.kind) for subject in facts.subjects] == [
        (2, 'completed'), (3, 'returned'), (4, 'returned' if second_returned else 'unallocated')]
    assert a.historical_base.sha == report['default_base']
    assert b.historical_base.sha == report['a_integrated'] != report['default_base']
    assert [len(subject.attempts) for subject in facts.subjects] == [3, 2, 2 if second_returned else 0]
    assert sum(len(subject.attempts) for subject in facts.subjects) == len(report['calls'])
    assert a.completion is not None and a.completion.close_readproof is not None
    assert json.loads(a.completion.close_ownership)['value_event_id'] == 4102
    assert a.resources[0].disposition == 'absent'
    assert b.resources[0].disposition == 'existing' and b.resources[0].checkout_archive_sha256
    assert b.attempts[-1].pending and not b.attempts[-1].accounted
    assert json.loads(b.attempts[-1].allocation_boundary)['base_sha'] == report['a_integrated']
    if second_returned:
        assert c.historical_base.sha == report['a_integrated']
        assert c.resources[0].disposition == 'existing' and c.resources[0].checkout_index_sha256
        assert c.attempts[-1].pending
    else:
        assert c.historical_base is None and c.resources[0].disposition == 'absent'
        assert c.record.table == 'run_graph_members' and c.budgets == ()
    assert all(attempt.continuation_basis is None for subject in facts.subjects for attempt in subject.attempts)
    assert facts.run_resource.expected_oid == report['a_integrated']
    assert facts.run_resource.head_evidence == (a.record,)
    assert len(facts.spec.deliveries) == 4 and len(facts.subjects) == 3  # Final is only a target definition.
    assert before == (paths['database'].read_bytes(), _git_bytes(managed.repository_root))
    assert not (managed.state_root / report['run_id'] / 'kernel.sqlite3').exists()
