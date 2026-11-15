"""External GitHub rollup and commit-check responses for the synthetic service."""
import json
import os
import sys


def fixture_prs(data):
    return [data['pull_request']] if data.get('pull_request') else data.get('prs', [])

def current_checks(data, fields, *, stderr=None):
    if data.get('rollup_http'):
        print('HTTP ' + str(data['rollup_http']), file=stderr or sys.stderr)
        raise SystemExit(1)
    if 'rollup_pages' in data:
        page = data['rollup_pages'][fields.get('cursor', '')]
        if page.get('http'):
            print('HTTP ' + str(page['http']), file=stderr or sys.stderr)
            raise SystemExit(1)
        return page
    matches = [pr for pr in fixture_prs(data) if pr['number'] == int(fields['number'])]
    if len(matches) > 1:
        raise ValueError('Duplicate fixture PR number')
    if not matches:
        return {'data': {'repository': {'pullRequest': None}}}
    pr = matches[0]
    sha = pr['head']['sha']
    nodes = fixture_check_nodes(data, sha)
    return {'data': {'repository': {'pullRequest': {'number': pr['number'],
        'headRefOid': sha, 'commits': {'nodes': [{'commit': {'oid': sha,
        'statusCheckRollup': {'contexts': {'nodes': nodes,
            'pageInfo': {'hasNextPage': False, 'endCursor': None}}}}}]}}}}}

def fixture_check_nodes(data, sha):
    checks = data.get('checks', [])
    nodes = checks
    if isinstance(checks, list):
        nodes = []
        for index, check in enumerate(checks):
            bucket = check.get('bucket')
            conclusion = {'pass': 'SUCCESS', 'fail': 'FAILURE', 'cancel': 'CANCELLED',
                          'skipping': 'SKIPPED', 'neutral': 'NEUTRAL'}.get(bucket, check.get('state', bucket))
            nodes.append({'__typename': 'CheckRun', 'id': 'check-' + str(index),
                'databaseId': index + 1, 'name': check['name'], 'isRequired': True,
                'status': 'QUEUED' if bucket == 'pending' else 'COMPLETED', 'conclusion': conclusion,
                'detailsUrl': check.get('link'), 'title': check.get('description', ''),
                'checkSuite': {'id': 'suite-current', 'databaseId': 101, 'commit': {'oid': sha},
                    'app': {'databaseId': 1001},
                    'workflowRun': {'workflow': {'name': check.get('workflow', '')}}}})
    return nodes

def fixture_commit_nodes(data, head):
    if 'rollup_pages' in data:
        nodes = []
        for page in data['rollup_pages'].values():
            if 'data' in page and not page.get('errors'):
                nodes.extend(page['data']['repository']['pullRequest']['commits']['nodes'][0]['commit']['statusCheckRollup']['contexts']['nodes'])
        return nodes
    if any(pr['head']['sha'] == head for pr in fixture_prs(data)):
        return fixture_check_nodes(data, head)
    return []

def _latest_check_pages(data, endpoint, *, stderr=None):
    from urllib.parse import urlsplit, parse_qs
    parsed = urlsplit(endpoint)
    if not ('/commits/' in parsed.path and parsed.path.endswith(('/check-runs', '/check-suites', '/status'))):
        return None
    head = parsed.path.split('/commits/')[1].split('/')[0]
    if parsed.path.endswith('/check-suites'):
        if 'suite_pages_after' in data:
            with open(os.environ['SOURCE_TEST_LOG']) as stream:
                if sum('/check-suites?' in line for line in stream) > 1: return data['suite_pages_after']
        if 'suite_pages' in data: return data['suite_pages']
        scope = {}
        for node in fixture_commit_nodes(data, head):
            suite = node.get('checkSuite') or {}
            if suite.get('databaseId'):
                scope[suite['databaseId']] = {'id':suite['databaseId'], 'head_sha':head,
                                            'app':{'id':(suite.get('app') or {}).get('databaseId')}}
        rows = list(scope.values())
        count = data.get('suite_count', len(rows))
        if 'suite_count_after' in data:
            with open(os.environ['SOURCE_TEST_LOG']) as stream:
                reads = sum('/check-suites?' in line for line in stream)
            if reads > 1:
                count = data['suite_count_after']
                if count == 2 and len(rows) == 1:
                    rows.append({'id':99999,'head_sha':head,'app':{'id':1001}})
        if 'suite_sample' in data: rows = [data['suite_sample']]
        return {'total_count': count, 'check_suites': rows}
    if data.get('latest_http'):
        print('HTTP ' + str(data['latest_http']), file=stderr or sys.stderr)
        raise SystemExit(1)
    if parsed.path.endswith('/check-runs'):
        query = parse_qs(parsed.query)
        assert query['filter'] == ['latest'] and 'status' not in query
        if 'latest_pages' in data:
            return data['latest_pages']
        app = int(query['app_id'][0])
    nodes = data.get('latest_nodes')
    if nodes is None:
        nodes = fixture_commit_nodes(data, head)
    if parsed.path.endswith('/status'):
        if 'status_pages' in data: return data['status_pages']
        rows = [{'id': index + 1, 'node_id': n['id'], 'context': n['context'], 'state': n['state'].lower()}
                for index, n in enumerate(nodes) if n.get('__typename') == 'StatusContext']
        return [{'sha': head, 'total_count': len(rows), 'statuses': rows}]
    rows = []
    for node in nodes:
        if node.get('__typename') != 'CheckRun': continue
        suite = node.get('checkSuite') or {}
        observed_app = (suite.get('app') or {}).get('databaseId')
        if observed_app != app: continue
        rows.append({'id': node.get('databaseId'), 'name': node['name'],
                     'app': {'id': observed_app}, 'head_sha': (suite.get('commit') or {}).get('oid'),
                     'check_suite': {'id': suite.get('databaseId')}, 'status': node['status'].lower(),
                     'conclusion': str(node.get('conclusion')).lower()})
    return [{'total_count': len(rows), 'check_runs': rows}]

def latest_check_response(data, endpoint, *, slurp=False, stderr=None):
    from urllib.parse import urlsplit, parse_qs
    pages = _latest_check_pages(data, endpoint, stderr=stderr)
    if slurp: return pages
    if not isinstance(pages,list): pages = [pages]
    index = int(parse_qs(urlsplit(endpoint).query).get('page',['1'])[0]) - 1
    if index < len(pages): return pages[index]
    field = 'check_runs' if '/check-runs?' in endpoint else 'check_suites' if '/check-suites?' in endpoint else 'statuses'
    return {**pages[0], field:[]}

def print_latest_response(data, args, *, stdout=None, stderr=None):
    from urllib.parse import urlsplit, parse_qs
    endpoint = args[1]
    payload = latest_check_response(data, endpoint, slurp='--slurp' in args, stderr=stderr)
    if '--include' in args:
        pages = _latest_check_pages(data, endpoint, stderr=stderr)
        index = int(parse_qs(urlsplit(endpoint).query).get('page',['1'])[0]) - 1
        links = '\r\nLink: <https://api.github.com/ignored?page=next>; rel="next"' if isinstance(pages,list) and index + 1 < len(pages) else ''
        print(data.get('latest_headers', 'HTTP/1.1 200 OK' + links + '\r\n\r\n'), end='', file=stdout)
    print(json.dumps(payload), file=stdout)
