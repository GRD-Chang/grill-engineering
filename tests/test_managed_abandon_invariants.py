"""Actual local service mutations; display-journal input is controlled, not a native Host proof."""
from copy import deepcopy
from dataclasses import replace
from agent_run.adapters.status_comment import StatusCommentTransport
import json
import subprocess

import pytest

from agent_run.adapters.git_objects import LocalGit
from agent_run.adapters.operations import NativeOperations
from agent_run.contracts.status_comment import StatusProjection, StatusTarget
from agent_run.contracts.write_authority import WriteAuthority
from agent_run.model import Effect, Execution
from agent_run.notifications.status_comment import StatusComments
from support.managed_abandon_transport import assert_completed_pr_preserved, install_currentness
from support.systemd_manager import _CODEX, _control_environment, _git


@pytest.fixture
def completed_pr_activity(git_repo, tmp_path, monkeypatch):
    environment, state = _control_environment(git_repo, tmp_path, monkeypatch, codex=_CODEX, tickets=(2,))
    install_currentness(tmp_path, state)
    git, remote = LocalGit(git_repo), tmp_path / 'origin.git'
    base = git.commit('HEAD')
    _git(git_repo, 'push', str(remote), 'HEAD:refs/heads/run')
    _git(git_repo, 'checkout', '-b', 'candidate')
    _git(git_repo, 'commit', '--allow-empty', '-m', 'Actual completed Ticket')
    _git(git_repo, 'push', str(remote), 'HEAD:refs/heads/candidate')
    candidate = git.commit('HEAD')
    port = NativeOperations(git_repo, 'example/project', runner_identity={'source': 'host'}, remote=str(remote))
    publication = Effect('publish-ticket', 'ticket:2', 'pr_write', 'example/project', 'refs/heads/candidate',
        'refs/heads/run', candidate.sha, candidate.sha, None, base, None, 'publisher',
        {'title': 'Original', 'body': 'Original', 'prior': None}, Execution('run-1', 'worker', 1))
    created = port.execute(publication, WriteAuthority(lambda: lambda: None))
    assert created.disposition == 'target' and created.pr is not None
    merge = replace(publication, effect_id='merge-ticket', operation='merge', before_sha=base.sha,
        pr_number=created.pr.number, payload={'method': 'squash', 'parents': [base.sha],
                                            'commit_message': created.pr.title + '\n\n' + created.pr.body})
    assert port.execute(merge, WriteAuthority(lambda: lambda: None)).disposition == 'target'
    target = StatusTarget('example/project', 'run-1', 1, 'ticket:2', 1, 2, created.pr.number, 'publisher',
                          publication.effect_id, 'controlled-publication-reference', publication.source_ref, publication.target_ref)
    projection = StatusProjection(target, 'Ticket complete')
    journal = {'pending': [], 'records': [], 'seen': []}
    entry = StatusComments._entry(journal, projection)

    status = StatusCommentTransport(port.root, port.repository, runner_identity=port.identity)

    def display(current):
        intent = StatusComments._stage_entry(entry, current)
        assert intent is not None
        observed = status.write_status_comment(target, entry['marker'], intent,
                                             WriteAuthority(lambda: lambda: None), lambda: False)
        assert observed['disposition'] == 'target'
        # Record only the actual adapter result; this controlled display input
        # does not create a Core publication, Action, completion or Host receipt.
        entry.update(comment_id=observed['comment_id'], confirmed_body=intent['body'], intent=None,
            receipt={'intent': intent['id'], 'comment_id': observed['comment_id'],
                     'write_result': {'intent': intent['id'], 'returncode': observed['write_returncode']}})

    def document():
        return json.loads(state.read_text())

    def writes():
        return [row for row in map(json.loads, (tmp_path / 'gh-calls.jsonl').read_text().splitlines()) if '--method' in row]

    display(projection)
    before, original_writes = document(), writes()
    projection = replace(projection, body='Run abandoned; Host exited')
    display(projection)

    def edit(body):
        payload = tmp_path / 'business-edit.json'
        payload.write_text(json.dumps({'body': body}))
        result = subprocess.run([str(tmp_path / 'control-bin/gh'), 'api',
            f'repos/example/project/pulls/{target.pr_number}', '--method', 'PATCH', '--input', str(payload)],
            cwd=git_repo, env=environment, text=True, capture_output=True, timeout=5)
        assert result.returncode == 0, result.stderr

    return before, document, original_writes, writes, projection, journal, port, publication, edit


def test_completed_pr_preserves_business_facts_with_exact_status_activity(completed_pr_activity):
    before, document, original, writes, projection, journal, port, publication, _ = completed_pr_activity
    old, current = before['prs'][0], document()['prs'][0]
    assert {key for key in old.keys() | current.keys() if old.get(key) != current.get(key)} == {'updated_at'}
    assert_completed_pr_preserved(before, document(), original, writes(), projection, journal)
    count = len(writes())
    forbidden = replace(publication, pr_number=projection.target.pr_number,
        payload={'title': 'Unexpected', 'body': 'Unexpected', 'prior': {'title': 'Original', 'body': 'Original'}})
    assert port.execute(forbidden, WriteAuthority(lambda: lambda: pytest.fail('Merged PR edit cannot write'))).disposition == 'third'
    assert len(writes()) == count


def test_completed_pr_invariant_rejects_business_drift_and_unproven_activity(completed_pr_activity):
    before, document, original, writes, projection, journal, _, _, edit = completed_pr_activity
    after, current = document(), writes()
    for field, value in [('body', 'Unexpected'), ('title', 'Unexpected'), ('state', 'open'), ('merged', False),
                         ('merge_commit_sha', 'f' * 40), ('head', {}), ('base', {}), ('user', {'login': 'foreign'}),
                         ('extra_field', 'Unaccounted change')]:
        changed = deepcopy(after)
        changed['prs'][0][field] = value
        with pytest.raises(AssertionError):
            assert_completed_pr_preserved(before, changed, original, current, projection, journal)
    no_activity = deepcopy(after)
    no_activity['status_writes'] = deepcopy(before['status_writes'])
    with pytest.raises(AssertionError):
        assert_completed_pr_preserved(before, no_activity, original, current, projection, journal)
    unexplained_time = deepcopy(after)
    unexplained_time['prs'][0]['updated_at'] = '2026-10-21T11:00:00Z'
    with pytest.raises(AssertionError):
        assert_completed_pr_preserved(before, unexplained_time, original, current, projection, journal)
    for field, value in [('body', 'Foreign body'), ('user', {'login': 'foreign'})]:
        changed = deepcopy(after)
        changed['comments'][0][field] = value
        with pytest.raises(AssertionError):
            assert_completed_pr_preserved(before, changed, original, current, projection, journal)
    wrong_receipt = deepcopy(journal)
    wrong_receipt['remote_status']['bindings'][projection.target.key]['receipt']['comment_id'] += 1
    with pytest.raises(AssertionError):
        assert_completed_pr_preserved(before, after, original, current, projection, wrong_receipt)
    edit('Unexpected physical mutation')
    with pytest.raises(AssertionError):
        assert_completed_pr_preserved(before, document(), original, writes(), projection, journal)
    edit('Original')
    assert {key: value for key, value in document()['prs'][0].items() if key != 'updated_at'} == {
        key: value for key, value in after['prs'][0].items() if key != 'updated_at'}
    with pytest.raises(AssertionError):
        assert_completed_pr_preserved(before, document(), original, writes(), projection, journal)
