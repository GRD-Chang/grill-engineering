"""Source classification uses raw JSON; transport and commit stories retain real gh."""
import json
import subprocess
from copy import deepcopy

import pytest

from agent_run.adapters import github_commands
from agent_run.adapters.github import GhGitHubReader
from agent_run.adapters.source import read_requirements
from agent_run.errors import GitHubReadError
from support.github_requirements import source_response, source_snapshot
from support.remote_effects import _reader, _reader_data


@pytest.fixture
def raw_source(monkeypatch):
    """Only replace command output; the production reader parses and validates it."""
    data, calls = _reader_data(), []

    def response(arguments, **kwargs):
        assert arguments[:3] == ['gh', 'api', 'graphql']
        calls.append(arguments[1:])
        fields = dict(value.split('=', 1) for index, value in enumerate(arguments)
                      if index and arguments[index - 1] in ('-F', '-f'))
        key = fields.get('cursor') or fields.get('labelsCursor') or (
            'parent' if fields['number'] == '1' else fields['number'])
        issue = deepcopy(data[key])
        if fields['number'] == '1':
            if 'labels(first:100' in fields['query']:
                issue = source_snapshot(issue, data)
        elif issue is not None:
            for field, cursor in (('labels', 'labelsCursor'), ('blockedBy', 'cursor')):
                issue[field] = data[fields.get(cursor) or key][field]
        packet = source_response(issue, data['repository'])
        if 'graphql_errors' in data:
            packet['errors'] = data['graphql_errors']
        return subprocess.CompletedProcess(arguments, 0, json.dumps(packet), '')

    monkeypatch.setattr(github_commands, '_run_bounded_command', response)
    return GhGitHubReader('example/project', runner_identity={'source': 'host'}), data, calls


@pytest.mark.parametrize('errors', [[{'message': 'Resource not accessible', 'path': ['repository', 'issue']}], None, {}, False])
def test_graphql_partial_data_and_errors_is_not_a_requirement_snapshot(raw_source, monkeypatch, errors):
    reader, data, calls = raw_source
    delays = []
    monkeypatch.setattr(github_commands.time, 'sleep', delays.append)
    data['graphql_errors'] = errors
    with pytest.raises(GitHubReadError):
        read_requirements(reader, 1)
    if errors:
        assert len(calls) == 3
        assert len(delays) == 2 and 1 <= delays[0] <= 1.25 and 2 <= delays[1] <= 2.25
    else:
        assert len(calls) == 1 and delays == []


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
    assert all(command[:2] == ['api', 'graphql'] and '--method' not in command for command in commands)
    queries = [dict(value.split('=', 1) for i, value in enumerate(command) if i and command[i - 1] in ('-F', '-f'))
               for command in commands if command[:2] == ['api', 'graphql']]
    assert all(fields['owner'] == 'example' and fields['name'] == 'project' for fields in queries)
    ticket_pages = [fields for fields in queries if fields['number'] == '9']
    assert len(ticket_pages) == 2  # The first Ticket page arrived in its Parent page.
    assert ticket_pages[0]['cursor'] == 'blocker-next' and ticket_pages[0]['labelsCursor'] == 'labels-next'
    assert 'cursor' not in ticket_pages[1] and ticket_pages[1]['labelsCursor'] == 'labels-last'
    assert len(commands) == 4  # Two nested Parent pages and two Ticket continuations.
    for key in ('9', 'blocker-next', 'labels-next', 'labels-last'):
        data[key]['body'] = 'A fresh current requirement'
    path.write_text(json.dumps(data))
    second = read_requirements(reader, 1)
    assert second.issues[0].body == 'A fresh current requirement'
    assert first.issues[0].body == 'Same original ticket body'
    assert len(log.read_text().splitlines()) == 8


def _mutate(data, case):
    parent, next_parent, ticket, next_ticket = (data[key] for key in ('parent', 'parent-next', '9', 'blocker-next'))
    child = parent['subIssues']['nodes'][0]
    if case == 'parent_number': parent['number'] = 2
    elif case == 'ticket_number': child['number'] = ticket['number'] = 3
    elif case == 'boolean_number': child['number'] = ticket['number'] = True
    elif case == 'ticket_node': next_ticket['id'] = 'another-node'  # Changed continuation after the nested first page.
    elif case == 'ticket_repository': child['repository']['nameWithOwner'] = ticket['repository']['nameWithOwner'] = 'outside/project'
    elif case == 'case_drift': child['repository']['nameWithOwner'] = ticket['repository']['nameWithOwner'] = 'EXAMPLE/PROJECT'
    elif case == 'child_repository': child['repository']['nameWithOwner'] = 'outside/project'
    elif case == 'child_parent': child['parent']['id'] = 'another-parent'
    elif case == 'ticket_parent': child['parent']['number'] = ticket['parent']['number'] = 4
    elif case == 'null_ticket_parent': child['parent'] = ticket['parent'] = None
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
def test_incomplete_or_drifting_source_is_rejected_as_one_observation(raw_source, case):
    reader, data, commands = raw_source
    _mutate(data, case)
    with pytest.raises(GitHubReadError):
        read_requirements(reader, 1)
    if case == 'child_repository':
        assert not any('number=9' in command for command in commands)  # Never read the local same-number Issue.


def test_selected_parent_may_itself_have_a_parent(raw_source):
    reader, data, _ = raw_source
    for key in ('parent', 'parent-next'):
        data[key]['parent'] = {'id': 'upstream-parent', 'number': 77}
    assert read_requirements(reader, 1).parent.sub_issue_numbers == (9, 3)


def test_canonical_repository_casing_and_label_text_are_preserved(raw_source):
    reader, data, _ = raw_source
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
    result = read_requirements(reader, 1)
    assert result.issues[1].labels == frozenset({'ready-for-agent', 'user-label'})
    assert result.parent.body == 'Original Parent body\r\n保留原文'


def test_completed_connection_recheck_compares_facts_not_opaque_cursor_bytes(raw_source):
    reader, data, _ = raw_source
    data['blocker-next']['labels']['pageInfo']['endCursor'] = 'another-valid-last-label-cursor'
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
    assert len(commands) == 3  # Three complete Source attempts; no metadata pre-read.


@pytest.mark.parametrize('page', ['9', 'blocker-next'])
def test_blocker_matching_a_local_ticket_number_must_match_its_global_id(raw_source, page):
    reader, data, _ = raw_source
    data[page]['blockedBy']['nodes'][0].update(number=3, id='outside-repository-issue-3', state='OPEN')
    if page == '9':
        data['blocker-next']['blockedBy']['nodes'][0].update(number=99, id='external-issue-99', state='OPEN')
    with pytest.raises(GitHubReadError, match='different global Issue'):
        read_requirements(reader, 1)


def test_internal_blocker_is_preserved_without_external_members(raw_source):
    reader, data, _ = raw_source
    data['9']['blockedBy'] = {
        'totalCount': 1, 'nodes': [{'id': 'issue-node-3', 'number': 3, 'state': 'OPEN'}],
        'pageInfo': {'hasNextPage': False, 'endCursor': None}}
    data['3']['state'] = 'OPEN'
    source = read_requirements(reader, 1)
    assert source.parent.sub_issue_numbers == (9, 3)
    assert [(b.number, b.state) for b in source.issues[0].blocked_by] == [(3, 'OPEN')]


def test_native_completion_waits_for_external_blocker_then_fresh_close_releases_successor(tmp_path, monkeypatch):
    from support.offline import GraphScenario

    reader, log = _reader(tmp_path, monkeypatch)
    path = tmp_path / 'responses.json'
    data = json.loads(path.read_text())
    blockers = [{'id': 'issue-node-3', 'number': 3, 'state': 'OPEN'},
                {'id': 'outside-repository-issue-99', 'number': 99, 'state': 'OPEN'}]
    data['9']['blockedBy'] = {'totalCount': len(blockers), 'nodes': blockers,
                             'pageInfo': {'hasNextPage': False, 'endCursor': None}}
    data['3']['state'] = 'OPEN'
    path.write_text(json.dumps(data))
    source = read_requirements(reader, 1)
    assert len(log.read_text().splitlines()) == 2  # Parent pages contain complete first Ticket pages.
    assert [(b.number, b.state) for b in source.issues[0].blocked_by] == [(3, 'OPEN'), (99, 'OPEN')]
    scenario = GraphScenario(tmp_path / 'normal-producer', source)
    assert scenario.kernel.source_status()['frontier'] == ['ticket:3']
    from support.run_scoped_kernel import prepare_domain_history
    with prepare_domain_history(scenario):
        assert scenario.run().kind == 'waiting'
    current = scenario.kernel.source_status()
    assert {c['subject'] for c in current['completions']} == {'ticket:3'}
    original_completion = current['completions']
    assert current['remaining']['ticket:9'] == 'blocked_by_open_issues'
    assert scenario.kernel.status('ticket:9')['phase'] == 'workspace'
    assert scenario.kernel.status('ticket:9')['pending_attempt'] is None
    with scenario.kernel.storage.transaction(write=False) as tx:
        assert not tx.rows("SELECT id FROM attempts WHERE subject='ticket:9'")

    blockers[1]['state'] = 'CLOSED'
    path.write_text(json.dumps(data))
    scenario.requirements = read_requirements(reader, 1)
    assert len(log.read_text().splitlines()) == 4
    assert [(b.number, b.state) for b in scenario.requirements.issues[0].blocked_by] == [(3, 'OPEN'), (99, 'CLOSED')]
    refreshed = scenario.refresh()
    assert refreshed['completions'] == original_completion
    assert refreshed['frontier'] == ['ticket:9']
    # The first actual allocation proves the refreshed graph released Ticket 9.
    # Its remaining publication, merge and close exercise the same delivery as Ticket 3.
    from support.core_operations import CoreOperations
    operations = CoreOperations(scenario.kernel, scenario.execution, scenario.make_ports, prepare_agents=False)
    step = operations.until(lambda step: step.preparation is not None)
    assert step.preparation.subject == "ticket:9" and step.preparation.role == "development"



def test_external_blocker_identity_drift_during_label_pagination_is_rejected(raw_source):
    reader, data, _ = raw_source
    _long_labels(data)
    data['labels-last']['blockedBy']['nodes'][0]['id'] = 'different-global-issue-id'
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
    assert len(log.read_text().splitlines()) == 1  # Complete single-page Source, including blocker identity.
