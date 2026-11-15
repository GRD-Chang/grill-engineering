"""Selected Delivery GraphQL fields over the existing locked fake-gh state."""
from copy import deepcopy
from functools import cache


def delivery_basis(document, fields, git, *, repository, actor, refresh_head=None, read_ref=None):
    repo = {'id': 'repository-' + repository, 'nameWithOwner': repository}

    @cache
    def raw_commit(sha):
        return git('cat-file', 'commit', sha)

    def commit(sha):
        raw = raw_commit(sha)
        first = raw.split('\n', 1)[0]
        assert first.startswith('tree ')
        return {'__typename': 'Commit', 'oid': sha, 'tree': {'oid': first[5:]}}

    for key, sha in fields.items():
        if not key.startswith('object'):
            continue
        repo[key] = commit(sha)
    resolve_ref = read_ref if read_ref is not None else document.get('refs', {}).get
    for key in ('sourceRef', 'targetRef'):
        if key not in fields:
            continue
        ref = fields[key]
        assert ref.startswith('refs/')
        namespace, separator, name = ref[5:].partition('/')
        assert namespace and separator and name
        sha = resolve_ref(ref)
        repo[key] = None if sha is None else {
            'name': name, 'prefix': 'refs/' + namespace + '/',
            'repository': {'nameWithOwner': repository}, 'target': commit(sha)}
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
            if pr['merged'] and 'parents(first:100)' in fields['query']:
                raw = raw_commit(pr['merge_commit_sha'])
                parents = []
                for line in raw.split('\n')[1:]:
                    if not line.startswith('parent '):
                        break
                    parents.append(line[7:])
                value['mergeCommit']['parents'] = {'nodes': [commit(sha) for sha in parents[:100]]}
        repo['pullRequest'] = value
    if 'head' in fields and 'base' in fields:
        matches = [pr for pr in document['prs'] if pr['head']['ref'] == fields['head']
                   and pr['base']['ref'] == fields['base']]
        nodes = []
        for pr in matches[:100]:
            head_repo = pr['head'].get('repo')
            nodes.append({'number': pr['number'], 'headRefName': pr['head']['ref'],
                'baseRefName': pr['base']['ref'], 'headRepositoryOwner': None if head_repo is None else
                {'login': head_repo['full_name'].split('/')[0]}})
        repo['discovery'] = {'nodes': nodes, 'pageInfo': {'hasNextPage': len(matches) > 100}}
    data = {'repository': repo}
    if 'viewer { login }' in fields['query']:
        data['viewer'] = {'login': actor}
    return {'data': data}
