"""The offline observer preserves external evidence without replaying writes."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys

from agent_run.github import GhGitHubReader


def test_close_readback_keeps_numeric_identity_and_all_original_pages(
    tmp_path: Path, monkeypatch,
) -> None:
    binary = tmp_path / 'bin'
    binary.mkdir()
    responses = {
        'user': {'login': 'actual-publisher', 'id': 701},
        'issue': {'number': 2, 'state': 'closed', 'body': 'Original Issue\r\n保留原文'},
        'comments': [[{'id': 301, 'body': 'publisher-close-intent: exact\r\n原文',
                       'user': {'login': 'actual-publisher'}, 'created_at': '2026-10-21T00:00:01Z'}], []],
        'events': [[{'id': 4100, 'event': 'reopened', 'actor': {'login': 'someone-else'},
                     'created_at': '2026-10-21T00:00:00Z'}],
                   [{'id': 4101, 'event': 'closed', 'actor': {'login': 'actual-publisher'},
                     'created_at': '2026-10-21T00:00:02Z'}]],
    }
    (tmp_path / 'responses.json').write_text(json.dumps(responses, ensure_ascii=False))
    script = binary / 'gh'
    script.write_text('#!' + sys.executable + '\n' + '''import json,os,sys
from pathlib import Path
args=sys.argv[1:]
root=Path(os.environ['MIGRATION_READBACK_FIXTURE'])
with (root/'calls.jsonl').open('a') as out: out.write(json.dumps(args)+'\\n')
data=json.loads((root/'responses.json').read_text())
if args[:2]==['repo','view']:
    value={'nameWithOwner':'example/project','defaultBranchRef':{'name':'main'}}
elif args[:2]==['api','repos/example/project/commits/main']:
    value={'sha':'1'*40}
elif args[:2]==['api','user']:
    value=data['user']
elif args[:2]==['api','repos/example/project/issues/2']:
    value=data['issue']
elif args[:2]==['api','repos/example/project/issues/2/comments']:
    assert '--paginate' in args and '--slurp' in args
    value=data['comments']
elif args[:2]==['api','repos/example/project/issues/2/events']:
    assert '--paginate' in args and '--slurp' in args
    value=data['events']
else:
    raise SystemExit('Unexpected command '+repr(args))
print(json.dumps(value,ensure_ascii=False,indent=2))
''')
    script.chmod(0o755)
    monkeypatch.setenv('PATH', str(binary) + os.pathsep + os.environ['PATH'])
    monkeypatch.setenv('MIGRATION_READBACK_FIXTURE', str(tmp_path))
    for key in ('GH_TOKEN', 'GITHUB_TOKEN'):
        monkeypatch.delenv(key, raising=False)
    reader = GhGitHubReader('example/project', working_directory=tmp_path,
                            runner_identity={'source': 'host'})
    from agent_run_kernel.migration_readback import read_issue_close_facts
    result = read_issue_close_facts(reader, 2)
    assert result['repository'] == 'example/project' and result['issue_number'] == 2
    assert result['actor'] == responses['user']
    assert result['issue'] == responses['issue']
    assert result['comment_pages'] == responses['comments']
    assert result['event_pages'] == responses['events']
    event = result['event_pages'][1][0]
    assert type(event['id']) is int and event['id'] == 4101
    expected = [responses[key] for key in ('user', 'issue', 'comments', 'events')]
    assert [json.loads(entry['stdout']) for entry in result['responses']] == expected
    assert all(entry['stdout'].startswith('{\n') or entry['stdout'].startswith('[\n')
               for entry in result['responses'])  # Preserve response text, not reserialized values.
    calls = [json.loads(line) for line in (tmp_path / 'calls.jsonl').read_text().splitlines()]
    assert all(arguments[0] in {'api', 'repo'} for arguments in calls)
    assert all('--method' not in arguments and '-X' not in arguments for arguments in calls)
    assert not (tmp_path / 'kernel.sqlite3').exists()
