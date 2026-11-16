"""Status display and remote identity contracts, without unrelated delivery setup."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path

import pytest

from agent_run.adapters.remote import RemoteEffects
from agent_run.contracts.status_comment import StatusProjection, StatusTarget
from agent_run.contracts.write_authority import WriteAuthority
from agent_run.notifications.status_comment import StatusComments
from agent_run.queries.read_facts import action_guidance
from agent_run.queries.status_comment import render_status_body
from agent_run.storage import notification_outbox as outbox
from support.status_comments import CommentsAPI, display_inputs


@pytest.mark.parametrize("field", ["target", "marker", "comment_id", "confirmed_body", "intent"])
def test_status_missing_intent_field_is_damaged_display_not_business_exception(confirmed_display_receipt, field):
    # Journal-shape validation only, not proof of a real publication/dispatch.
    # The journal contract below retains real sender rejection and zero reads;
    # the Core projection test retains SQLite isolation and corruption handling.
    projection, document, _comments, _writes = confirmed_display_receipt
    entry, = document["remote_status"]["bindings"].values()
    del entry[field]
    with pytest.raises(ValueError, match="Status binding changed"):
        StatusComments._entry(document, projection)


@pytest.fixture
def confirmed_display_receipt():
    """Controlled assertion input, not a Core publication or external write."""
    target = StatusTarget('example/project', 'frozen-run', 1, 'parent:1', 1, 1, 7,
        'offline-publisher', 'publication-effect', 'publication-receipt', 'refs/heads/parent', 'refs/heads/main')
    projection = StatusProjection(target, 'Recorded display')
    journal = {'pending': [], 'records': [], 'seen': []}
    entry = StatusComments._entry(journal, projection)
    body = entry['marker'] + '\n' + projection.body
    entry.update(comment_id=77, confirmed_body=body, receipt={
        'intent': 'a' * 32, 'comment_id': 77, 'write_result': None})
    comment = {'id': 77, 'body': body, 'user': {'login': target.actor},
        'issue_url': 'https://api.github.com/repos/example/project/issues/7'}
    writes = [['api', 'repos/example/project/issues/7/comments', '--method', 'POST', '--input', '-']]
    return projection, journal, [comment], writes


def test_parent_only_comment_accounting_requires_the_existing_exact_status_receipt(confirmed_display_receipt):
    from support.notification import assert_confirmed_status_comment
    projection, journal, comments, writes = confirmed_display_receipt
    before = deepcopy(journal)
    assert_confirmed_status_comment(projection, journal, comments, writes)
    assert journal == before


@pytest.fixture
def display(tmp_path):
    projection, operation_binding = display_inputs(tmp_path)
    current, binding = [projection], [operation_binding]
    def read(subject):
        assert subject == current[0].target.subject
        return current[0]
    owner = StatusComments(tmp_path, read_projection=read, read_binding=lambda: binding[0])
    return owner, current, binding, CommentsAPI(projection.target)


def test_lost_patch_keeps_original_intent_until_readback_before_new_copy(tmp_path, display):
    owner, current, binding, api = display
    subject, run_id = current[0].target.subject, current[0].target.run_id
    assert owner.deliver(subject, api=api) == 'confirmed'
    current[0] = replace(current[0], body='first update')
    api.lose = True
    assert owner.deliver(subject, api=api) == 'unavailable'
    original = owner.stage(subject)
    assert original['comment_id'] == 100 and original['sent']
    current[0] = replace(current[0], body='newer update')
    assert owner.stage(subject) == original
    recovered = StatusComments(tmp_path, read_projection=lambda _: current[0], read_binding=lambda: binding[0])
    api.lose = False
    assert [command[1] for command in api.commands] == ['POST', 'PATCH']
    assert recovered.deliver(subject, api=api) == 'confirmed'
    assert api.comments[0]['body'].endswith('first update')
    assert recovered.deliver(subject, api=api) == 'confirmed'
    assert api.comments[0]['body'].endswith('newer update')
    assert [command[1] for command in api.commands] == ['POST', 'PATCH', 'PATCH']
    assert len(api.comments) == 1
    assert 'private diagnostic' not in outbox.notification_path(tmp_path, run_id).read_text()


def test_capacity_and_damaged_journal_never_reach_remote(tmp_path, monkeypatch, display):
    owner, current, _binding, api = display
    subject, run_id = current[0].target.subject, current[0].target.run_id
    with monkeypatch.context() as patch:
        patch.setattr(outbox, 'MAX_JOURNAL_BYTES', 32)
        assert owner.deliver(subject, api=api) == 'unavailable'
    assert api.reads == 0 and api.commands == []
    owner.stage(subject)
    with outbox.transaction(tmp_path, run_id) as document:
        document['remote_status']['version'] = 999
    with pytest.raises(ValueError, match='journal'):
        owner.stage(subject)
    assert owner.deliver(subject, api=api) == 'unavailable'
    assert api.reads == 0 and api.commands == []
    with outbox.transaction(tmp_path, run_id) as document:
        document['remote_status']['version'] = 1
    assert owner.deliver(subject, api=api) == 'confirmed'
    with outbox.transaction(tmp_path, run_id) as document:
        entry, = document['remote_status']['bindings'].values()
        del entry['intent']
    reads, commands = api.reads, list(api.commands)
    assert owner.deliver(subject, api=api) == 'unavailable'
    outbox.notification_path(tmp_path, run_id).write_text('{"remote_status":')
    assert owner.deliver(subject, api=api) == 'unavailable'
    assert api.reads == reads and api.commands == commands


@pytest.mark.parametrize('dispatched', [True, False], ids=['sent', 'prepared'])
def test_real_process_exit_retains_original_status_through_compaction(tmp_path, dispatched, display):
    import os
    import subprocess
    import sys
    owner, current, _binding, api = display
    subject, run_id = current[0].target.subject, current[0].target.run_id
    evidence = tmp_path / 'physical-comment.json'
    script = '''
import json,os,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from support.status_comments import CommentsAPI,display_inputs
from agent_run.notifications.status_comment import StatusComments
root,output=Path(sys.argv[2]),Path(sys.argv[3])
dispatched=sys.argv[4]=='True'
projection,binding=display_inputs(root)
class Interrupted(CommentsAPI):
 def object(self,endpoint):
  if not dispatched:os._exit(18)
  return super().object(endpoint)
 def write(self,*args):
  super().write(*args)
  output.write_text(json.dumps({'comments':self.comments,'commands':self.commands}))
  os._exit(17)
api=Interrupted(projection.target)
StatusComments(root,read_projection=lambda _:projection,read_binding=lambda:binding).deliver(projection.target.subject,api=api)
raise AssertionError('physical dispatch was not reached')
'''
    result = subprocess.run([sys.executable, '-c', script, str(Path(__file__).parent),
        str(tmp_path), str(evidence), str(dispatched)], capture_output=True, text=True, timeout=20)
    assert result.returncode == (17 if dispatched else 18), result.stderr
    physical = json.loads(evidence.read_text()) if dispatched else {'comments': [], 'commands': []}
    assert len(physical['commands']) == int(dispatched)
    with outbox.transaction(tmp_path, run_id) as document:
        entry, = document['remote_status']['bindings'].values()
        assert entry['intent']['sent'] is dispatched and entry['claim']['pid'] != os.getpid()
        original = dict(entry['intent'])
        for n in range(outbox.MAX_RECORDS + 1):
            outbox.enqueue(document, {'id': f'other-{n}', 'kind': 'stage_start'}, 0)
            document['pending'][-1]['outcome'] = 'success'
            outbox.settle(document, 0)
        assert len(document['records']) == outbox.MAX_RECORDS and entry['intent'] == original
    if not dispatched:
        current[0] = replace(current[0], body='changed after preparation exit')
    api.comments = physical['comments']
    assert owner.deliver(subject, api=api) == 'confirmed'
    if dispatched:
        assert api.commands == []
    else:
        assert len(api.commands) == 1 and api.comments[0]['body'].endswith(current[0].body)


def test_denied_post_keeps_observed_exit_when_readback_fails(tmp_path, display):
    import subprocess
    from agent_run.errors import GitHubReadError
    owner, current, _binding, _api = display
    subject, run_id = current[0].target.subject, current[0].target.run_id
    class Denied(CommentsAPI):
        readback_unavailable = False
        def write(self, endpoint, method, body):
            self.commands.append((endpoint, method, dict(body)))
            self.readback_unavailable = True
            return subprocess.CompletedProcess([], 1, '', 'HTTP 403; secret must not enter journal')
        def list(self, endpoint):
            if self.readback_unavailable:
                raise GitHubReadError('synthetic_read_failed', 'secret must not enter journal')
            return super().list(endpoint)
    api = Denied(current[0].target)
    assert owner.deliver(subject, api=api) == 'unknown'
    api.readback_unavailable = False
    assert owner.deliver(subject, api=api) == 'unknown'
    assert len(api.commands) == 1
    entry, = outbox.read_notifications(tmp_path, run_id)['remote_status']['bindings'].values()
    assert entry['write_result'] == {'intent': entry['intent']['id'], 'returncode': 1}
    assert 'secret must' not in outbox.notification_path(tmp_path, run_id).read_text()


def test_app_403_is_scoped_display_failure_before_dispatch(tmp_path, monkeypatch, display):
    from agent_run.adapters.auth.app import GitHubCredentialError
    owner, current, binding, api = display
    subject, run_id = current[0].target.subject, current[0].target.run_id
    identity = {'source': 'app', 'app_profile': {'app_id': '123', 'installation_id': '456',
        'private_key_path': str(tmp_path / 'unread-key.pem')},
        'app_read_only': False, 'app_current_repository_only': True}
    binding[0] = replace(binding[0], inputs=replace(binding[0].inputs, runner_identity=identity))
    def denied(profile):
        assert profile.app_id == '123' and profile.installation_id == '456'
        raise GitHubCredentialError('synthetic-secret', http_status=403)
    monkeypatch.setattr('agent_run.adapters.auth.app.resolve_app_login', denied)
    assert owner.deliver(subject, api=api) == 'unavailable'
    entry, = outbox.read_notifications(tmp_path, run_id)['remote_status']['bindings'].values()
    assert entry['diagnostic'] == {'code': 'github_credential_unavailable', 'http_status': 403}
    assert 'claim' not in entry and entry['intent']['sent'] is False
    assert api.commands == [] and api.reads == 0
    assert 'synthetic-secret' not in outbox.notification_path(tmp_path, run_id).read_text()
    def unexpected(profile):
        raise RuntimeError('unexpected program failure')
    monkeypatch.setattr('agent_run.adapters.auth.app.resolve_app_login', unexpected)
    with pytest.raises(RuntimeError, match='unexpected program failure'):
        owner.deliver(subject, api=api)
    entry, = outbox.read_notifications(tmp_path, run_id)['remote_status']['bindings'].values()
    assert 'claim' not in entry and entry['intent']['sent'] is False


def test_remote_status_rejects_foreign_comments_and_publication(tmp_path, monkeypatch, confirmed_display_receipt):
    projection, journal, comments, _writes = confirmed_display_receipt
    target = projection.target
    entry = journal['remote_status']['bindings'][target.key]
    marker = entry['marker']
    intent = {'comment_id': 77, 'before': comments[0]['body'], 'body': marker + '\nNew status'}
    api = CommentsAPI(target)
    remote = RemoteEffects(tmp_path, target.repository, runner_identity={'source': 'host'}, api=api)
    api.comments = [{'id': n, 'body': 'other', 'user': {'login': 'other'}} for n in range(200, 351)] + comments
    assert remote.read_status_comment(target, marker, intent, lambda: False)['disposition'] == 'prior'
    cases = [[]]
    for field, value in (('body', comments[0]['body'] + '\nHuman edit'),
                         ('user', {'login': 'foreign'}),
                         ('issue_url', 'https://api.github.com/repos/example/project/issues/999')):
        cases.append([{**comments[0], field: value}])
    cases.append([*comments, {**comments[0], 'id': 78}])
    cases.append([{**comments[0], 'body': comments[0]['body'].replace(marker[-35:-3], '0' * 32)}])
    for rows in cases:
        api.comments = rows
        assert remote.read_status_comment(target, marker, intent, lambda: False)['disposition'] == 'third'
        with pytest.raises(ValueError, match='changed'):
            remote.write_status_comment(target, marker, intent, WriteAuthority(lambda: lambda: None), lambda: False)
    api.comments = deepcopy(comments)
    original_object = api.object
    for changed in ('actor', 'pr', 'repository', 'ref'):
        def changed_object(endpoint, changed=changed):
            row = original_object(endpoint)
            if endpoint == 'user':
                if changed == 'actor':
                    row['login'] = 'different-login'
            elif changed == 'pr':
                row['number'] += 1
            elif changed == 'repository':
                row['head']['repo']['full_name'] = 'foreign/repository'
            elif changed == 'ref':
                row['head']['ref'] = 'foreign-branch'
            return row
        with monkeypatch.context() as patch:
            patch.setattr(api, 'object', changed_object)
            assert remote.read_status_comment(target, marker, intent, lambda: False)['disposition'] == 'third'
    replacement = marker.replace(marker.rsplit(':', 1)[1].split()[0], 'f' * 32)
    unbound = {**intent, 'comment_id': None, 'before': None, 'body': intent['body'].replace(marker, replacement)}
    assert remote.read_status_comment(target, replacement, unbound, lambda: False)['disposition'] == 'third'
    assert api.commands == []


def test_remote_status_repository_case_preserves_identity(tmp_path, monkeypatch, confirmed_display_receipt):
    projection, journal, comments, _writes = confirmed_display_receipt
    target = projection.target
    marker = journal['remote_status']['bindings'][target.key]['marker']
    intent = {'comment_id': 77, 'before': comments[0]['body'], 'body': marker + '\nUpdated'}
    api = CommentsAPI(target)
    api.comments = comments
    comments[0]['issue_url'] = comments[0]['issue_url'].replace(target.repository, target.repository.upper())
    original = api.object
    def repository_case(endpoint):
        row = original(endpoint)
        if endpoint != 'user':
            for side in ('head', 'base'):
                row[side]['repo']['full_name'] = target.repository.upper()
        return row
    api.object = repository_case
    remote = RemoteEffects(tmp_path, target.repository, runner_identity={'source': 'host'}, api=api)
    assert remote.write_status_comment(target, marker, intent, WriteAuthority(lambda: lambda: None), lambda: False)['disposition'] == 'target'
    for invalid in ('foreign/repository', 'example/other', None, 17):
        def foreign(endpoint):
            row = repository_case(endpoint)
            if endpoint != 'user':
                row['base']['repo']['full_name'] = invalid
            return row
        with monkeypatch.context() as patch:
            patch.setattr(api, 'object', foreign)
            assert remote.read_status_comment(target, marker, intent, lambda: False)['disposition'] == 'third'
    assert [command[1] for command in api.commands] == ['PATCH']


@pytest.mark.parametrize('run_id', ['plain', "a'  b", 'a`b`c', 'a```b', '123', 'a\nb'])
def test_status_markdown_preserves_exact_action(tmp_path: Path, confirmed_display_receipt, run_id):
    import subprocess
    import sys
    from markdown_it import MarkdownIt
    from agent_run.cli.main import _parser
    target = replace(confirmed_display_receipt[0].target, run_id=run_id)
    action = action_guidance('resume', run_id=run_id, repository=target.repository)
    body = render_status_body(target, 'parent_only', ['Delivery: complete'], [action])
    displayed = [token.content.rstrip('\n') for token in MarkdownIt().parse(body) if token.type == 'fence']
    assert displayed == [action]
    if run_id in ('123', 'a\nb'):
        assert not action.startswith('agent-run ') and json.dumps(run_id) in action
        return
    executable = tmp_path / 'agent-run'
    executable.write_text(f'#!{sys.executable} -I\nimport json,sys\nprint(json.dumps(sys.argv[1:]))\n')
    executable.chmod(0o700)
    result = subprocess.run(['/bin/sh', '-c', displayed[0]], cwd=tmp_path,
        env={'PATH': str(tmp_path), 'HOME': str(tmp_path)}, text=True, capture_output=True, timeout=5)
    assert result.returncode == 0, result.stderr
    argv = json.loads(result.stdout)
    assert argv == ['resume', '--repo', target.repository, '--', run_id]
    selected = _parser('en').parse_args(argv)
    assert selected.run_id == run_id and selected.repo == target.repository


@pytest.mark.parametrize('changed', ['issue', 'marker', 'actor', 'comment_id', 'endpoint', 'receipt', 'run_binding', 'extra'])
def test_parent_only_comment_accounting_rejects_foreign_display_facts(confirmed_display_receipt, changed):
    from support.notification import assert_confirmed_status_comment
    projection, journal, comments, writes = confirmed_display_receipt
    if changed == 'issue':
        comments[0]['issue_url'] = 'https://api.github.com/repos/example/project/issues/999'
    elif changed == 'marker':
        foreign = replace(projection, target=replace(projection.target, run_id='foreign-run'))
        foreign_entry = StatusComments._entry({'pending': [], 'records': [], 'seen': []}, foreign)
        comments[0]['body'] = foreign_entry['marker'] + '\n' + foreign.body
    elif changed == 'actor':
        comments[0]['user']['login'] = 'foreign-actor'
    elif changed == 'comment_id':
        comments[0]['id'] = 78
    elif changed == 'endpoint':
        writes[0][1] = 'repos/example/project/issues/999/comments'
    elif changed == 'receipt':
        journal['remote_status']['bindings'][projection.target.key]['receipt']['comment_id'] = 78
    elif changed == 'run_binding':
        journal['native_binding'][0] = 'foreign-run'
    else:
        comments.append(deepcopy(comments[0]))
    before = deepcopy(journal)
    with pytest.raises((AssertionError, ValueError)):
        assert_confirmed_status_comment(projection, journal, comments, writes)
    assert journal == before
