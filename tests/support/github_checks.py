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
                'checkSuite': {'id': 'suite-current', 'commit': {'oid': sha},
                    'app': {'databaseId': 1001},
                    'workflowRun': {'workflow': {'name': check.get('workflow', '')}}}})
    return {'data': {'repository': {'pullRequest': {'number': int(fields['number']),
        'headRefOid': sha, 'commits': {'nodes': [{'commit': {'oid': sha,
        'statusCheckRollup': {'contexts': {'nodes': nodes,
            'pageInfo': {'hasNextPage': False, 'endCursor': None}}}}}]}}}}}
'''
