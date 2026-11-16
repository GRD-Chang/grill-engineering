"""Synthetic API persistence, independent of already-tested Core delivery journeys."""
import json
from pathlib import Path

from support.offline import OfflinePort
from support.offline_api import ExternalObjects, SyntheticGitHub


def test_synthetic_api_persists_pr_retirement_and_issue_reopening(tmp_path: Path) -> None:
    port = OfflinePort.create(tmp_path / 'api')
    repository = 'example/repository'
    port.objects.bind(repository, {2: 'open'})
    port.git.run('update-ref', 'refs/heads/change', port.git.commit('HEAD').sha)
    head = port.external_append('refs/heads/change', {'change.txt': 'Original work\n'}, 'api-change')
    assert port.physical.push('refs/heads/change', None, head.sha).returncode == 0
    api = SyntheticGitHub(port.git, port.database, repository,
        physical=port.physical, written=lambda *args: None, read_faults=set())
    response = api.write(f'repos/{repository}/pulls', 'POST', {
        'head': 'change', 'base': 'main', 'title': 'Original change', 'body': 'Reviewed work'})
    assert response.returncode == 0
    number = json.loads(response.stdout)['number']
    assert api.write(f'repos/{repository}/pulls/{number}', 'PATCH', {'state': 'closed'}).returncode == 0
    # Reopen the actual storage facade; do not rely on the transport's objects.
    objects = ExternalObjects(port.database)
    pr = objects.pr(number)
    assert pr is not None and pr['state'] == 'closed' and pr['merged'] is False
    events = objects.events(number)
    assert len(events) == 1 and events[0]['event'] == 'closed'
    assert events[0]['actor']['login'] == 'offline-publisher'
    assert pr['updated_at'] == events[0]['created_at']

    endpoint = f'repos/{repository}/issues/2'
    for state in ('closed', 'open'):
        assert api.write(endpoint, 'PATCH', {'state': state}).returncode == 0
    objects = ExternalObjects(port.database)
    events = objects.events(2)
    issue = api.object(endpoint)
    assert [event['event'] for event in events] == ['closed', 'reopened']
    assert issue['state'] == 'open'
    assert issue['updated_at'] == events[-1]['created_at']
    assert [event['actor']['login'] for event in events] == ['offline-publisher'] * 2
    before = objects.snapshot()
    assert api.object(f'repos/{repository}/pulls/{number}') == pr
    assert api.list(endpoint + '/events') == events
    assert api.object(endpoint) == issue
    assert ExternalObjects(port.database).snapshot() == before
