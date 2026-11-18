"""Requirements select fresh source identity; delivery getters still freeze exact refs."""
import json

import pytest

from agent_run.adapters.source import read_required_check_facts, read_requirements
from agent_run.errors import GitHubReadError
from support.github_retries import set_read_retry_sleep
from support.remote_effects import _reader
from support.fixture_text import replace_once


def test_requirements_do_not_need_an_unconsumed_missing_default_ref(tmp_path, monkeypatch):
    reader, log = _reader(tmp_path, monkeypatch)
    path = tmp_path / 'responses.json'
    data = json.loads(path.read_text())
    data['refs'] = {}
    path.write_text(json.dumps(data))
    source = read_requirements(reader, 1)
    assert source.parent.sub_issue_numbers == (9, 3)
    commands = [json.loads(line) for line in log.read_text().splitlines()]
    assert commands[0][:2] == ['api', 'graphql'] and 'SourceSnapshot' in commands[0][3]
    assert not any('/git/ref/heads/' in str(command) for command in commands)


def _metadata_reader(root, monkeypatch):
    reader, log = _reader(root, monkeypatch)
    script = root / 'bin' / 'gh'
    marker = "if args[:2]==['repo','view'] or (args[:1]==['api'] and '/git/ref/heads/' in args[1]):"
    program = replace_once(script.read_text(), marker, '''if args[:2]==['repo','view']:
    with open(os.environ['SOURCE_TEST_RESPONSES']) as f: data=json.load(f)
    if data.get('metadata_http'):
        print('HTTP '+str(data['metadata_http'])+' metadata unavailable',file=sys.stderr)
        raise SystemExit(1)
    result={'nameWithOwner':data.get('metadata_repository',data['repository'])}
    if 'defaultBranchRef' in args[-1].split(','):
        result['defaultBranchRef']={'name':data['default_branch']}
    print(json.dumps(result))
    raise SystemExit(0)
''' + marker)
    script.write_text(replace_once(program, "print(result.stdout,end='');print(result.stderr,end='',file=sys.stderr)", '''if result.returncode==0 and data.get('ref_type'):
        value=json.loads(result.stdout); value['object']['type']=data['ref_type']; result.stdout=json.dumps(value)
    print(result.stdout,end='');print(result.stderr,end='',file=sys.stderr)'''))
    set_read_retry_sleep(monkeypatch, lambda _: None)  # Keep all three real failed subprocess attempts.
    return reader, log


def _damage_default_ref(data, damage):
    if damage == 'missing':
        data['refs'] = {}
    elif damage == 'noncommit':
        data['ref_type'] = 'tag'
    else:
        data['ref_errors'] = {'repos/example/project/git/ref/heads/main': 'HTTP 403 Contents denied'}


@pytest.mark.parametrize('damage', ['missing', 'noncommit', 'contents-403'])
def test_requirements_identity_is_independent_of_delivery_default_ref_permissions(tmp_path, monkeypatch, damage):
    reader, log = _metadata_reader(tmp_path, monkeypatch)
    path = tmp_path / 'responses.json'
    data = json.loads(path.read_text())
    _damage_default_ref(data, damage)
    path.write_text(json.dumps(data))
    source = read_requirements(reader, 1)
    commands = [json.loads(line) for line in log.read_text().splitlines()]
    assert source.parent.sub_issue_numbers == (9, 3)
    assert source.parent.body == 'Original Parent body\r\n保留原文'
    assert commands[0][:2] == ['api', 'graphql'] and 'SourceSnapshot' in commands[0][3]
    assert len(commands) == 3  # Two nested Parent pages and one Ticket continuation.
    assert not any('/git/ref/heads/' in str(command) for command in commands)
    for read in (reader.repository, lambda: read_required_check_facts(reader, 17, expected_head_sha='2' * 40)):
        before = len(log.read_text().splitlines())
        with pytest.raises(GitHubReadError) as caught:
            read()
        assert caught.value.code == ('github_invalid_response' if damage == 'noncommit' else 'github_read_failed')
        later = [json.loads(line) for line in log.read_text().splitlines()][before:]
        assert later[0][-1] == 'nameWithOwner,defaultBranchRef'
        assert later[1:] == [['api', 'repos/example/project/git/ref/heads/main']] * (1 if damage == 'noncommit' else 3)


@pytest.mark.parametrize('damage', ['missing', 'noncommit', 'contents-403'])
def test_initial_creation_still_requires_the_exact_default_commit(git_repo, tmp_path, monkeypatch, damage):
    from agent_run.adapters.workspace import ManagedWorkspace
    from agent_run.runtime.task_creation import prepare_creation
    from agent_run.config.defaults import UserDefaultsStore
    from support.workspace import git

    remote = tmp_path / 'origin.git'
    git(git_repo, 'init', '--bare', '--initial-branch=main', str(remote))
    git(git_repo, 'push', str(remote), 'HEAD:refs/heads/main')
    _, log = _metadata_reader(tmp_path, monkeypatch)
    path = tmp_path / 'responses.json'
    data = json.loads(path.read_text())
    _damage_default_ref(data, damage)
    path.write_text(json.dumps(data))
    defaults = UserDefaultsStore()
    defaults.configure(language='en')
    with pytest.raises(GitHubReadError):
        prepare_creation(1, 'example/project', source_directory=git_repo, defaults=defaults, remote_url=str(remote))
    workspace = ManagedWorkspace.for_repository('example/project')
    assert not list(workspace.state_root.rglob('*.sqlite3'))
    assert git(workspace.repository_root, 'for-each-ref', '--format=%(refname)', 'refs/heads/agent-run') == ''
    commands = [json.loads(line) for line in log.read_text().splitlines()]
    assert [c[-1] for c in commands if c[:2] == ['repo', 'view']] == ['nameWithOwner,defaultBranchRef']
    assert any('/git/ref/heads/main' in str(command) for command in commands)


@pytest.mark.parametrize('read_kind', ['requirements', 'repository'])
@pytest.mark.parametrize('reported', ['outside/project', 'example/project/extra', None])
def test_repository_metadata_rejects_wrong_selected_identity_before_other_reads(tmp_path, monkeypatch, read_kind, reported):
    reader, log = _metadata_reader(tmp_path, monkeypatch)
    path = tmp_path / 'responses.json'
    data = json.loads(path.read_text()); data['metadata_repository'] = reported
    path.write_text(json.dumps(data))
    with pytest.raises(GitHubReadError) as caught:
        read_requirements(reader, 1) if read_kind == 'requirements' else reader.repository()
    assert caught.value.code == 'github_invalid_response'
    assert len(log.read_text().splitlines()) == 1


def test_metadata_permission_failure_is_not_an_empty_graph_or_another_layout(tmp_path, monkeypatch):
    reader, log = _metadata_reader(tmp_path, monkeypatch)
    path = tmp_path / 'responses.json'
    data = json.loads(path.read_text()); data['metadata_http'] = 403
    path.write_text(json.dumps(data))
    with pytest.raises(GitHubReadError) as caught:
        read_requirements(reader, 1)
    assert caught.value.code == 'github_read_failed' and 'HTTP 403' in caught.value.message
    commands = [json.loads(line) for line in log.read_text().splitlines()]
    assert len(commands) == 3 and all(command[:2] == ['api', 'graphql'] and 'SourceSnapshot' in command[3]
                                      for command in commands)


def test_each_read_is_fresh_and_canonical_case_does_not_change_ref_or_sha(tmp_path, monkeypatch):
    reader, log = _metadata_reader(tmp_path, monkeypatch)
    reader.repository_override = 'EXAMPLE/PROJECT'
    path = tmp_path / 'responses.json'
    data = json.loads(path.read_text().replace('example/project', 'Example/Project'))
    data['default_branch'] = 'Feature/Case'
    data['refs'] = {'refs/heads/Feature/Case': 'b' * 40}
    path.write_text(json.dumps(data))
    first = read_requirements(reader, 1)
    full = reader.repository()
    assert (full.name_with_owner, full.default_branch, full.default_head_sha) == ('Example/Project', 'Feature/Case', 'b' * 40)
    for key in ('parent', 'parent-next'):
        data[key]['body'] = 'Fresh Parent\r\n新需求'
    data['refs'] = {'refs/heads/Feature/Case': 'c' * 40}
    path.write_text(json.dumps(data))
    second = read_requirements(reader, 1)
    assert first.parent.body == 'Original Parent body\r\n保留原文'
    assert second.parent.body == 'Fresh Parent\r\n新需求'
    assert reader.repository().default_head_sha == 'c' * 40
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    metadata = [call for call in calls if call[:2] == ['repo', 'view']]
    assert [call[-1] for call in metadata] == ['nameWithOwner,defaultBranchRef'] * 2
    assert all(call[2] == 'EXAMPLE/PROJECT' for call in metadata)
    refs = [call for call in calls if '/git/ref/heads/' in str(call)]
    assert refs == [['api', 'repos/Example/Project/git/ref/heads/Feature%2FCase']] * 2
