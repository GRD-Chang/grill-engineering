"""String variable encoding and exhausted observation budget regressions."""
import json

import pytest

from agent_run.adapters.source import read_requirements
from agent_run.errors import GitHubReadError
from support.github_retries import set_read_retry_sleep
from support.remote_effects import _reader
from test_source_snapshot_observation import _flat


@pytest.mark.parametrize('owner,name', [('Example', 'True'), ('Example', 'False'), ('Example', 'Null'),
                                      ('Example', '123'), ('True', 'Project'), (None, 'true')])
def test_repository_string_variables_survive_canonical_pages(tmp_path, monkeypatch, owner, name):
    reader, log = _reader(tmp_path, monkeypatch)
    canonical = f'{owner or "Example"}/{name.lower()}'
    state = tmp_path / 'responses.json'
    state.write_text(state.read_text().replace('example/project', canonical))
    reader.repository_override = f'{owner}/{name}' if owner is not None else None
    monkeypatch.setenv('GH_REPO', canonical if owner is None else 'Unrelated/Context')
    program = tmp_path / 'bin/gh'
    marker = "    if 'CurrentRequiredChecks' in fields['query']:"
    codec = '''    with open(os.environ['SOURCE_TEST_RESPONSES']) as f: selection=json.load(f)
    context=os.environ.get('GH_REPO',selection['repository']).split('/')
    for index,arg in enumerate(args):
        if index and args[index-1] in ('-f','-F') and '=' in arg:
            key,value=arg.split('=',1)
            if key not in {'owner','name'}: continue
            if args[index-1]=='-F':
                if value in {'{owner}','{repo}'}: value=context[0 if key=='owner' else 1]
                elif value in {'true','false','null'}: value=json.loads(value)
                elif value.lstrip('-').isdigit(): value=int(value)
            fields[key]=value
    if any(not isinstance(fields.get(key),str) for key in ('owner','name')):
        print('GraphQL String variable has a non-string value',file=sys.stderr);raise SystemExit(1)
    if (fields['owner']+'/'+fields['name']).lower()!=selection['repository'].lower():
        print('HTTP 404 selected repository differs',file=sys.stderr);raise SystemExit(1)
'''
    text = program.read_text()
    assert marker in text
    program.write_text(text.replace(marker, codec + marker, 1))
    from agent_run.adapters import github_commands
    set_read_retry_sleep(monkeypatch, lambda _: None)
    bounded, arguments = github_commands._run_bounded_command, []
    def request(command, **options):
        arguments.append(command)
        result = bounded(command, **options)
        if owner is None:
            monkeypatch.setenv('GH_REPO', 'Different/Context')
        return result
    monkeypatch.setattr(github_commands, '_run_bounded_command', request)
    observed = read_requirements(reader, 1)
    assert observed.parent.sub_issue_numbers == (9, 3)
    assert len(arguments) == len(log.read_text().splitlines()) == 3
    fields = [dict(arg.split('=',1) for i,arg in enumerate(command) if i and command[i-1] in ('-f','-F'))
              for command in arguments]
    assert fields[-1]['owner'] == (owner or 'Example') and fields[-1]['name'] == name.lower()
    if owner is None:
        assert (fields[0]['owner'], fields[0]['name']) == ('{owner}', '{repo}')
        assert (fields[1]['owner'], fields[1]['name']) == ('Example', 'true')


@pytest.mark.parametrize('where,requests', [('parent', 2), ('ticket', 3)])
def test_actual_compact_packet_overflow_remains_a_capacity_contradiction(tmp_path, monkeypatch, where, requests):
    reader, state, log = _flat(tmp_path, monkeypatch, 1)
    data = json.loads(state.read_text())
    data['parent' if where == 'parent' else '9']['body'] = 'L' * (1024 * 1024 + 1)
    state.write_text(json.dumps(data))
    with pytest.raises(GitHubReadError) as caught:
        read_requirements(reader, 1)
    assert caught.value.code == 'github_invalid_response'
    assert len(log.read_text().splitlines()) == requests
