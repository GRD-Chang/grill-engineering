"""Stateful gh executable and remote-effect fixtures over private real Git.

Only external responses are synthetic; each consumer owns its paths and patches.
"""
from dataclasses import replace
import json
import os
from pathlib import Path
import sys

from agent_run.adapters.git_objects import LocalGit
from agent_run.adapters.github import GhGitHubReader
from agent_run.adapters.remote import RemoteEffects
from agent_run.contracts.write_authority import WriteAuthority
from agent_run.model import Effect, Execution
from support.github_checks import CHECK_ROLLUP_FAKE, LATEST_CHECK_FAKE
from support.github_requirements import source_issue
import shutil
import pytest
from agent_run.adapters.local_git import LocalEffects
from agent_run.model import DeliveryKind, EffectResult
from support.offline import Scenario
from support.core_operations import prepare_scripted_agent, scripted_return


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


def _reader(root: Path, monkeypatch, *, invalid: bool = False) -> tuple[GhGitHubReader, Path]:
    binary = root / 'bin'
    binary.mkdir()
    responses = root / 'responses.json'
    log = root / 'gh-calls.jsonl'
    def page(nodes, more=False, cursor=None):
        return {'nodes': nodes, 'pageInfo': {'hasNextPage': more, 'endCursor': cursor}}
    initial = {
        'pull_request': {'number': 17, 'state': 'open', 'title': 'Original PR', 'body': 'Original body\r\n保留原文',
                         'user': {'login': 'actual-publisher'}, 'merged': False, 'merge_commit_sha': None,
                         'head': {'ref': 'source', 'sha': '2' * 40, 'repo': {'full_name': 'outside/fork'}},
                         'base': {'ref': 'main', 'sha': '1' * 40, 'repo': {'full_name': 'example/project'}}},
        'checks': [], 'rulesets': [], 'classic': [],
        'repository': 'example/project', 'default_branch': 'main',
        'refs': {'refs/heads/main': '1' * 40},
        'parent': {'number': 1, 'title': 'Original Parent', 'body': 'Original Parent body\r\n保留原文',
                   'subIssues': None if invalid else page([{'number': 9}], True, 'parent-next')},
        'parent-next': {'number': 1, 'title': 'Original Parent', 'body': 'Original Parent body\r\n保留原文',
                        'subIssues': page([{'number': 3}])},
        '9': {'number': 9, 'title': 'Original later ticket', 'body': 'Same original ticket body', 'state': 'OPEN',
              'labels': page([{'name': 'ready-for-agent'}]),
              'blockedBy': page([{'number': 99, 'state': 'OPEN'}], True, 'blocker-next')},
        'blocker-next': {'number': 9, 'title': 'Original later ticket', 'body': 'Same original ticket body', 'state': 'OPEN',
              'labels': page([{'name': 'ready-for-agent'}]), 'blockedBy': page([{'number': 3, 'state': 'CLOSED'}])},
        '3': {'number': 3, 'title': 'Original first ticket', 'body': 'Original body', 'state': 'CLOSED',
              'labels': page([{'name': 'ready-for-agent'}, {'name': 'user-label'}]), 'blockedBy': page([])},
    }
    for key in ('parent', 'parent-next', '9', 'blocker-next', '3'):
        totals = {'subIssues': 2} if key.startswith('parent') else {'blockedBy': 2} if key in ('9', 'blocker-next') else {}
        initial[key] = source_issue(initial[key], totals=totals)
    responses.write_text(json.dumps(initial))
    script = binary / 'gh'
    script.write_text('#!' + sys.executable + '\n' + f'import sys\nsys.path.insert(0, {str(Path(__file__).parent.parent)!r})\nfrom support.github_checks import repository_response\n' + CHECK_ROLLUP_FAKE + LATEST_CHECK_FAKE + '''import json, os, sys
args=sys.argv[1:]
with open(os.environ['SOURCE_TEST_LOG'],'a') as f: f.write(json.dumps(args)+'\\n')
if args[:2]==['repo','view'] or (args[:1]==['api'] and '/git/ref/heads/' in args[1]):
    with open(os.environ['SOURCE_TEST_RESPONSES']) as f: data=json.load(f)
    result=repository_response(args, repository=data['repository'], default_branch=data['default_branch'],
                               refs=data['refs'], errors=data.get('ref_errors'))
    print(result.stdout,end='');print(result.stderr,end='',file=sys.stderr)
    raise SystemExit(result.returncode)
elif args[:2]==['api','graphql']:
    fields=dict(value.split('=',1) for index,value in enumerate(args) if index and args[index-1] in ('-F','-f'))
    if 'CurrentRequiredChecks' in fields['query']:
        with open(os.environ['SOURCE_TEST_RESPONSES']) as f: data=json.load(f)
        print(json.dumps(current_checks(data, fields)))
        raise SystemExit(0)
    with open(os.environ['SOURCE_TEST_RESPONSES']) as f: control=json.load(f)
    if control.get('graphql_http'):
        print('HTTP '+str(control['graphql_http'])+' source unavailable',file=sys.stderr)
        raise SystemExit(1)
    key=fields.get('cursor') or fields.get('labelsCursor') or ('parent' if fields['number']=='1' else fields['number'])
    issue=None if control[key] is None else dict(control[key])
    if fields['number']!='1' and issue is not None:
        for field, cursor in [('labels','labelsCursor'),('blockedBy','cursor')]:
            issue[field]=control[fields.get(cursor) or key][field]
    from support.github_requirements import source_response
    result=source_response(issue, control['repository'])
    with open(os.environ['SOURCE_TEST_RESPONSES']) as f: control=json.load(f)
    if 'graphql_errors' in control: result['errors']=control['graphql_errors']
    print(json.dumps(result))
elif args[:1]==['api'] and '/commits/' in args[1] and args[1].split('?')[0].endswith(('/check-runs','/check-suites','/status')):
    with open(os.environ['SOURCE_TEST_RESPONSES']) as f: data=json.load(f)
    print_latest_response(data,args)
elif args[:2]==['api','repos/example/project/pulls/17']:
    with open(os.environ['SOURCE_TEST_RESPONSES']) as f: data=json.load(f)
    pr=data['pull_request']
    with open(os.environ['SOURCE_TEST_LOG']) as f: count=sum(json.loads(line)[:2]==args[:2] for line in f)
    if count>1 and 'pr_head_after' in data: pr['head']['sha']=data['pr_head_after']
    print(json.dumps(pr))
elif args[:1]==['api'] and args[1].startswith('repos/example/project/actions/jobs/'):
    with open(os.environ['SOURCE_TEST_RESPONSES']) as f: data=json.load(f)
    if args[1].endswith('/logs'):
        if data.get('logs_http'):
            print('HTTP '+str(data['logs_http']),file=sys.stderr)
            raise SystemExit(1)
        print(data['job_logs'],end='')
    else:
        print(json.dumps(data['job']))
elif args[:2]==['pr','checks']:
    assert '--required' in args
    with open(os.environ['SOURCE_TEST_RESPONSES']) as f: print(json.dumps(json.load(f)['checks']))
elif args[:2]==['api','repos/example/project/rulesets']:
    assert '--paginate' in args and '--slurp' in args
    with open(os.environ['SOURCE_TEST_RESPONSES']) as f: print(json.dumps([json.load(f)['rulesets']]))
elif args[:1]==['api'] and args[1].startswith('repos/example/project/rulesets/'):
    with open(os.environ['SOURCE_TEST_RESPONSES']) as f: print(json.dumps(json.load(f)['ruleset_detail']))
elif args[:2] in (['api','repos/example/project/branches/main/protection/required_status_checks/contexts'], ['api','repos/example/project/branches/main/protection/required_status_checks']):
    with open(os.environ['SOURCE_TEST_RESPONSES']) as f: data=json.load(f)
    if data.get('classic_http'):
        print('HTTP '+str(data['classic_http']),file=sys.stderr)
        raise SystemExit(1)
    print(json.dumps(data['classic'] if args[1].endswith('/contexts') else {'contexts':data['classic'], 'checks':data.get('classic_checks', [{'context':context,'app_id':None} for context in data['classic']])}))
else:
    raise SystemExit('Unexpected external command: '+repr(args))
''')
    script.chmod(0o755)
    monkeypatch.setenv('PATH', str(binary) + os.pathsep + os.environ['PATH'])
    monkeypatch.setenv('SOURCE_TEST_RESPONSES', str(responses))
    monkeypatch.setenv('SOURCE_TEST_LOG', str(log))
    for name in ('GH_TOKEN', 'GITHUB_TOKEN'):
        monkeypatch.delenv(name, raising=False)
    return GhGitHubReader('example/project', working_directory=root, runner_identity={'source': 'host'}), log


def publication(git_repo: Path, tmp_path: Path) -> tuple[LocalGit, LocalEffects, Effect]:
    git = LocalGit(git_repo)
    git.run("config", "user.name", "Frozen Repository Author")
    git.run("config", "user.email", "frozen@example.invalid")
    base = git.commit("HEAD")
    owned = tmp_path / "work"
    owned.mkdir()
    checkout = owned / "delivery"
    git.run("worktree", "add", "--detach", str(checkout), base.sha)
    effect = Effect(
        "publication-original", "ticket:3", "publication_commit", "example/repository",
        "refs/heads/task", "refs/heads/run", base.sha, None, base, base, None,
        None, {"checkout": str(checkout), "owned_root": str(owned),
               "common_git_dir": str(git_repo / ".git"), "integrated_sha": base.sha,
               "integrated_tree": base.tree, "parents": [base.sha], "reuse_sha": None,
               "commit_message": "Publish the reviewed change", "intent_token": "publication-original"},
        Execution("run", "executor", 1),
    )
    return git, LocalEffects(git, owned_root=owned), effect

def _actual_publication_push(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                             command_exit: int) -> tuple[Scenario, Effect]:
    """Use a real Git transport command; its outer exit is not a forged result.

    Adapted from the independent dbfddc8d operation review. All network commands
    are confined to a local bare repository; gh may only return the fixture actor.
    """
    scenario = Scenario(tmp_path / "scenario", DeliveryKind.PARENT_ONLY)
    bare = scenario.port.physical.remote
    real_git = shutil.which("git")
    assert real_git is not None
    binary = tmp_path / "bin"
    binary.mkdir()
    trace = tmp_path / "push-command.jsonl"
    actor = binary / "gh"
    actor.write_text(f"#!{sys.executable}\nimport json,sys\nassert sys.argv[1:]==['api','user']\n"
                     "print(json.dumps({'login':'offline-publisher'}))\n")
    actor.chmod(0o755)
    command = binary / "git"
    command.write_text(f"""#!{sys.executable}
import json,subprocess,sys
from pathlib import Path
arguments=sys.argv[1:]
response=subprocess.run([{real_git!r},*arguments])
if arguments[:2]==['push',{str(bare)!r}]:
    actual_exit={command_exit} if response.returncode==0 else response.returncode
    with Path({str(trace)!r}).open('a') as output:
        output.write(json.dumps({{'arguments':arguments,'inner_git_returncode':response.returncode,
                                 'actual_command_returncode':actual_exit}})+'\\n')
    if actual_exit:
        print('actual transport command exited nonzero after the local push',file=sys.stderr)
    raise SystemExit(actual_exit)
raise SystemExit(response.returncode)
""")
    command.chmod(0o755)
    monkeypatch.setenv("PATH", str(binary) + os.pathsep + os.environ["PATH"])
    for variable in ("GH_TOKEN", "GITHUB_TOKEN"):
        monkeypatch.delenv(variable, raising=False)
    native = RemoteEffects(scenario.port.git.root, scenario.spec.repository,
                           runner_identity={"source": "host"}, remote=str(bare))
    runner = scenario.runner()
    execute = runner.ports.execute
    observed: list[tuple[Effect, EffectResult]] = []
    grants: list[str] = []

    def execute_original(effect, authorize):
        if effect.operation != "push":
            return execute(effect, authorize)
        def prepare_grant():
            final_admission = authorize.prepare()
            def current_grant():
                final_admission()
                grants.append(effect.effect_id)
            return current_grant
        result = native.execute(effect, WriteAuthority(prepare_grant))
        observed.append((effect, result))
        return result

    monkeypatch.setattr(runner.ports, "execute", execute_original)
    for _ in range(48):
        step = prepare_scripted_agent(scenario.kernel, runner.run_once())
        if step.call is not None:
            scripted_return(scenario.kernel, runner, step.call, scenario.agent)
        if observed:
            break
    else:
        raise AssertionError("Normal public operations never reached the original push")
    effect, physical = observed[0]
    assert grants == [effect.effect_id]
    commands = [json.loads(line) for line in trace.read_text().splitlines()]
    assert len(commands) == 1 and commands[0]["inner_git_returncode"] == 0
    assert commands[0]["actual_command_returncode"] == command_exit
    assert physical.disposition == "target" and physical.raw["write_returncode"] == command_exit
    assert scenario.port.git.run("--git-dir", str(bare), "rev-parse", effect.source_ref).stdout.strip() == effect.after_sha
    return scenario, effect
