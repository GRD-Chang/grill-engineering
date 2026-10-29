"""Selected Delivery GraphQL fields over the existing locked fake-gh state."""
from copy import deepcopy


def delivery_basis(document, fields, git, *, repository, actor, refresh_head=None):
    repo = {'id': 'repository-' + repository, 'nameWithOwner': repository}
    for key, sha in fields.items():
        if not key.startswith('object'):
            continue
        raw = git('cat-file', 'commit', sha)
        first = raw.split('\n', 1)[0]
        assert first.startswith('tree ')
        repo[key] = {'__typename': 'Commit', 'oid': sha, 'tree': {'oid': first[5:]}}
    if 'pr' in fields:
        number = int(fields['pr'])
        matches = [pr for pr in document['prs'] if pr['number'] == number]
        assert len(matches) <= 1
        value = None
        if matches:
            original = matches[0]
            if refresh_head is not None and not original['merged']:
                for side in ('head', 'base'):
                    sha = refresh_head('refs/heads/' + original[side]['ref'])
                    if sha:
                        original[side]['sha'] = sha
            pr = deepcopy(original)
            head_repo = pr['head'].get('repo')
            value = {'number': pr['number'], 'title': pr['title'], 'body': pr['body'],
                'state': 'MERGED' if pr['merged'] else pr['state'].upper(), 'author': pr.get('user'),
                'repository': {'nameWithOwner': repository},
                'baseRepository': None if pr['base'].get('repo') is None else {'nameWithOwner': pr['base']['repo']['full_name']},
                'headRepository': None if head_repo is None else {'nameWithOwner': head_repo['full_name']},
                'headRefName': pr['head']['ref'], 'headRefOid': pr['head']['sha'],
                'baseRefName': pr['base']['ref'], 'baseRefOid': pr['base']['sha'],
                'mergeCommit': None if not pr['merged'] else {'oid': pr['merge_commit_sha']}}
        repo['pullRequest'] = value
    data = {'repository': repo}
    if 'viewer { login }' in fields['query']:
        data['viewer'] = {'login': actor}
    return {'data': data}
