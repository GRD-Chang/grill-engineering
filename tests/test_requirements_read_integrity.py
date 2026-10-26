"""Fail closed on partial source evidence, through the real synthetic gh process."""
import json
from pathlib import Path
from copy import deepcopy

import pytest

from agent_run.adapters.source import read_requirements
from agent_run.errors import GitHubReadError
from test_kernel_source_reading import _reader


@pytest.mark.parametrize('errors', [[{'message': 'Resource not accessible', 'path': ['repository', 'issue']}], None, {}, False])
def test_graphql_partial_data_and_errors_is_not_a_requirement_snapshot(tmp_path, monkeypatch, errors):
    reader, _ = _reader(tmp_path, monkeypatch)
    path = tmp_path / 'responses.json'
    data = json.loads(path.read_text())
    data['graphql_errors'] = errors
    path.write_text(json.dumps(data))
    with pytest.raises(GitHubReadError):
        read_requirements(reader, 1)


def test_partial_source_cannot_be_committed_by_the_normal_kernel_refresh(tmp_path, monkeypatch):
    from support.requirements_scenarios import source_run

    reader, _ = _reader(tmp_path, monkeypatch)
    source = read_requirements(reader, 1)
    kernel, _ = source_run(tmp_path / 'normal-run.sqlite3', source)
    before = kernel.source_status()
    path = tmp_path / 'responses.json'
    data = json.loads(path.read_text())
    data['parent']['body'] = 'Partial source must never become accepted work'
    data['graphql_errors'] = [{'message': 'Incomplete source'}]
    path.write_text(json.dumps(data))
    with pytest.raises(GitHubReadError):
        kernel.refresh(lambda: read_requirements(reader, 1))
    assert kernel.source_status() == before


def _long_labels(data):
    labels = [{'id': f'label-{n}', 'name': f'Label {n}'} for n in range(201)]
    labels[0]['name'] = 'ready-for-agent'
    labels[1]['name'] = 'Custom Mixed Case'
    for key, nodes, cursor, more in (
            ('9', labels[:100], 'labels-next', True),
            ('labels-next', labels[100:200], 'labels-last', True),
            ('labels-last', labels[200:], None, False)):
        if key != '9':
            data[key] = deepcopy(data['9'])
        data[key]['labels'] = {'totalCount': 201, 'nodes': nodes,
                              'pageInfo': {'hasNextPage': more, 'endCursor': cursor}}
    data['blocker-next']['labels'] = deepcopy(data['9']['labels'])
    return labels


def test_labels_and_blockers_have_independent_complete_cursors_and_each_read_is_fresh(tmp_path, monkeypatch):
    reader, log = _reader(tmp_path, monkeypatch)
    path = tmp_path / 'responses.json'
    data = json.loads(path.read_text())
    labels = _long_labels(data)
    path.write_text(json.dumps(data))
    first = read_requirements(reader, 1)
    assert first.parent.sub_issue_numbers == (9, 3)
    assert first.issues[0].labels == frozenset(label['name'] for label in labels)
    assert [(b.number, b.state) for b in first.issues[0].blocked_by] == [(99, 'OPEN'), (3, 'CLOSED')]
    commands = [json.loads(line) for line in log.read_text().splitlines()]
    queries = [dict(value.split('=', 1) for i, value in enumerate(command) if i and command[i - 1] in ('-F', '-f'))
               for command in commands if command[:2] == ['api', 'graphql']]
    ticket_pages = [fields for fields in queries if fields['number'] == '9']
    assert len(ticket_pages) == 3
    assert ticket_pages[1]['cursor'] == 'blocker-next' and ticket_pages[1]['labelsCursor'] == 'labels-next'
    assert 'cursor' not in ticket_pages[2] and ticket_pages[2]['labelsCursor'] == 'labels-last'
    assert len(commands) == 7  # One metadata read, two Parent pages, three Ticket9 pages, one Ticket3.
    for key in ('9', 'blocker-next', 'labels-next', 'labels-last'):
        data[key]['body'] = 'A fresh current requirement'
    path.write_text(json.dumps(data))
    second = read_requirements(reader, 1)
    assert second.issues[0].body == 'A fresh current requirement'
    assert first.issues[0].body == 'Same original ticket body'
    assert len(log.read_text().splitlines()) == 14


def _mutate(data, case):
    parent, next_parent, ticket, next_ticket = (data[key] for key in ('parent', 'parent-next', '9', 'blocker-next'))
    child = parent['subIssues']['nodes'][0]
    if case == 'parent_number': parent['number'] = 2
    elif case == 'ticket_number': ticket['number'] = 3
    elif case == 'boolean_number': ticket['number'] = True
    elif case == 'ticket_node': ticket['id'] = 'another-node'
    elif case == 'ticket_repository': ticket['repository']['nameWithOwner'] = 'outside/project'
    elif case == 'case_drift': ticket['repository']['nameWithOwner'] = 'EXAMPLE/PROJECT'
    elif case == 'child_repository': child['repository']['nameWithOwner'] = 'outside/project'
    elif case == 'child_parent': child['parent']['id'] = 'another-parent'
    elif case == 'ticket_parent': ticket['parent']['number'] = 4
    elif case == 'null_ticket_parent': ticket['parent'] = None
    elif case == 'null_child': parent['subIssues']['nodes'][0] = None
    elif case == 'missing_issue': data['parent'] = None
    elif case == 'missing_labels': del ticket['labels']
    elif case == 'labels_pageinfo': del ticket['labels']['pageInfo']
    elif case == 'labels_nodes': ticket['labels']['nodes'] = None
    elif case == 'labels_total': del ticket['labels']['totalCount']
    elif case == 'blocker_pageinfo': del ticket['blockedBy']['pageInfo']
    elif case == 'parent_pageinfo': del parent['subIssues']['pageInfo']
    elif case == 'parent_cursor': del parent['subIssues']['pageInfo']['endCursor']
    elif case == 'terminal_cursor': del ticket['labels']['pageInfo']['endCursor']
    elif case == 'nonboolean_page': ticket['blockedBy']['pageInfo']['hasNextPage'] = 'false'
    elif case == 'count_change': next_ticket['blockedBy']['totalCount'] = 3
    elif case == 'parent_drift': next_parent['body'] = 'Changed Parent on a later page'
    elif case == 'ticket_drift': next_ticket['title'] = 'Changed Ticket on a later page'
    elif case == 'state_drift': next_ticket['state'] = 'CLOSED'
    elif case == 'repository_drift': next_parent['repository']['id'] = 'a-different-repository'
    elif case == 'duplicate_child': next_parent['subIssues']['nodes'] = deepcopy(parent['subIssues']['nodes'])
    elif case == 'duplicate_blocker': next_ticket['blockedBy']['nodes'] = deepcopy(ticket['blockedBy']['nodes'])
    elif case == 'duplicate_label':
        ticket['labels']['nodes'] *= 2
        ticket['labels']['totalCount'] = 2
    elif case == 'duplicate_label_name':
        ticket['labels']['nodes'].append({'id': 'different-label-id', 'name': 'ready-for-agent'})
        ticket['labels']['totalCount'] = 2
    elif case == 'blocker_cursor_loop':
        ticket['blockedBy']['totalCount'] = next_ticket['blockedBy']['totalCount'] = 3
        next_ticket['blockedBy']['pageInfo'] = {'hasNextPage': True, 'endCursor': 'blocker-next'}
    elif case == 'parent_cursor_loop':
        parent['subIssues']['totalCount'] = next_parent['subIssues']['totalCount'] = 3
        next_parent['subIssues']['pageInfo'] = {'hasNextPage': True, 'endCursor': 'parent-next'}
    elif case == 'label_cursor_loop':
        _long_labels(data)
        data['labels-next']['labels']['pageInfo']['endCursor'] = 'labels-next'
    elif case == 'label_count_change':
        _long_labels(data)
        data['labels-next']['labels']['totalCount'] = 202
    elif case == 'late_label_drift':
        next_ticket['labels']['nodes'][0]['name'] = 'ready-for-human'
    elif case == 'late_blocker_drift':
        _long_labels(data)
        data['labels-last']['blockedBy']['nodes'][0]['state'] = 'CLOSED'
    elif case == 'incomplete_terminal': next_ticket['blockedBy']['totalCount'] = ticket['blockedBy']['totalCount'] = 3
    elif case == 'invalid_state': ticket['state'] = 'UNKNOWN'
    else: raise AssertionError(case)


@pytest.mark.parametrize('case', [
    'parent_number', 'ticket_number', 'boolean_number', 'ticket_node', 'ticket_repository', 'case_drift',
    'child_repository', 'child_parent', 'ticket_parent', 'null_ticket_parent', 'null_child', 'missing_issue',
    'missing_labels', 'labels_pageinfo', 'labels_nodes', 'labels_total', 'blocker_pageinfo', 'parent_pageinfo',
    'parent_cursor', 'terminal_cursor', 'nonboolean_page', 'count_change', 'parent_drift', 'ticket_drift',
    'state_drift', 'repository_drift', 'duplicate_child', 'duplicate_blocker', 'duplicate_label',
    'duplicate_label_name', 'blocker_cursor_loop', 'parent_cursor_loop', 'label_cursor_loop',
    'label_count_change', 'late_label_drift', 'late_blocker_drift', 'incomplete_terminal', 'invalid_state',
])
def test_incomplete_or_drifting_source_is_rejected_as_one_observation(tmp_path, monkeypatch, case):
    reader, log = _reader(tmp_path, monkeypatch)
    path = tmp_path / 'responses.json'
    data = json.loads(path.read_text())
    _mutate(data, case)
    path.write_text(json.dumps(data))
    with pytest.raises(GitHubReadError):
        read_requirements(reader, 1)
    commands = [json.loads(line) for line in log.read_text().splitlines()]
    assert all(command[:2] != ['api', 'graphql'] or '--method' not in command for command in commands)
    if case == 'child_repository':
        assert not any('number=9' in command for command in commands)  # Never read the local same-number Issue.


def test_selected_parent_may_itself_have_a_parent(tmp_path, monkeypatch):
    reader, _ = _reader(tmp_path, monkeypatch)
    path = tmp_path / 'responses.json'
    data = json.loads(path.read_text())
    for key in ('parent', 'parent-next'):
        data[key]['parent'] = {'id': 'upstream-parent', 'number': 77}
    path.write_text(json.dumps(data))
    assert read_requirements(reader, 1).parent.sub_issue_numbers == (9, 3)


def test_canonical_repository_casing_and_label_text_are_preserved(tmp_path, monkeypatch):
    reader, _ = _reader(tmp_path, monkeypatch)
    path = tmp_path / 'responses.json'
    data = json.loads(path.read_text())
    data['repository'] = reader.repository_override = 'Example/Project'

    def canonical(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key == 'nameWithOwner': value[key] = 'Example/Project'
                elif key == 'id' and child == 'repository-example/project': value[key] = 'repository-Example/Project'
                else: canonical(child)
        elif isinstance(value, list):
            for child in value: canonical(child)

    canonical(data)
    path.write_text(json.dumps(data))
    result = read_requirements(reader, 1)
    assert result.issues[1].labels == frozenset({'ready-for-agent', 'user-label'})
    assert result.parent.body == 'Original Parent body\r\n保留原文'


def test_completed_connection_recheck_compares_facts_not_opaque_cursor_bytes(tmp_path, monkeypatch):
    reader, _ = _reader(tmp_path, monkeypatch)
    path = tmp_path / 'responses.json'
    data = json.loads(path.read_text())
    data['blocker-next']['labels']['pageInfo']['endCursor'] = 'another-valid-last-label-cursor'
    path.write_text(json.dumps(data))
    assert read_requirements(reader, 1).issues[0].labels == frozenset({'ready-for-agent'})


def test_source_403_is_not_empty_data_or_a_different_read_layout(tmp_path, monkeypatch):
    reader, log = _reader(tmp_path, monkeypatch)
    path = tmp_path / 'responses.json'
    data = json.loads(path.read_text())
    data['graphql_http'] = 403
    path.write_text(json.dumps(data))
    with pytest.raises(GitHubReadError) as caught:
        read_requirements(reader, 1)
    assert caught.value.code == 'github_read_failed'
    commands = [json.loads(line) for line in log.read_text().splitlines()]
    assert len(commands) == 4  # One metadata read and the existing three bounded attempts.


@pytest.mark.parametrize('page', ['9', 'blocker-next'])
def test_blocker_matching_a_local_ticket_number_must_match_its_global_id(tmp_path, monkeypatch, page):
    reader, _ = _reader(tmp_path, monkeypatch)
    path = tmp_path / 'responses.json'
    data = json.loads(path.read_text())
    data[page]['blockedBy']['nodes'][0].update(number=3, id='outside-repository-issue-3', state='OPEN')
    if page == '9':
        data['blocker-next']['blockedBy']['nodes'][0].update(number=99, id='external-issue-99', state='OPEN')
    path.write_text(json.dumps(data))
    with pytest.raises(GitHubReadError, match='different global Issue'):
        read_requirements(reader, 1)


@pytest.mark.parametrize('external_state', [None, 'OPEN', 'CLOSED'], ids=['internal-only', 'external-open', 'external-closed'])
def test_global_blocker_identity_preserves_native_completion_and_external_state(tmp_path, monkeypatch, external_state):
    from support.offline import GraphScenario

    reader, log = _reader(tmp_path, monkeypatch)
    path = tmp_path / 'responses.json'
    data = json.loads(path.read_text())
    blockers = [{'id': 'issue-node-3', 'number': 3, 'state': 'OPEN'}]
    if external_state is not None:
        blockers.append({'id': 'outside-repository-issue-99', 'number': 99, 'state': external_state})
    data['9']['blockedBy'] = {'totalCount': len(blockers), 'nodes': blockers,
                             'pageInfo': {'hasNextPage': False, 'endCursor': None}}
    data['3']['state'] = 'OPEN'
    path.write_text(json.dumps(data))
    source = read_requirements(reader, 1)
    assert len(log.read_text().splitlines()) == 5  # One metadata read, two Parent pages and two Tickets.
    assert [(b.number, b.state) for b in source.issues[0].blocked_by] == [(b['number'], b['state']) for b in blockers]
    scenario = GraphScenario(tmp_path / 'normal-producer', source)
    assert scenario.kernel.source_status()['frontier'] == ['ticket:3']
    boundary = scenario.run()
    current = scenario.kernel.source_status()
    assert any(c['subject'] == 'ticket:3' for c in current['completions'])
    if external_state == 'OPEN':
        assert boundary.kind == 'waiting'
        assert current['remaining']['ticket:9'] == 'blocked_by_open_issues'
        assert scenario.kernel.status('ticket:9')['phase'] == 'workspace'
        assert scenario.kernel.status('ticket:9')['pending_attempt'] is None
        with scenario.kernel.storage.transaction(write=False) as tx:
            assert not tx.rows("SELECT id FROM attempts WHERE subject='ticket:9'")
    else:
        assert boundary.kind == 'complete'
        assert scenario.kernel.status('ticket:9')['phase'] == 'complete'
        assert {c['subject'] for c in current['completions']} == {'ticket:3', 'ticket:9'}


def test_external_blocker_identity_drift_during_label_pagination_is_rejected(tmp_path, monkeypatch):
    reader, _ = _reader(tmp_path, monkeypatch)
    path = tmp_path / 'responses.json'
    data = json.loads(path.read_text())
    _long_labels(data)
    data['labels-last']['blockedBy']['nodes'][0]['id'] = 'different-global-issue-id'
    path.write_text(json.dumps(data))
    with pytest.raises(GitHubReadError) as caught:
        read_requirements(reader, 1)
    assert caught.value.code == 'github_observation_pending'


def test_internal_blocker_identity_does_not_add_single_page_requests(tmp_path, monkeypatch):
    reader, log = _reader(tmp_path, monkeypatch)
    path = tmp_path / 'responses.json'
    data = json.loads(path.read_text())
    children = data['parent']['subIssues']
    children['nodes'] += data['parent-next']['subIssues']['nodes']
    children['pageInfo'] = {'hasNextPage': False, 'endCursor': None}
    data['9']['blockedBy'] = {'totalCount': 1, 'nodes': [{'id': 'issue-node-3', 'number': 3, 'state': 'OPEN'}],
                             'pageInfo': {'hasNextPage': False, 'endCursor': None}}
    path.write_text(json.dumps(data))
    assert [(b.number, b.state) for b in read_requirements(reader, 1).issues[0].blocked_by] == [(3, 'OPEN')]
    assert len(log.read_text().splitlines()) == 2 + 2  # One metadata read, one Parent page, N Tickets.
