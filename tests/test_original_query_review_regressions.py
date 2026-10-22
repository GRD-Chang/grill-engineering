from contextlib import closing
import json
from pathlib import Path
import sqlite3
import pytest

from agent_run.paths import app_data_root
from agent_run.managed_workspace import ManagedWorkspace
from agent_run_kernel.query_cli import main
from agent_run_kernel.query import read
from agent_run_kernel.migration import capture_v6
from agent_run_kernel.migration_records import preserve_v6_records

from test_kernel_original_public_query import _original_source, _bytes
from test_kernel_query_cli import _create, _snapshot
from test_kernel_query_discovery import _published


def test_unread_raw_does_not_block_earlier_history(tmp_path, git_repo, capsys):
    source = _original_source(tmp_path, git_repo)
    database = Path(source['database'])
    selected = source['runs'][0]
    args = ['history', selected, '--state-dir', str(database), '--limit', '1', '--json']
    assert main(args) == 0
    first_page = json.loads(capsys.readouterr().out)
    assert len(first_page['events']) == 1
    first_event = first_page['events'][0]['facts']
    with closing(sqlite3.connect(database)) as db:
        late_raw = db.execute(
            'SELECT i.raw_result_evidence_id,e.material_id FROM work_invocations i '
            'JOIN work_evidence e ON e.run_id=i.run_id AND e.evidence_id=i.raw_result_evidence_id '
            'WHERE i.run_id=? ORDER BY i.started_at DESC LIMIT 1', (selected,),
        ).fetchone()
        assert late_raw is not None
        assert late_raw[0] not in json.dumps(first_event)
        db.execute('UPDATE work_evidence_materials SET body=? WHERE material_id=?', ('not json', late_raw[1]))
        db.commit()
    before = _bytes(tmp_path)
    code = main(args)
    output = capsys.readouterr().out
    assert _bytes(tmp_path) == before
    assert code == 0, output


def test_unrelated_bad_original_route_does_not_block_exact_native(tmp_path, capsys):
    _published(ManagedWorkspace.for_repository('example/project').state_root, run_id='native-exact')
    args = ['status', 'native-exact', '--repo', 'example/project', '--json']
    assert main(args) == 0
    capsys.readouterr()
    locator = app_data_root() / 'run-locator.json'
    locator.parent.mkdir(parents=True, exist_ok=True)
    locator.write_text(json.dumps({'entries': [{
        'run_id': 'unrelated-old-run', 'repository_root': str(tmp_path / 'old-repository'),
        'state_dir': 'damaged-relative-path', 'updated_at': '2026-10-22T00:00:00Z',
        'repository': 'example/other', 'parent_number': 2,
    }]}))
    before = _snapshot(tmp_path)
    code = main(args)
    output = capsys.readouterr().out
    assert _snapshot(tmp_path) == before
    assert code == 0, output


@pytest.mark.parametrize('source_kind', ['original', 'archive'])
def test_raw_closure_is_page_scoped_and_requested_damage_is_not_hidden(
    tmp_path, git_repo, capsys, source_kind,
):
    source = _original_source(tmp_path, git_repo)
    database, selected = Path(source['database']), source['runs'][0]
    with closing(sqlite3.connect(database)) as db:
        late = db.execute(
            'SELECT i.attempt_id,i.raw_result_evidence_id,e.material_id,i.started_at FROM work_invocations i '
            'JOIN work_evidence e ON e.run_id=i.run_id AND e.evidence_id=i.raw_result_evidence_id '
            'WHERE i.run_id=? ORDER BY i.started_at DESC LIMIT 1', (selected,),
        ).fetchone()
        slots = db.execute(
            'SELECT e.value_semantic_attempt_id,e.value_agent_invocation_started_at FROM run_timeline_slots s JOIN run_timeline_events e '
            'ON e.run_id=s.run_id AND e.event_id=s.event_id WHERE s.run_id=? ORDER BY s.stream,s.position',
            (selected,),
        ).fetchall()
        requested = next(index for index, row in enumerate(slots) if (row[0], row[1]) == (late[0], late[3]))
        assert requested > 0
        db.execute('UPDATE work_evidence_materials SET body=? WHERE material_id=?', ('not json', late[2]))
        db.commit()
    if source_kind == 'archive':
        snapshot = capture_v6(database, tmp_path / 'captured.sqlite3')
        kernel = _create(tmp_path / 'native', run_id=selected)
        preserve_v6_records(snapshot, kernel, run_id=selected, subject='ticket:2')
        database = kernel.storage.path
    before = _bytes(tmp_path)
    args = ['history', selected, '--state-dir', str(database), '--limit', '1', '--json']
    assert main(args) == 0
    first = json.loads(capsys.readouterr().out)
    record = first if source_kind == 'original' else first['original']
    assert record['raw_returns'] == {} and len(record['events']) == 1
    assert main([*args, '--after', str(requested)]) == 2
    assert json.loads(capsys.readouterr().out)['diagnostics'][0]['code'] == 'incompatible_run_state'
    assert _bytes(tmp_path) == before


@pytest.mark.parametrize('scope', ['other-run', 'other-repository', 'other-parent', 'matching', 'unknown',
                                 'empty-run-other-repository', 'empty-run-other-parent'])
def test_locator_scope_precedes_payload_validation_but_matching_errors_stay_strict(tmp_path, capsys, scope):
    _published(ManagedWorkspace.for_repository('example/project').state_root, run_id='native-exact')
    route = {'run_id': 'native-exact', 'repository_root': str(tmp_path / 'old'),
             'state_dir': 'invalid-relative-path', 'updated_at': '2026-10-22T00:00:00Z',
             'repository': 'example/project', 'parent_number': 1}
    selector = ['native-exact']
    if scope == 'other-run':
        route['run_id'] = 'another-run'
    elif scope in {'other-repository', 'empty-run-other-repository'}:
        route['repository'] = 'example/other'
    elif scope in {'other-parent', 'empty-run-other-parent'}:
        selector = ['--parent', '1']
        route['parent_number'] = 2
    elif scope == 'unknown':
        route.pop('repository')
        route.pop('parent_number')
        route['run_id'] = None
    if scope.startswith('empty-run-'):
        route['run_id'] = ''
    locator = app_data_root() / 'run-locator.json'
    locator.write_text(json.dumps({'entries': [route]}))
    before = _snapshot(tmp_path)
    code = main(['status', *selector, '--repo', 'example/project', '--json'])
    output = json.loads(capsys.readouterr().out)
    if scope in {'matching', 'unknown'}:
        assert code == 2 and output['diagnostics'][0]['code'] == 'run_locator_invalid'
    else:
        assert code == 0 and output['run_id'] == 'native-exact'
    assert _snapshot(tmp_path) == before


@pytest.mark.parametrize('source_kind', ['original', 'archive'])
def test_requested_invocation_cannot_borrow_another_attempts_valid_raw(tmp_path, git_repo, capsys, source_kind):
    source = _original_source(tmp_path, git_repo)
    database, selected = Path(source['database']), source['runs'][0]
    with closing(sqlite3.connect(database)) as db:
        target = db.execute('SELECT work_subject,started_at,attempt_id FROM work_invocations WHERE run_id=?',
                            (selected,)).fetchone()
        foreign = db.execute('SELECT raw_result_evidence_id FROM work_invocations WHERE run_id=?',
                             (source['runs'][1],)).fetchone()[0]
        # Both evidence bodies and their digests remain valid. Copying an
        # existing foreign relation into this Run must not grant provenance.
        evidence = db.execute('SELECT * FROM work_evidence WHERE run_id=? AND evidence_id=?',
                              (source['runs'][1], foreign)).fetchone()
        db.execute('INSERT INTO work_evidence VALUES (' + ','.join('?' for _ in evidence) + ')',
                   (selected, *evidence[1:]))
        db.execute('UPDATE work_invocations SET raw_result_evidence_id=? WHERE run_id=? AND work_subject=? AND started_at=?',
                   (foreign, selected, target[0], target[1]))
        slots = db.execute('SELECT e.value_agent_invocation_started_at FROM run_timeline_slots s '
            'JOIN run_timeline_events e ON e.run_id=s.run_id AND e.event_id=s.event_id '
            'WHERE s.run_id=? ORDER BY s.stream,s.position', (selected,)).fetchall()
        requested = next(index for index, row in enumerate(slots) if row[0] == target[1])
        db.commit()
    if source_kind == 'archive':
        snapshot = capture_v6(database, tmp_path / 'captured.sqlite3')
        kernel = _create(tmp_path / 'native', run_id=selected)
        preserve_v6_records(snapshot, kernel, run_id=selected, subject='ticket:2')
        database = kernel.storage.path
    before = _bytes(tmp_path)
    args = ['history', selected, '--state-dir', str(database), '--limit', '1', '--json']
    assert main(args) == 0
    assert 'events' in json.loads(capsys.readouterr().out)
    assert main([*args, '--after', str(requested)]) == 2
    diagnostic = json.loads(capsys.readouterr().out)['diagnostics'][0]
    assert diagnostic['code'] == 'incompatible_run_state'
    assert diagnostic['message'] == 'source_evidence_owner'
    assert _bytes(tmp_path) == before


@pytest.mark.parametrize('source_kind', ['original', 'archive'])
def test_requested_event_digest_is_verified_after_page_selection(tmp_path, git_repo, capsys, source_kind):
    source = _original_source(tmp_path, git_repo)
    database, selected = Path(source['database']), source['runs'][0]
    with closing(sqlite3.connect(database)) as db:
        slots = db.execute('SELECT event_id FROM run_timeline_slots WHERE run_id=? ORDER BY stream,position',
                           (selected,)).fetchall()
        db.execute('UPDATE run_timeline_events SET value_worker=? WHERE run_id=? AND event_id=?',
                   ('a changed, otherwise valid string', selected, slots[-1][0]))
        db.commit()
    if source_kind == 'archive':
        snapshot = capture_v6(database, tmp_path / 'captured.sqlite3')
        kernel = _create(tmp_path / 'native', run_id=selected)
        preserve_v6_records(snapshot, kernel, run_id=selected, subject='ticket:2')
        database = kernel.storage.path
    before = _bytes(tmp_path)
    args = ['history', selected, '--state-dir', str(database), '--limit', '1', '--json']
    assert main(args) == 0
    capsys.readouterr()
    assert main([*args, '--after', str(len(slots)-1)]) == 2
    assert json.loads(capsys.readouterr().out)['diagnostics'][0]['code'] == 'incompatible_run_state'
    assert _bytes(tmp_path) == before


def test_native_page_control_records_keep_the_recorded_order(tmp_path):
    kernel = _create(tmp_path / 'native')
    actions = ['z-stop', 'z-resume', 'a-stop', 'a-resume']
    for kind, identity in zip(['stop', 'resume', 'stop', 'resume'], actions):
        kernel.control(kind, identity, subject='ticket:2')
    before = _bytes(tmp_path)
    assert [row['id'] for row in read(kernel.storage.path, 'history').data['actions']] == actions
    assert _bytes(tmp_path) == before
