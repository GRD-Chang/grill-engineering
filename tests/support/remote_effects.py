"""Stateful gh executable and remote-effect fixtures over private real Git.

Only external responses are synthetic; each consumer owns its paths and patches.
"""
from dataclasses import replace
import json
import os
from pathlib import Path
import sys

from agent_run.adapters.git_objects import LocalGit
from agent_run.adapters.remote import RemoteEffects
from agent_run.contracts.write_authority import WriteAuthority
from agent_run.model import Effect, Execution
from support.github_checks import CHECK_ROLLUP_FAKE, LATEST_CHECK_FAKE


def _fixture(tmp_path, monkeypatch, *, repository="example/project", actor="publisher"):
    repo = tmp_path / "repo"
    repo.mkdir()
    git = LocalGit(repo)
    git.run("init", "--initial-branch=main")
    git.run("config", "--local", "user.name", "Remote fixture author")
    git.run("config", "--local", "user.email", "remote-fixture@example.invalid")
    git.run("config", "gc.auto", "0")
    (repo / "base").write_text("original\n")
    git.run("add", ".")
    git.run("commit", "-m", "base")
    base = git.commit("HEAD")
    remote = tmp_path / "origin.git"
    git.run("init", "--bare", str(remote))
    git.run("remote", "add", "origin", str(remote))
    git.run("push", "origin", "refs/heads/main", base.sha + ":refs/heads/delivery")
    git.run("switch", "-c", "delivery")
    (repo / "change").write_text("actual publication\n")
    git.run("add", ".")
    git.run("commit", "-m", "publication")
    published = git.commit("HEAD")
    state = tmp_path / "github.json"
    state.write_text(json.dumps({"prs": [], "comments": [], "events": [], "issue_state": "open"}))
    calls = tmp_path / "calls.jsonl"
    binary = tmp_path / "bin"
    binary.mkdir()
    fake = binary / "gh"
    fake.write_text("#!" + sys.executable + "\n" +
                    FAKE_GH.replace("example/project", repository).replace("publisher", actor))
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(binary) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("REMOTE_TEST_STATE", str(state))
    monkeypatch.setenv("REMOTE_TEST_CALLS", str(calls))
    monkeypatch.setenv("REMOTE_TEST_BARE", str(remote))
    for name in ("GH_TOKEN", "GITHUB_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    port = RemoteEffects(repo, repository, runner_identity={"source": "host"})
    effect = Effect("push-1", "ticket:2", "push", repository, "refs/heads/delivery", "refs/heads/main",
        base.sha, published.sha, published, base, None, actor, {}, Execution("run-1", "worker", 1))
    return port, git, effect, state, calls


def _change(state, **values):
    document = json.loads(state.read_text())
    document.update(values)
    state.write_text(json.dumps(document))


def _pr_effect(effect):
    return replace(effect, effect_id="pr-1", operation="pr_write", before_sha=effect.after_sha,
                   payload={"title": "Original title", "body": "Original narrative\r\n中文", "prior": None})


def _publish_pr(port, effect):
    assert port.execute(effect, WriteAuthority(lambda: lambda: None)).disposition == "target"
    effect = _pr_effect(effect)
    result = port.execute(effect, WriteAuthority(lambda: lambda: None))
    assert result.disposition == "target" and result.pr.number == 17
    return effect, result.pr


def _source_kernel(tmp_path, port, push):
    from agent_run.model import Delivery, DeliveryKind, RunSpec
    from agent_run.core.kernel import Kernel
    from agent_run.contracts.agent_profiles import resolve_profiles
    from agent_run.config.resources import resolve_resources
    from agent_run.model import CreationInputs, ParentIssue, Requirements
    source = Requirements(ParentIssue(1, "Parent", "Whole requirement", ()), ())
    job = Delivery("parent:1", DeliveryKind.PARENT_ONLY, 1, "not-authority", push.base,
                   push.source_ref, push.target_ref)
    inputs = CreationInputs(resolve_profiles(preset="economy"), resolve_resources("en"),
                            {"source": "host"}, {"source": "host"}, {"enabled": False}, {"source": "explicit-test"})
    spec = RunSpec("source-run", port.repository, 1, "not-parent", "not-graph", (job,),
                   language="en", repository_root=str(port.root.resolve()), inputs=inputs)
    return Kernel.create(tmp_path / "source.sqlite3", spec, requirements=source)


FAKE_GH = ('import sys\nsys.path.insert(0, ' + repr(str(Path(__file__).parent.parent)) + ')\n'
           'from support.fixture_text import read_text, write_text\n'
           + CHECK_ROLLUP_FAKE + LATEST_CHECK_FAKE) + r'''import json, os, sys
args=sys.argv[1:]
state=os.environ['REMOTE_TEST_STATE']
import fcntl
server_lock=open(state+'.lock','a+')
fcntl.flock(server_lock.fileno(),fcntl.LOCK_EX)
d=json.loads(read_text(state))
os.environ.update(GIT_AUTHOR_NAME='Publisher',GIT_AUTHOR_EMAIL='publisher@example.invalid',GIT_COMMITTER_NAME='Publisher',GIT_COMMITTER_EMAIL='publisher@example.invalid',GIT_AUTHOR_DATE='2000-01-01T00:00:00Z',GIT_COMMITTER_DATE='2000-01-01T00:00:00Z')
with open(os.environ['REMOTE_TEST_CALLS'],'a') as f: f.write(json.dumps(args)+'\n')
def git(*args, input=None):
    import subprocess
    result=subprocess.run(['git','--git-dir',os.environ['REMOTE_TEST_BARE'],*args],text=True,input=input,capture_output=True,check=True)
    return result.stdout.strip()
def head(ref):
    import subprocess
    r=subprocess.run(['git','--git-dir',os.environ['REMOTE_TEST_BARE'],'rev-parse','--verify',ref],text=True,capture_output=True)
    return r.stdout.strip() if r.returncode==0 else None
if args[:2]==['repo','view']:
    print(json.dumps({'nameWithOwner':'example/project','defaultBranchRef':{'name':'main'}}));raise SystemExit(0)
if args[:2]==['pr','checks']:
    print(json.dumps(d.get('checks',[])));raise SystemExit(0)
if args[0]!='api': raise SystemExit('unexpected '+repr(args))
endpoint=args[1].split('?',1)[0]
method=args[args.index('--method')+1] if '--method' in args else 'GET'
body=json.loads(read_text(args[args.index('--input')+1])) if '--input' in args else {}
status_operation=False
if d.get('read_error') and method=='GET':
    print('HTTP 403 forbidden',file=sys.stderr);raise SystemExit(1)
if endpoint=='user': result={'login':d.get('actor','publisher')}
elif endpoint=='graphql' and method=='GET':
    fields=dict(value.split('=',1) for index,value in enumerate(args) if index and args[index-1] in ('-F','-f'))
    if 'CurrentRequiredChecks' in fields.get('query',''):
        print(json.dumps(current_checks(d, fields)));raise SystemExit(0)
    graph=d.get('source_graph',{'parent':{'number':1,'title':'Parent','body':d.get('source_body','Whole requirement'),'sub_issues':[]},'issues':{}})
    if fields['number']=='1':
        p=graph['parent'];issue={'number':1,'title':p['title'],'body':p['body'],'subIssues':{'nodes':[{'number':n} for n in p['sub_issues']],'pageInfo':{'hasNextPage':False,'endCursor':None}}}
    else:
        p=graph['issues'][fields['number']];issue={**p,'labels':{'nodes':[{'name':name} for name in p['labels']],'pageInfo':{'hasNextPage':False,'endCursor':None}},'blockedBy':{'nodes':[],'pageInfo':{'hasNextPage':False,'endCursor':None}}}
    from support.github_requirements import source_issue, source_response
    result=source_response(source_issue(issue))
elif endpoint=='graphql':
    v=body['variables']; assert v['issueId']=='issue-node-2' and head('refs/heads/'+v['name'])==v['oid']
    d.setdefault('links',[]).append({'ref':v['name'],'sha':v['oid']})
    result={'data':{'createLinkedBranch':{'linkedBranch':{'ref':{'name':v['name'],'target':{'oid':v['oid']}}}}}}
elif '/commits/' in endpoint and endpoint.endswith(('/check-runs','/check-suites','/status')):
    print_latest_response(d,args);raise SystemExit(0)
elif endpoint=='repos/example/project/commits/main': result={'sha':head('refs/heads/main')}
elif endpoint.startswith('repos/example/project/git/ref/heads/'):
    from urllib.parse import unquote
    ref='refs/heads/'+unquote(endpoint.split('/git/ref/heads/',1)[1])
    sha=head(ref)
    if sha is None:
        print('HTTP 404 ref absent',file=sys.stderr);raise SystemExit(1)
    result={'ref':ref,'object':{'type':'commit','sha':sha}}
elif endpoint=='repos/example/project/rulesets': result=[[]]
elif '/protection/required_status_checks' in endpoint: result={'contexts':[],'checks':[]}
elif '/git/commits/' in endpoint:
    import subprocess
    sha=endpoint.rsplit('/',1)[1]
    raw=subprocess.run(['git','--git-dir',os.environ['REMOTE_TEST_BARE'],'cat-file','commit',sha],capture_output=True,check=True).stdout.decode('utf-8')
    result={'sha':sha,'tree':{'sha':git('show','-s','--format=%T',sha)},'parents':[{'sha':x} for x in git('show','-s','--format=%P',sha).split()],'message':raw.partition('\n\n')[2]}
elif '/compare/' in endpoint:
    import subprocess
    a,b=endpoint.rsplit('/',1)[1].split('...')
    r=subprocess.run(['git','--git-dir',os.environ['REMOTE_TEST_BARE'],'merge-base','--is-ancestor',a,b])
    result={'status':'identical' if a==b else 'ahead' if r.returncode==0 else 'diverged','base_commit':{'sha':a},'merge_base_commit':{'sha':git('merge-base',a,b)}}
elif endpoint.startswith('repos/example/project/pulls'):
    tail=endpoint.split('/pulls',1)[1]
    if not tail or tail.startswith('?'):
        if method=='POST':
            pr={'number':17,'state':'open','title':body['title'],'body':body['body'],'user':{'login':'publisher'},'merged':False,'merge_commit_sha':None,
                'head':{'sha':head('refs/heads/'+body['head']),'ref':body['head'],'repo':{'full_name':'example/project'}},
                'base':{'sha':head('refs/heads/'+body['base']),'ref':body['base'],'repo':{'full_name':'example/project'}}}
            d['prs'].append(pr)
            result=pr
        else:
            from urllib.parse import parse_qs,urlsplit
            query=parse_qs(urlsplit(args[1]).query)
            selected=[p for p in d['prs'] if ('head' not in query or p['head']['ref']==query['head'][0].split(':',1)[-1])
                      and ('base' not in query or p['base']['ref']==query['base'][0])]
            result=[selected] if '--slurp' in args else selected
    else:
        number=int(tail.split('/')[1]);pr=next((p for p in d['prs'] if p['number']==number),None)
        if pr is None: print('HTTP 404',file=sys.stderr);raise SystemExit(1)
        if tail.endswith('/merge'):
            assert body['sha']==pr['head']['sha']
            parents=[pr['base']['sha']]+([] if body['merge_method']=='squash' else [pr['head']['sha']])
            command=['commit-tree',git('show','-s','--format=%T',pr['head']['sha'])]
            for parent in parents:command+=['-p',parent]
            message=body['commit_title']+('\n\n'+body['commit_message'] if body.get('commit_message') else '')
            sha=git(*command,input=message if message.endswith('\n') else message+'\n')
            git('update-ref','refs/heads/'+pr['base']['ref'],sha,pr['base']['sha'])
            pr.update(state='closed',merged=True,merge_commit_sha=sha,merged_by={'login':'publisher'})
            result={'merged':True,'sha':sha}
        elif method=='PATCH':
            pr.update(body);result=pr
            if body.get('state')=='closed':
                d['events'].append({'id':len(d['events'])+1,'event':'closed','actor':{'login':'publisher'},'created_at':'2026-10-21T10:01:00Z'})
        else:result=pr
elif endpoint.startswith('repos/example/project/issues/comments/'):
    status_operation=True
    identifier=int(endpoint.rsplit('/',1)[1])
    matches=[row for row in d['comments'] if row['id']==identifier and row['body'].startswith('<!-- agent-run-status:')]
    if len(matches)!=1 or method!='PATCH' or set(body)!={'body'}:
        raise SystemExit('unexpected Status comment object')
    result=matches[0];result['body']=body['body']
    d.setdefault('status_writes',[]).append({'method':'PATCH','id':identifier,'body':body['body']})
elif endpoint.startswith('repos/example/project/issues/') and endpoint.endswith('/comments'):
    if d.get('issue_read_error') and method=='GET':
        print('HTTP 403 Issue unavailable',file=sys.stderr);raise SystemExit(1)
    number=int(endpoint.split('/issues/',1)[1].split('/',1)[0])
    graph=d.get('source_graph',{})
    known_issue=number==2 or number==graph.get('parent',{}).get('number') or str(number) in graph.get('issues',{})
    known_pr=any(pr['number']==number for pr in d['prs'])
    if not (known_issue or known_pr):raise SystemExit('unknown comment target')
    url='https://api.github.com/repos/example/project/issues/'+str(number)
    status_operation=method=='POST' and body.get('body','').startswith('<!-- agent-run-status:')
    if method=='POST':
        if status_operation and not known_pr:raise SystemExit('unknown Status target PR')
        result={'id':max((row['id'] for row in d['comments']),default=0)+1,'body':body['body'],
                'user':{'login':d.get('actor','publisher')},'issue_url':url,'created_at':'2026-10-21T10:00:00Z'}
        d['comments'].append(result)
        if status_operation:d.setdefault('status_writes',[]).append({'method':'POST','id':result['id'],'body':body['body']})
    elif method=='GET':
        # Legacy fixture rows without issue_url are the declared Ticket #2's
        # completion comments. Never expose those rows on the distinct PR.
        rows=[row for row in d['comments'] if row.get('issue_url','https://api.github.com/repos/example/project/issues/2')==url]
        result=[rows] if '--slurp' in args else rows
    else:raise SystemExit('unexpected comment method')
elif endpoint.startswith('repos/example/project/issues/'):
    if d.get('issue_read_error') and method=='GET':
        print('HTTP 403 Issue unavailable',file=sys.stderr);raise SystemExit(1)
    if endpoint.endswith('/events'):result=[d['events']] if '--slurp' in args else d['events']
    elif method=='PATCH':
        d['issue_state']=body['state'];
        event='closed' if body['state']=='closed' else 'reopened'
        at='2026-10-21T10:01:00Z' if event=='closed' else '2026-10-21T10:02:00Z'
        d['events'].append({'id':len(d['events'])+1,'event':event,'actor':{'login':'publisher'},'created_at':at});result={'state':d['issue_state']}
    else:result={'number':2,'node_id':'issue-node-2','state':d['issue_state']}
else:raise SystemExit('unexpected endpoint '+endpoint)
if method!='GET' and not status_operation and d.get('read_error_after_write'):d['read_error']=True
write_text(state,json.dumps(d))
print(json.dumps(result))
if method!='GET' and not status_operation and d.get('lose_operation')==('issue_link' if endpoint=='graphql' else 'merge' if endpoint.endswith('/merge') else 'comment' if endpoint.endswith('/comments') else 'close' if '/issues/' in endpoint else 'pr'):
    print('response lost after actual mutation',file=sys.stderr);raise SystemExit(1)
'''
