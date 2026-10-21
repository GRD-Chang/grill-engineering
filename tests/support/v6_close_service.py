"""A local GitHub command boundary for the unchanged v6 close adapter.

The original GhGitHubPublisher creates the actual comment/intent and recovers
the numeric transition. This service owns only Issue/comment/event responses;
it never opens or edits a v6 database.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sys


_SERVICE = r'''
import json,os,sys
from pathlib import Path
args=sys.argv[1:]
path=Path(os.environ['V6_CLOSE_SERVICE'])
log=Path(os.environ['V6_CLOSE_CALLS'])
with log.open('a') as out: out.write(json.dumps(args)+'\n')
data=json.loads(path.read_text())
external=json.loads((path.parent/'source/github.json').read_text())
if args[:2]==['repo','view']:
    print(json.dumps({'nameWithOwner':external['repository'],
                      'defaultBranchRef':{'name':external['default_branch']}}))
elif args[:2]==['api','repos/example/project/commits/main']:
    print(json.dumps({'sha':external['default_head_sha']}))
elif args[:2]==['api','graphql']:
    fields=dict(value.split('=',1) for index,value in enumerate(args)
                if index and args[index-1] in ('-F','-f'))
    def page(nodes): return {'nodes':nodes,'pageInfo':{'hasNextPage':False,'endCursor':None}}
    if fields['number']=='1':
        parent=external['parent']
        value={key:parent[key] for key in ('number','title','body')}
        value['subIssues']=page([{'number':number} for number in parent['sub_issues']])
    else:
        issue=external['issues'][fields['number']]
        value={key:issue[key] for key in ('number','title','body','state')}
        value['labels']=page([{'name':name} for name in issue['labels']])
        value['blockedBy']=page(issue['blocked_by'])
    print(json.dumps({'data':{'repository':{'issue':value}}}))
elif args[:2]==['api','repos/example/project/pulls/1']:
    pr=external['delivery']['pull_requests'][0]
    print(json.dumps({'number':pr['number'],'state':'closed','merged':True,
        'merge_commit_sha':pr['integrated_sha'],'title':pr['title'],'body':pr['body'],
        'user':{'login':'fixture-publisher'},
        'head':{'ref':pr['branch'],'sha':pr['head_sha'],'repo':{'full_name':external['repository']}},
        'base':{'ref':pr['base_branch'],'sha':data.get('pr_base_sha',pr['base_sha']),'repo':{'full_name':external['repository']}}}))
elif args[:2]==['api','repos/example/project/issues/2']:
    issue=external['issues']['2']
    print(json.dumps({**issue,'state':data['state'].lower(),'updated_at':data['updated_at']}))
elif args[:2]==['api','repos/example/project/issues/2/comments']:
    assert '--paginate' in args and '--slurp' in args
    print(json.dumps([[{'id':comment['id'],'body':comment['body'],
                        'user':comment['author'],'created_at':comment['createdAt']}
                      for comment in data['comments']]]))
elif args[:2]==['api','user']:
    print(json.dumps({'login':'fixture-publisher'}))
elif args[:3]==['issue','view','2']:
    print(json.dumps({'state':data['state'],'comments':data['comments'],'updatedAt':data['updated_at']}))
elif args[:2]==['api','repos/example/project/issues/2/events']:
    assert '--paginate' in args and '--slurp' in args
    print(json.dumps([data['events']]))
elif args[:3]==['issue','comment','2']:
    body=args[args.index('--body')+1]
    data['comments'].append({'id':301,'body':body,'author':{'login':'fixture-publisher'},
                             'createdAt':'2026-10-21T00:00:01Z'})
    data['updated_at']='2026-10-21T00:00:01Z'
    path.write_text(json.dumps(data))
    print('{}')
elif args[:3]==['issue','close','2']:
    assert data['state']=='OPEN' and data['comments'] and not data['events']
    data['state']='CLOSED'
    data['updated_at']='2026-10-21T00:00:02Z'
    data['events'].append({'id':4101,'event':'closed','actor':{'login':'fixture-publisher'},
                           'created_at':'2026-10-21T00:00:02Z'})
    path.write_text(json.dumps(data))
    print('{}')
else:
    raise SystemExit('Unexpected GitHub command: '+repr(args))
'''


def close_service_environment(root: Path, environment: dict[str, str]) -> dict[str, str]:
    binary = root / 'close-service-bin'
    binary.mkdir()
    script = binary / 'gh'
    script.write_text('#!' + sys.executable + '\n' + _SERVICE)
    script.chmod(0o755)
    # Only the remote read boundary is synthetic. Every other Git operation
    # still executes the real binary against the original managed clone.
    real_git = shutil.which('git', path=environment['PATH'])
    assert real_git is not None
    git = binary / 'git'
    git.write_text('#!' + sys.executable + '\n' + '''import json,os,sys
from pathlib import Path
args=sys.argv[1:]
if args[:1]==['ls-remote']:
    root=Path(os.environ['V6_CLOSE_SERVICE']).parent
    data=json.loads((root/'source/github.json').read_text())
    branches={**data['delivery']['published_branches'],data['default_branch']:data['default_head_sha']}
    for ref in args[args.index('--')+2:]:
        if ref.startswith('refs/heads/') and ref[11:] in branches:
            print(branches[ref[11:]]+'\\t'+ref)
else:
    os.execv(os.environ['V6_CLOSE_REAL_GIT'],[os.environ['V6_CLOSE_REAL_GIT'],*args])
''')
    git.chmod(0o755)
    service = root / 'close-service.json'
    service.write_text(json.dumps({'state': 'OPEN', 'comments': [], 'events': [],
                                   'updated_at': '2026-10-21T00:00:00Z'}))
    return {**environment, 'PATH': str(binary) + os.pathsep + environment['PATH'],
            'V6_CLOSE_SERVICE': str(service), 'V6_CLOSE_CALLS': str(root / 'close-service-calls.jsonl'),
            'V6_CLOSE_REAL_GIT': real_git}
