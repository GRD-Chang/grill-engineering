"""Canonical comment requests for the existing locked fake-gh service.

The caller owns the service lock, persistence and lost acknowledgements. The
optional operation_clock is this fixture's committed-write clock, not a claim
that GitHub Issue.updated_at always equals its latest comment.created_at.
"""
from datetime import datetime, timedelta, timezone
import sys


def service_time(document):
    if 'operation_clock' not in document:
        return '2026-10-21T10:00:00Z'
    document['operation_clock'] += 1
    value = datetime(2026, 10, 21, 10, 0, tzinfo=timezone.utc) + timedelta(seconds=document['operation_clock'])
    return value.isoformat().replace('+00:00', 'Z')


def _target(document, number):
    graph = document.get('source_graph', {})
    resource = (graph.get('parent') if number == graph.get('parent', {}).get('number')
                else graph.get('issues', {}).get(str(number)))
    pull = next((row for row in document['prs'] if row['number'] == number), None)
    resource = resource if resource is not None else pull
    if resource is None and (number != 2 or 'operation_clock' in document):
        raise SystemExit('unknown comment target')
    return resource, pull is not None


def _mirror(document, issue_prefix):
    # Rebuild, including empty old buckets: a stale mirror cannot resurrect a row.
    mirror = {number: [] for number in document.get('comments_by_issue', {})}
    for row in document['comments']:
        url = row.get('issue_url', issue_prefix + '2')
        number = url.rsplit('/', 1)[-1]
        if url == issue_prefix + number:
            mirror.setdefault(number, []).append(row)
    document['comments_by_issue'] = mirror


def status_comment_pages(document, fields, pull, *, repository, actor):
    """Project the existing PR/comment objects into complete GraphQL pages."""
    def author(value):
        if not isinstance(value, dict):
            return value
        login = value.get('login')
        bot = isinstance(login, str) and login.endswith('[bot]')
        return {'__typename': 'Bot' if bot else 'User', 'login': login[:-5] if bot else login}

    number = int(fields['number'])
    if pull is None:
        pages = [{'data': {'repository': {'nameWithOwner': repository, 'pullRequest': None}}}]
    else:
        rows, _ = comment_request(document, f'repos/{repository}/issues/{number}/comments',
                                  'GET', {}, repository=repository, actor=actor, slurp=False)
        binding = {key: pull[key] for key in ('number', 'author', 'headRefName', 'headRefOid', 'baseRefName',
                                             'headRepository', 'baseRepository')}
        binding['author'] = author(binding['author'])
        nodes = [{'fullDatabaseId': str(row['id']) if row.get('id') is not None else None,
                  'body': row.get('body'),
                  'author': author(row.get('user')), 'repository': {'nameWithOwner': repository},
                  'pullRequest': {'number': number, 'repository': {'nameWithOwner': repository}}}
                 for row in rows]
        pages = []
        for offset in range(0, max(1, len(nodes)), 100):
            selected = nodes[offset:offset + 100]
            end = offset + len(selected)
            comments = {'nodes': selected, 'totalCount': len(nodes), 'pageInfo': {
                'hasNextPage': end < len(nodes), 'endCursor': str(end) if selected else None}}
            pages.append({'data': {'repository': {'nameWithOwner': repository,
                'pullRequest': {**binding, 'comments': comments}}}})
    if 'viewer' in fields['query']:
        for page in pages:
            page['data']['viewer'] = {'login': actor}
    return pages


def comment_request(document, endpoint, method, body, *, repository, actor, slurp):
    """Mutate canonical rows, target version and mirror once under the caller's lock."""
    issue_prefix = 'https://api.github.com/repos/' + repository + '/issues/'
    tail = endpoint.split('/issues/', 1)[1]
    status = False
    if tail.startswith('comments/'):
        identifier = int(tail.rsplit('/', 1)[1])
        matches = [row for row in document['comments'] if row['id'] == identifier]
        if len(matches) != 1:
            raise SystemExit('unknown comment object')
        result = matches[0]
        url = result.get('issue_url', issue_prefix + '2')
        if not url.startswith(issue_prefix):
            raise SystemExit('foreign comment object')
        resource, _ = _target(document, int(url[len(issue_prefix):]))
        status = result['body'].startswith('<!-- agent-run-status:')
        if method == 'DELETE':
            document['comment_sequence'] = max(document.get('comment_sequence', 0),
                max((row['id'] for row in document['comments']), default=0))
            document['comments'].remove(result)
            result = None
        elif method == 'PATCH' and status and set(body) == {'body'}:
            result['body'] = body['body']
        else:
            raise SystemExit('unexpected Status comment object')
        stamp = service_time(document)
        if method == 'PATCH':
            document.setdefault('status_writes', []).append({'method': 'PATCH', 'id': identifier,
                                                             'body': body['body'], 'updated_at': stamp})
    else:
        if document.get('issue_read_error') and method == 'GET':
            print('HTTP 403 Issue unavailable', file=sys.stderr)
            raise SystemExit(1)
        number = int(tail.split('/', 1)[0])
        resource, is_pull = _target(document, number)
        url = issue_prefix + str(number)
        if method == 'GET':
            rows = [row for row in document['comments'] if row.get('issue_url', issue_prefix + '2') == url]
            _mirror(document, issue_prefix)
            return ([rows] if slurp else rows), False
        if method != 'POST':
            raise SystemExit('unexpected comment method')
        status = body.get('body', '').startswith('<!-- agent-run-status:')
        if status and not is_pull:
            raise SystemExit('unknown Status target PR')
        stamp = service_time(document)
        # A deleted object's receipt ID must never identify a subsequent POST.
        identifier = max(document.get('comment_sequence', 0), max((row['id'] for row in document['comments']), default=0)) + 1
        document['comment_sequence'] = identifier
        result = {'id': identifier,
                  'body': body['body'], 'user': {'login': actor}, 'issue_url': url, 'created_at': stamp}
        document['comments'].append(result)
        if status:
            document.setdefault('status_writes', []).append({'method': 'POST', 'id': result['id'],
                                                             'body': result['body'], 'updated_at': stamp})
    if 'operation_clock' in document:
        resource['updated_at'] = stamp
    _mirror(document, issue_prefix)
    return result, status
