"""Add committed Issue timestamps to the original managed fake-GitHub transport."""
import json
from pathlib import Path


def assert_completed_pr_preserved(before, after, original_writes, current_writes, projection, journal):
    """Keep every PR business field; account for activity through an exact Status receipt."""
    from collections import Counter
    from copy import deepcopy
    from datetime import datetime
    from agent_run.notifications.status_comment import StatusComments
    from support.notification import assert_confirmed_status_comment

    target = projection.target
    original, = [pr for pr in before['prs'] if pr['number'] == target.pr_number]
    current, = [pr for pr in after['prs'] if pr['number'] == target.pr_number]
    assert original['merged'] and original['state'] == 'closed'
    assert {key: value for key, value in current.items() if key != 'updated_at'} == {
        key: value for key, value in original.items() if key != 'updated_at'}
    entry = StatusComments._entry(deepcopy(journal), projection)
    comment, = [row for row in after['comments'] if row['id'] == entry['comment_id']]
    post = f'repos/{target.repository}/issues/{target.pr_number}/comments'
    patch = f'repos/{target.repository}/issues/comments/{comment["id"]}'
    scoped = deepcopy(journal)
    scoped['remote_status']['bindings'] = {target.key: deepcopy(entry)}
    assert_confirmed_status_comment(projection, scoped, [comment],
        [row for row in current_writes if row[1].split('?', 1)[0] in {post, patch}])
    assert current_writes[:len(original_writes)] == original_writes
    old_status = before.get('status_writes', [])
    assert after.get('status_writes', [])[:len(old_status)] == old_status
    activity = [row for row in after.get('status_writes', [])[len(old_status):] if row['id'] == comment['id']]
    assert all(row['method'] in {'POST', 'PATCH'} and row['body'].startswith(entry['marker'] + '\n') for row in activity)
    expected = Counter((row['method'], post if row['method'] == 'POST' else patch) for row in activity)
    actual = Counter()
    url = f'https://api.github.com/repos/{target.repository}/issues/{target.pr_number}'
    forbidden = {f'repos/{target.repository}/{kind}/{target.pr_number}' for kind in ('issues', 'pulls')}
    forbidden.add(f'repos/{target.repository}/pulls/{target.pr_number}/merge')
    for row in current_writes[len(original_writes):]:
        endpoint, method = row[1].split('?', 1)[0], row[row.index('--method') + 1]
        assert endpoint not in forbidden  # Even an edit followed by restoration is forbidden.
        on_target = endpoint == post
        if endpoint.startswith(f'repos/{target.repository}/issues/comments/'):
            identifier = int(endpoint.rsplit('/', 1)[-1])
            on_target = any(item['id'] == identifier and item.get('issue_url') == url
                            for item in before['comments'] + after['comments'])
        if on_target:
            actual[(method, endpoint)] += 1
    assert actual == expected
    if activity:
        assert activity[-1]['body'] == comment['body']
        assert activity[-1]['updated_at'] == current['updated_at']
        assert datetime.fromisoformat(current['updated_at']) > datetime.fromisoformat(original['updated_at'])
    else:
        assert current['updated_at'] == original['updated_at']


def install_currentness(directory: Path, state: Path) -> None:
    """Call after _control_environment, before any delivery or GitHub operation.

    This models the external service, never Kernel records. Each accepted write
    commits its timestamp with its actual comment/event; GET uses those stored
    values. Lost acknowledgements therefore retain the committed timestamp too.
    """
    document = json.loads(state.read_text())
    assert document["prs"] == [] and document["comments"] == [] and document["events"] == []
    document["operation_clock"] = 0
    for issue in (document["source_graph"]["parent"], *document["source_graph"]["issues"].values()):
        issue["updated_at"] = "2026-10-21T10:00:00Z"
    state.write_text(json.dumps(document))
    executable = directory / "control-bin" / "gh"
    program = executable.read_text()

    def replace(original: str, changed: str) -> None:
        nonlocal program
        assert program.count(original) == 1, original
        program = program.replace(original, changed)

    replace("d=json.loads(read_text(state))", """d=json.loads(read_text(state))
from support.github_comments import service_time
def tick():
    return service_time(d)""")
    replace("'merge_commit_sha':None,", "'merge_commit_sha':None,'updated_at':tick(),")
    replace("'created_at':'2026-10-21T10:01:00Z'", "'created_at':tick()")
    replace(";d.setdefault('events_by_issue',{}).setdefault(str(number),[]).append(d['events'][-1])",
            ";d.setdefault('events_by_issue',{}).setdefault(str(number),[]).append(d['events'][-1]);pr['updated_at']=d['events'][-1]['created_at']")
    replace("at='2026-10-21T10:01:00Z' if event=='closed' else '2026-10-21T10:02:00Z'", "at=tick()")
    replace("d['events'].append(observed);events.append(observed);result={'state':body['state']}",
            "d['events'].append(observed);events.append(observed);resource['updated_at']=at;result={'state':body['state']}")
    replace("elif method=='PATCH':\n        resource['state']=body['state'].upper()", """elif method=='PATCH':
        if 'state' not in body:
            assert body and set(body) <= {'title','body'}
            resource.update(body)
            resource['updated_at']=tick()
            write_text(state,json.dumps(d))
            print(json.dumps(resource))
            raise SystemExit(0)
        resource['state']=body['state'].upper()""")
    replace("'state':resource.get('state','OPEN').lower()}",
            "'state':resource.get('state','OPEN').lower(),'updated_at':resource['updated_at']}")
    executable.write_text(program)
