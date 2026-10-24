"""Raw current-rollup responses for the existing isolated gh executables."""

CHECK_ROLLUP_FAKE = r'''
def current_checks(data, fields):
    if data.get('rollup_http'):
        print('HTTP ' + str(data['rollup_http']), file=sys.stderr)
        raise SystemExit(1)
    if 'rollup_pages' in data:
        page = data['rollup_pages'][fields.get('cursor', '')]
        if page.get('http'):
            print('HTTP ' + str(page['http']), file=sys.stderr)
            raise SystemExit(1)
        return page
    pr = data.get('pull_request') or data['prs'][0]
    sha = pr['head']['sha']
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
    return {'data': {'repository': {'pullRequest': {'number': int(fields['number']),
        'headRefOid': sha, 'commits': {'nodes': [{'commit': {'oid': sha,
        'statusCheckRollup': {'contexts': {'nodes': nodes,
            'pageInfo': {'hasNextPage': False, 'endCursor': None}}}}}]}}}}}
'''

LATEST_CHECK_FAKE = r'''
def latest_check_response(data, endpoint):
    from urllib.parse import urlsplit, parse_qs
    parsed = urlsplit(endpoint)
    if not ('/commits/' in parsed.path and parsed.path.endswith(('/check-runs', '/check-suites', '/status'))):
        return None
    head = parsed.path.split('/commits/')[1].split('/')[0]
    if parsed.path.endswith('/check-suites'):
        count = data.get('suite_count', 1)
        if 'suite_count_after' in data:
            with open(os.environ['SOURCE_TEST_LOG']) as stream:
                reads = sum('/check-suites?' in line for line in stream)
            if reads > 1: count = data['suite_count_after']
        return {'total_count': count, 'check_suites': [{'id':101,'head_sha':head}]}
    if data.get('latest_http'):
        print('HTTP ' + str(data['latest_http']), file=sys.stderr)
        raise SystemExit(1)
    if parsed.path.endswith('/check-runs'):
        assert '--paginate' in sys.argv and '--slurp' in sys.argv
        query = parse_qs(parsed.query)
        assert query['filter'] == ['latest'] and 'status' not in query
        if 'latest_pages' in data:
            return data['latest_pages']
        app = int(query['app_id'][0])
    nodes = data.get('latest_nodes')
    if nodes is None:
        pages = data.get('rollup_pages')
        if pages is None:
            pages = {'': current_checks(data, {'number': str((data.get('pull_request') or data['prs'][0])['number'])})}
        nodes = []
        for page in pages.values():
            if 'data' in page and not page.get('errors'):
                nodes.extend(page['data']['repository']['pullRequest']['commits']['nodes'][0]['commit']['statusCheckRollup']['contexts']['nodes'])
    if parsed.path.endswith('/status'):
        rows = [{'node_id': n['id'], 'context': n['context'], 'state': n['state'].lower()}
                for n in nodes if n.get('__typename') == 'StatusContext']
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
'''
