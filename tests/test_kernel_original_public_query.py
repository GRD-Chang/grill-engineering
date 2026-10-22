"""Formal selectors/rendering read original, copied WAL and imported records."""
from __future__ import annotations

from contextlib import closing
import io
import json
import os
from pathlib import Path
import select
import shutil
import sqlite3
import subprocess
import sys

import pytest

from agent_run.managed_workspace import ManagedWorkspace
from agent_run_kernel import Kernel
from agent_run_kernel.migration import capture_v6
from agent_run_kernel.migration_records import V6RecordReader, preserve_v6_records
from agent_run_kernel.query_cli import main
from support.frozen_v6_runtime import old_v6_runtime


_TERMINAL = r'''
import json,sys
from pathlib import Path
from agent_run.run_publication import RunPublicationEngine
from agent_run.controller import Controller
from agent_run.git import GitRepository
from agent_run.github_fixture import FixtureGitHubReader
from agent_run.run_locator import RunLocatorIndex
from agent_run.state import StateStore
from agent_run.user_defaults import UserDefaultsStore
from support.run_scenarios import _accepted_run
from support.run_publication import InvocationRunPublicationAgents,HumanThenRunPublicationAgents
import agent_run.run_publication
request=json.load(sys.stdin)
frozen,root=Path(request['frozen']),Path(request['repository'])
assert Path(agent_run.run_publication.__file__).resolve().is_relative_to(frozen/'project/src')
UserDefaultsStore().configure(language=request['language'])
ids=[]
for index in range(2):
 if index:
  # The helper defaults to reusing the latest Run, including a completed Run
  # whose branch was correctly retired. Create a new identity via the old
  # production entry rather than rewriting the first Run's facts or refs.
  Controller(FixtureGitHubReader(root/'github.json'),GitRepository(root),
      StateStore(root/'.agent-run')).start(1,reuse_existing=False)
 state,states,git,publisher=_accepted_run(root)
 engine=RunPublicationEngine(git=git,states=states,agents=InvocationRunPublicationAgents() if index==0 else HumanThenRunPublicationAgents(),
     github=publisher,default_branch='main',default_head_sha=git.resolve('main'))
 if index==0:
  assert engine.publish(state['run_id'])['status']=='run_approval_pending'
  assert engine.approve(state['run_id'])['status']=='completed'
 else:
  assert engine.publish(state['run_id'])['status']=='ready_for_human'
 ids.append(state['run_id'])
 RunLocatorIndex.default().register(run_id=state['run_id'],repository_root=root,
     state_dir=states.root,repository='example/project',parent_number=1)
assert len(set(ids))==2
print(json.dumps({'runs':ids,'database':str(states.root/'state.sqlite3')}))
'''


def _bytes(root: Path) -> dict[str, bytes]:
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob('*')
            if path.is_file() and not path.name.endswith('-shm')
            and not (path.name.endswith('-wal') and path.stat().st_size == 0)}


def _original_source(tmp_path: Path, git_repo: Path, language: str = 'en') -> dict:
    frozen = tmp_path / 'old02'
    old_v6_runtime(frozen)
    observed = subprocess.run([sys.executable, '-c', _TERMINAL],
        input=json.dumps({'frozen': str(frozen), 'repository': str(git_repo), 'language': language}),
        env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONPATH': os.pathsep.join(
            str(frozen / part) for part in ('project/src', 'project/tests'))},
        text=True, capture_output=True, timeout=45)
    assert observed.returncode == 0, observed.stdout + observed.stderr
    return json.loads(observed.stdout)


@pytest.mark.parametrize('language', ['en', 'zh'])
def test_original_terminal_selectors_history_raw_and_live_wal_are_read_only(
    tmp_path: Path, git_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    language: str,
) -> None:
    source = _original_source(tmp_path, git_repo, language)
    database = Path(source['database'])
    before = _bytes(tmp_path)
    assert main(['runs', '--repo', 'example/project', '--json']) == 0
    candidates = json.loads(capsys.readouterr().out)['runs']
    assert {item['run_id'] for item in candidates} == set(source['runs'])
    assert next(item for item in candidates if item['run_id'] == source['runs'][0])['is_terminal']
    assert not next(item for item in candidates if item['run_id'] == source['runs'][1])['is_terminal']
    assert main(['status', '--repo', 'example/project', '--parent', '1', '--json']) == 2
    assert json.loads(capsys.readouterr().out)['diagnostics'][0]['code'] == 'run_locator_conflict'
    selected = source['runs'][0]
    assert main(['status', selected, '--json']) == 0
    status = json.loads(capsys.readouterr().out)
    assert status['run_id'] == selected and status['status'] == 'completed'
    assert status['language'] == language and status['current_liveness'] == 'unknown'
    assert status['agent_invocations'] and status['semantic_agent_attempts'] and status['raw_returns']
    assert all(raw['record']['run_id'] == selected for raw in status['raw_returns'].values())
    with closing(sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True)) as db:
        expected = [tuple(row) for row in db.execute('SELECT stream,position,event_id FROM run_timeline_slots '
                                                    'WHERE run_id=? ORDER BY stream,position', (selected,))]
        raw_values = {row[0]: json.loads(row[1]) for row in db.execute('SELECT e.evidence_id,m.body '
            'FROM work_invocations i JOIN work_evidence e ON e.run_id=i.run_id AND e.evidence_id=i.raw_result_evidence_id '
            'JOIN work_evidence_materials m ON m.material_id=e.material_id WHERE i.run_id=?', (selected,))}
    assert {key: value['value'] for key, value in status['raw_returns'].items()} == raw_values
    events, after = [], 0
    while True:
        assert main(['history', selected, '--json', '--limit', '3', '--after', str(after)]) == 0
        page = json.loads(capsys.readouterr().out)
        events.extend((item['slot']['stream'], item['slot']['position'], item['slot']['event_id']) for item in page['events'])
        if not page['history_page']['has_more']:
            break
        assert page['history_page']['next_after'] > after
        after = page['history_page']['next_after']
    assert events == expected
    assert main(['history', selected, '--plain', '--details']) == 0
    plain = capsys.readouterr().out
    assert ('Original Run records' if language == 'en' else '原始运行记录') in plain
    assert raw_values and all(json.dumps(value, ensure_ascii=False, sort_keys=True) in plain for value in raw_values.values())
    assert main(['status', source['runs'][1], '--json']) == 0
    blocked = json.loads(capsys.readouterr().out)
    blocker = 'GitHub denied access; tried gh issue view; grant Issue read access.'
    assert blocked['status'] == 'ready_for_human'
    assert blocker in json.dumps(blocked['current_findings'], ensure_ascii=False)
    assert main(['history', source['runs'][1], '--plain', '--details']) == 0
    assert blocker in capsys.readouterr().out
    with monkeypatch.context() as terminal:
        stream = io.StringIO()
        terminal.setattr(stream, 'isatty', lambda: True)
        terminal.setattr(sys, 'stdout', stream)
        terminal.delenv('NO_COLOR', raising=False)
        terminal.setenv('TERM', 'xterm')
        assert main(['history', selected, '--details']) == 0
        assert ('Original Run records' if language == 'en' else '原始运行记录') in stream.getvalue()
    assert _bytes(tmp_path) == before

    # A copied original is committed through SQLite backup into a live WAL.
    # No business row is fabricated. immutable=1 would miss these tables.
    copy = tmp_path / 'wal-copy' / 'state.sqlite3'
    copy.parent.mkdir()
    child = subprocess.Popen([sys.executable, '-c', r'''
import sqlite3,sys
source=sqlite3.connect('file:'+sys.argv[1]+'?mode=ro',uri=True)
target=sqlite3.connect(sys.argv[2])
assert target.execute('PRAGMA journal_mode=WAL').fetchone()[0]=='wal'
source.backup(target);source.close()
print('ready',flush=True);sys.stdin.read();target.close()
''', str(database), str(copy)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert child.stdout is not None and select.select([child.stdout], [], [], 10)[0]
        assert child.stdout.readline().strip() == 'ready'
        assert Path(str(copy) + '-wal').stat().st_size > 0
        before_copy = _bytes(copy.parent)
        assert main(['status', selected, '--state-dir', str(copy), '--json']) == 0
        assert json.loads(capsys.readouterr().out)['recorded_run'] == status['recorded_run']
        assert _bytes(copy.parent) == before_copy
    finally:
        if child.stdin is not None:
            child.stdin.close()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)
        if child.stdout is not None:
            child.stdout.close()
        if child.stderr is not None:
            child.stderr.close()


def test_exact_original_selection_does_not_decode_another_runs_damaged_header(
    tmp_path: Path, git_repo: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    source = _original_source(tmp_path, git_repo)
    database, selected = Path(source['database']), source['runs'][0]
    # Fault injection on synthetic data, after the unchanged old writer has
    # created both Runs. A sibling's damage belongs to that sibling's query.
    with closing(sqlite3.connect(database)) as db:
        db.execute('UPDATE run_root SET value_language=? WHERE run_id=?', ('broken', source['runs'][1]))
        db.commit()
    before = _bytes(tmp_path)
    for command in ('status', 'history'):
        assert main([command, selected, '--state-dir', str(database), '--json']) == 0
        assert json.loads(capsys.readouterr().out)['run_id'] == selected
    assert main(['status', source['runs'][1], '--state-dir', str(database), '--json']) == 2
    assert json.loads(capsys.readouterr().out)['diagnostics'][0]['code'] == 'incompatible_run_state'
    assert _bytes(tmp_path) == before


def test_unread_original_history_fails_only_when_its_page_is_requested(
    tmp_path: Path, git_repo: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    source = _original_source(tmp_path, git_repo)
    database, selected = Path(source['database']), source['runs'][0]
    with closing(sqlite3.connect(database)) as db:
        slots = db.execute('SELECT event_id FROM run_timeline_slots WHERE run_id=? ORDER BY stream,position',
                           (selected,)).fetchall()
        assert len(slots) > 1 and slots[0] != slots[-1]
        db.execute('UPDATE run_timeline_events SET value_at=? WHERE run_id=? AND event_id=?',
                   ('invalid timestamp', selected, slots[-1][0]))
        db.commit()
    before = _bytes(tmp_path)
    assert main(['status', selected, '--state-dir', str(database), '--json']) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'completed'
    assert main(['history', selected, '--state-dir', str(database), '--limit', '1', '--json']) == 0
    assert len(json.loads(capsys.readouterr().out)['events']) == 1
    assert main(['history', selected, '--state-dir', str(database), '--after', str(len(slots)-1), '--json']) == 2
    assert json.loads(capsys.readouterr().out)['diagnostics'][0]['code'] == 'incompatible_run_state'
    assert _bytes(tmp_path) == before


def test_original_copies_keep_path_and_run_identity_without_choosing_newest(
    tmp_path: Path, git_repo: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    source = _original_source(tmp_path, git_repo)
    selected = source['runs'][0]
    selection = tmp_path / 'selection'
    for name in ('first', 'second'):
        target = selection / name / 'state.sqlite3'
        target.parent.mkdir(parents=True)
        shutil.copyfile(source['database'], target)
    os.utime(selection / 'second/state.sqlite3', (1, 1))
    before = _bytes(tmp_path)
    assert main(['status', selected, '--state-dir', str(selection), '--json']) == 2
    conflict = json.loads(capsys.readouterr().out)
    assert conflict['diagnostics'][0]['code'] == 'run_locator_conflict'
    assert {item['database'] for item in conflict['candidates']} == {
        str(selection / name / 'state.sqlite3') for name in ('first', 'second')}
    assert main(['history', selected, '--state-dir', str(selection / 'second'), '--json']) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'completed'
    assert _bytes(tmp_path) == before


def test_original_history_page_does_not_decode_unread_native_history(
    tmp_path: Path, git_repo: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    from test_kernel_query_cli import _create
    source = _original_source(tmp_path, git_repo)
    selected = source['runs'][0]
    snapshot = capture_v6(Path(source['database']), tmp_path / 'captured.sqlite3')
    kernel = _create(tmp_path / 'native', run_id=selected)
    preserve_v6_records(snapshot, kernel, run_id=selected, subject='ticket:2')
    # Damage an existing native history snapshot, not the imported records or
    # current facts. It must fail exactly when its own page is requested.
    with kernel.storage.transaction() as tx:
        tx.execute("UPDATE history SET status='{' WHERE position=(SELECT min(position) FROM history)")
    count = V6RecordReader(kernel).tables()['run_timeline_slots']['count']
    assert count > 1
    before = _bytes(tmp_path)
    assert main(['history', selected, '--state-dir', str(kernel.storage.path), '--limit', '1', '--json']) == 0
    first = json.loads(capsys.readouterr().out)
    assert len(first['original']['events']) == 1 and first['timeline'] == []
    assert first['history_page'] == {'after': 0, 'next_after': 1, 'has_more': True}
    assert main(['history', selected, '--state-dir', str(kernel.storage.path), '--after', str(count), '--json']) == 2
    assert json.loads(capsys.readouterr().out)['diagnostics'][0]['code'] == 'incompatible_run_state'
    assert _bytes(tmp_path) == before


def test_public_imported_history_preserves_original_provenance_and_native_state(
    tmp_path: Path, git_repo: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    # Reuse the verified formal original producer and CLI migration, unchanged.
    from test_kernel_migration_cli import test_offline_apply_reconciles_real_old_dispatched_close_without_replay
    test_offline_apply_reconciles_real_old_dispatched_close_without_replay(tmp_path, git_repo)
    capsys.readouterr()
    managed = ManagedWorkspace.for_repository('example/project')
    inspection = json.loads((tmp_path / 'numeric-event-evidence/inspection.json').read_text())
    run_id = inspection['source']['run_id']
    database = managed.state_root / run_id / 'kernel.sqlite3'
    reader = V6RecordReader(Kernel(database))
    original = reader.row('runs', (run_id,))
    before = _bytes(tmp_path)
    assert main(['status', '--repo', 'example/project', '--parent', '1', '--json']) == 0
    status = json.loads(capsys.readouterr().out)
    assert status['original']['recorded_run'] == original
    assert status['original']['original_source']['sha256'] == reader.source['sha256']
    assert status['status'] != original['status']
    old_count = reader.tables()['run_timeline_slots']['count']
    seen_old, seen_native, after = [], [], 0
    while True:
        assert main(['history', run_id, '--state-dir', str(database), '--json', '--limit', '3', '--after', str(after)]) == 0
        page = json.loads(capsys.readouterr().out)
        seen_old.extend(item['slot'] for item in page['original']['events'])
        seen_native.extend(item['position'] for item in page['timeline'])
        if not page['history_page']['has_more']:
            break
        assert page['history_page']['next_after'] > after
        after = page['history_page']['next_after']
    assert len(seen_old) == old_count
    with Kernel(database).storage.transaction(write=False) as tx:
        assert seen_native == [row[0] for row in tx.all('SELECT position FROM history ORDER BY position')]
    assert main(['history', run_id, '--state-dir', str(database), '--plain', '--details']) == 0
    assert ('原始运行记录' if status['original']['language'] == 'zh' else 'Original Run records') in capsys.readouterr().out
    assert _bytes(tmp_path) == before
