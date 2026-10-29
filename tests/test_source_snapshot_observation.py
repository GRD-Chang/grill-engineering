"""Complete fresh Source snapshots: projection, quota, retries and cancellation."""
import json

import pytest

from agent_run.adapters import github, github_commands
from agent_run.adapters.source import read_requirements
from agent_run.errors import GitHubReadError
from support.github_requirements import source_issue
from support.remote_effects import _reader


def _flat(tmp_path, monkeypatch, count=2):
    reader, log = _reader(tmp_path, monkeypatch)
    state = tmp_path / 'responses.json'
    data = json.loads(state.read_text())
    numbers = [9, 3, 13, 17, 23][:count]
    for number in numbers:
        data[str(number)] = source_issue({'number': number, 'title': f'Ticket {number}', 'body': f'Original {number}',
            'state': 'OPEN', 'labels': {'nodes': [{'name': 'ready-for-agent'}],
                'pageInfo': {'hasNextPage': False, 'endCursor': None}},
            'blockedBy': {'nodes': [], 'pageInfo': {'hasNextPage': False, 'endCursor': None}}})
    data['parent']['subIssues']['nodes'] = [source_issue({'number': n}) for n in numbers]
    data['parent']['subIssues']['totalCount'] = count
    data['parent']['subIssues']['pageInfo'] = {'hasNextPage': False, 'endCursor': None}
    state.write_text(json.dumps(data))
    monkeypatch.setattr(github_commands.time, 'sleep', lambda _: None)
    return reader, state, log


def _calls(log):
    return [dict(value.split('=', 1) for i, value in enumerate(row) if i and row[i-1] in ('-f', '-F'))
            for row in map(json.loads, log.read_text().splitlines())]


@pytest.mark.parametrize('count', [2, 4])
def test_complete_first_page_uses_one_cli_and_preserves_native_values(tmp_path, monkeypatch, count):
    reader, state, log = _flat(tmp_path, monkeypatch, count)
    data = json.loads(state.read_text())
    result = read_requirements(reader, 1)
    assert result.parent.body == data['parent']['body']
    assert tuple(issue.number for issue in result.issues) == tuple(n['number'] for n in data['parent']['subIssues']['nodes'])
    assert [issue.body for issue in result.issues] == [f'Original {n}' for n in result.parent.sub_issue_numbers]
    assert all(issue.labels == frozenset({'ready-for-agent'}) and issue.state == 'OPEN' for issue in result.issues)
    assert len(_calls(log)) == 1
    query = _calls(log)[0]['query']
    assert 'subIssues(first:16' in query and 'labels(first:100)' in query and 'blockedBy(first:100)' in query


@pytest.mark.parametrize('parent_bytes,ticket_bytes,count', [(0, 630000, 2), (630000, 630000, 1), (0, 900000, 4)])
def test_only_explicit_aggregate_overflow_uses_compact_projection_without_truncation(
    tmp_path, monkeypatch, parent_bytes, ticket_bytes, count,
):
    reader, state, log = _flat(tmp_path, monkeypatch, count)
    data = json.loads(state.read_text())
    data['parent']['body'] = 'P' * parent_bytes
    for child in data['parent']['subIssues']['nodes']:
        data[str(child['number'])]['body'] = 'T' * ticket_bytes
    state.write_text(json.dumps(data))
    observed = read_requirements(reader, 1)
    assert observed.parent.body == 'P' * parent_bytes
    assert all(issue.body == 'T' * ticket_bytes for issue in observed.issues)
    queries = _calls(log)
    assert len(queries) == 2 + count
    assert 'labels(first:100' in queries[0]['query'] and 'labels(first:100' not in queries[1]['query']
    assert all('SourceTicket' in row['query'] for row in queries[2:])


def test_final_source_limit_is_independent_of_per_packet_limit(tmp_path, monkeypatch):
    reader, state, log = _flat(tmp_path, monkeypatch, 5)
    data = json.loads(state.read_text())
    for child in data['parent']['subIssues']['nodes']:
        data[str(child['number'])]['body'] = 'T' * 900000
    state.write_text(json.dumps(data))
    with pytest.raises(GitHubReadError) as caught:
        read_requirements(reader, 1)
    assert caught.value.code == 'github_invalid_response' and 'material limit' in str(caught.value)
    assert len(_calls(log)) == 7  # One overflow, one compact Parent, five individually legal Tickets.


@pytest.mark.parametrize('fault', ['permission', 'partial', 'null', 'null-data', 'null-repository'])
def test_incomplete_observations_never_trigger_capacity_fallback(tmp_path, monkeypatch, fault):
    reader, state, log = _flat(tmp_path, monkeypatch)
    data = json.loads(state.read_text())
    if fault == 'permission':
        data['graphql_http'] = 403
    else:
        if fault in {'partial', 'null'}:
            data['graphql_errors'] = [{'message': 'Controlled incomplete observation'}]
        if fault.startswith('null'):
            program = tmp_path / 'bin/gh'
            marker = "    print(json.dumps(result))\nelif args[:1]==['api'] and '/commits/'"
            text = program.read_text()
            assert marker in text
            assignment = "    result['data']=None\n" if fault == 'null-data' else "    result['data']['repository']=None\n"
            program.write_text(text.replace(marker, assignment + marker, 1))
    state.write_text(json.dumps(data))
    with pytest.raises(GitHubReadError) as caught:
        read_requirements(reader, 1)
    assert caught.value.code == 'github_read_failed'
    queries = _calls(log)
    assert len(queries) == 3 and all('labels(first:100' in row['query'] for row in queries)


@pytest.mark.parametrize('budget', ['attempts', 'deadline', 'cancelled'])
def test_overflow_and_compact_reads_share_original_budget(tmp_path, monkeypatch, budget):
    reader, state, log = _flat(tmp_path, monkeypatch)
    data = json.loads(state.read_text())
    for child in data['parent']['subIssues']['nodes']:
        data[str(child['number'])]['body'] = 'T' * 630000
    state.write_text(json.dumps(data))
    program = tmp_path / 'bin/gh'
    text = program.read_text()
    marker = "    if 'CurrentRequiredChecks' in fields['query']:"
    text = text.replace(marker, "    if 'SourceSnapshot' in fields['query'] and 'labels(first:100' not in fields['query']:\n        print('HTTP 403 compact unavailable',file=sys.stderr);raise SystemExit(1)\n" + marker, 1)
    program.write_text(text)
    bounded, timeouts, clock, stopped = github_commands._run_bounded_command, [], [0.], [False]
    monkeypatch.setattr(github, 'monotonic', lambda: clock[0])
    reader.cancelled = lambda: stopped[0]

    def request(arguments, **options):
        timeouts.append(options['timeout'])
        try:
            return bounded(arguments, **options)
        finally:
            if budget == 'deadline':
                clock[0] = 70. if len(timeouts) == 1 else 91.
            if budget == 'cancelled':
                stopped[0] = True

    monkeypatch.setattr(github_commands, '_run_bounded_command', request)
    expected = InterruptedError if budget == 'cancelled' else GitHubReadError
    with pytest.raises(expected):
        read_requirements(reader, 1)
    assert len(timeouts) == {'attempts': 3, 'deadline': 2, 'cancelled': 1}[budget]
    if budget == 'deadline':
        assert timeouts == [30, 20]
    assert len(_calls(log)) == len(timeouts)


def test_retry_discards_all_prior_parent_pages(tmp_path, monkeypatch):
    reader, log = _reader(tmp_path, monkeypatch)
    monkeypatch.setattr(github_commands.time, 'sleep', lambda _: None)
    state = tmp_path / 'responses.json'
    bounded, failed = github_commands._run_bounded_command, []

    def request(arguments, **options):
        if 'cursor=parent-next' in arguments and not failed:
            failed.append(True)
            data = json.loads(state.read_text())
            for key in ('parent', 'parent-next'):
                data[key]['body'] = 'Fresh complete Parent'
            for key in ('9', 'blocker-next'):
                data[key]['body'] = 'Fresh complete Ticket'
            state.write_text(json.dumps(data))
            # One failed physical response; all succeeding pages must be re-read.
            import subprocess
            return subprocess.CompletedProcess(arguments, 1, '', 'HTTP 503 controlled')
        return bounded(arguments, **options)

    monkeypatch.setattr(github_commands, '_run_bounded_command', request)
    observed = read_requirements(reader, 1)
    assert observed.parent.body == 'Fresh complete Parent'
    assert observed.issues[0].body == 'Fresh complete Ticket'
    roots = [row for row in _calls(log) if row['number'] == '1' and 'cursor' not in row]
    assert len(roots) == 2


def test_inferred_repository_is_resolved_inside_the_bounded_cli(tmp_path, monkeypatch):
    reader, _, log = _flat(tmp_path, monkeypatch)
    reader.repository_override = None
    monkeypatch.setattr(reader, 'repository_hint', lambda: pytest.fail('Unbounded local origin hint must not run'))
    observed = read_requirements(reader, 1)
    assert observed.parent.sub_issue_numbers == (9, 3)
    assert len(_calls(log)) == 1
    assert (_calls(log)[0]['owner'], _calls(log)[0]['name']) == ('{owner}', '{repo}')


@pytest.mark.parametrize('selected', ['https://foreign.invalid/owner/project', '@request.json/project'])
def test_url_or_typed_file_value_is_never_a_frozen_repository(tmp_path, monkeypatch, selected):
    reader, _, log = _flat(tmp_path, monkeypatch)
    reader.repository_override = selected
    with pytest.raises(GitHubReadError) as caught:
        read_requirements(reader, 1)
    assert caught.value.code == 'github_invalid_response'
    assert not log.exists(), 'Invalid selected identity must fail before preparing a command'


@pytest.mark.parametrize('change', ['none', 'count-drift', 'cursor-loop'])
def test_declared_totals_bound_many_legal_pages_without_a_three_request_cap(tmp_path, monkeypatch, change):
    from copy import deepcopy
    reader, state, log = _flat(tmp_path, monkeypatch, 1)
    data = json.loads(state.read_text())
    for index in range(10):
        key = '9' if index == 0 else f'labels-{index}'
        data[key] = deepcopy(data['9'])
        data[key]['labels'] = {'totalCount': 10, 'nodes': [{'id': f'label-{index}', 'name': f'L{index}'}],
            'pageInfo': {'hasNextPage': index < 9, 'endCursor': f'labels-{index+1}' if index < 9 else None}}
    if change == 'count-drift':
        data['labels-1']['labels']['totalCount'] = 11
    elif change == 'cursor-loop':
        data['labels-1']['labels']['pageInfo']['endCursor'] = 'labels-1'
    state.write_text(json.dumps(data))
    if change == 'none':
        observed = read_requirements(reader, 1)
        assert observed.issues[0].labels == frozenset(f'L{i}' for i in range(10))
        assert len(_calls(log)) == 10  # All pages are one attempt, not ten retries.
    else:
        with pytest.raises(GitHubReadError) as caught:
            read_requirements(reader, 1)
        assert caught.value.code == ('github_observation_pending' if change == 'count-drift' else 'github_invalid_response')
        assert len(_calls(log)) == 2


def test_shorter_outer_cancellation_still_stops_the_owned_source_command(tmp_path, monkeypatch):
    import time
    reader, _, _ = _flat(tmp_path, monkeypatch)
    program = tmp_path / 'bin/gh'
    first, _, rest = program.read_text().partition('\n')
    program.write_text(first + '\nimport time\ntime.sleep(5)\n' + rest)
    popen, children, started_at = github_commands.subprocess.Popen, [], []
    def started(*args, **options):
        child = popen(*args, **options)
        children.append(child)
        started_at.append(time.monotonic())
        return child
    monkeypatch.setattr(github_commands.subprocess, 'Popen', started)
    reader.cancelled = lambda: bool(started_at) and time.monotonic() >= started_at[0] + 0.25
    with pytest.raises(InterruptedError, match='cancelled'):
        read_requirements(reader, 1)
    assert time.monotonic() - started_at[0] < 2
    assert len(children) == 1 and children[0].returncode is not None
    assert children[0].stdout.closed and children[0].stderr.closed


@pytest.mark.parametrize('large_field', ['body', 'labels'])
def test_oversized_source_is_rejected_before_prefetching_all_parent_pages(tmp_path, monkeypatch, large_field):
    from copy import deepcopy
    reader, state, log = _flat(tmp_path, monkeypatch, 1)
    data = json.loads(state.read_text())
    numbers = list(range(2, 83))
    template = deepcopy(data['9'])
    for number in numbers:
        value = deepcopy(template)
        value['number'], value['id'] = number, f'issue-node-{number}'
        if large_field == 'body':
            value['body'] = 'T' * 60000
        else:
            value['labels']['nodes'] = [{'id': f'label-{number}', 'name': 'L' * 60000}]
        data[str(number)] = value
    parent = deepcopy(data['parent'])
    for offset in range(0, len(numbers), 16):
        page = deepcopy(parent)
        chunk = numbers[offset:offset+16]
        page['subIssues'] = {'totalCount': len(numbers), 'nodes': [source_issue({'number': n}) for n in chunk],
            'pageInfo': {'hasNextPage': offset+16 < len(numbers),
                         'endCursor': f'parent-{offset+16}' if offset+16 < len(numbers) else None}}
        data['parent' if offset == 0 else f'parent-{offset}'] = page
    state.write_text(json.dumps(data))
    with pytest.raises(GitHubReadError) as caught:
        read_requirements(reader, 1)
    assert caught.value.code == 'github_invalid_response' and 'material limit' in str(caught.value)
    assert len(_calls(log)) == 5, 'The material lower bound must reject before a sixth Parent packet'


def test_repeated_large_parent_wire_bytes_do_not_shrink_valid_source_range(tmp_path, monkeypatch):
    from copy import deepcopy
    reader, state, log = _flat(tmp_path, monkeypatch, 1)
    data = json.loads(state.read_text())
    numbers = list(range(2, 8))
    parent, ticket = deepcopy(data['parent']), deepcopy(data['9'])
    parent['body'] = 'P' * 900000
    for index, number in enumerate(numbers):
        data[str(number)] = {**deepcopy(ticket), 'number': number, 'id': f'issue-node-{number}'}
        page = deepcopy(parent)
        page['subIssues'] = {'totalCount': 6, 'nodes': [source_issue({'number': number})],
            'pageInfo': {'hasNextPage': index < 5, 'endCursor': f'parent-{index+1}' if index < 5 else None}}
        data['parent' if index == 0 else f'parent-{index}'] = page
    state.write_text(json.dumps(data))
    bounded, packets = github_commands._run_bounded_command, []
    def request(*args, **options):
        response = bounded(*args, **options)
        packets.append(len(response.stdout.encode()))
        return response
    monkeypatch.setattr(github_commands, '_run_bounded_command', request)
    observed = read_requirements(reader, 1)
    assert observed.parent.body == 'P' * 900000 and len(observed.issues) == 6
    assert len(packets) == 6 and max(packets) <= github_commands.MAX_COMMAND_OUTPUT_BYTES
    assert sum(packets) > 4 * 1024 * 1024


@pytest.mark.parametrize('where', ['parent', 'ticket'])
def test_escaped_non_scalar_text_is_a_complete_invalid_observation(tmp_path, monkeypatch, where):
    reader, state, log = _flat(tmp_path, monkeypatch, 1)
    data = json.loads(state.read_text())
    data['parent' if where == 'parent' else '9']['body'] = '\ud800'
    state.write_text(json.dumps(data))  # Valid UTF-8 JSON can contain this invalid scalar escape.
    with pytest.raises(GitHubReadError) as caught:
        read_requirements(reader, 1)
    assert caught.value.code == 'github_invalid_response'
    assert len(_calls(log)) == 1
