"""The shared managed-control transport keeps each Issue's actual REST facts."""
import json
from pathlib import Path
import subprocess

import pytest

from support.managed_abandon_transport import install_currentness
from support.systemd_manager import _CODEX, _control_environment, _git


def test_status_writes_change_check_diagnostics_without_changing_delivery_identity(git_repo, tmp_path, monkeypatch):
    from agent_run.adapters.git_objects import LocalGit
    from agent_run.adapters.remote import RemoteEffects
    from agent_run.contracts.write_authority import WriteAuthority
    from agent_run.contracts.required_checks import same_required_checks
    from agent_run.core.operation_contract import DeliveryRead
    from agent_run.model import Effect, Execution
    from dataclasses import asdict

    environment, state = _control_environment(git_repo, tmp_path, monkeypatch, codex=_CODEX, tickets=(2,))
    install_currentness(tmp_path, state)
    git = LocalGit(git_repo)
    base = git.commit('HEAD')
    _git(git_repo, 'checkout', '-b', 'candidate')
    _git(git_repo, 'commit', '--allow-empty', '-m', 'Actual candidate')
    _git(git_repo, 'push', str(tmp_path / 'origin.git'), 'HEAD:refs/heads/candidate')
    candidate = git.commit('HEAD')
    port = RemoteEffects(git_repo, 'example/project', runner_identity={'source': 'host'}, remote=str(tmp_path / 'origin.git'))
    execution = Execution('run-1', 'worker', 1)
    effect = Effect('create-pr', 'ticket:2', 'pr_write', 'example/project', 'refs/heads/candidate',
                    'refs/heads/main', candidate.sha, candidate.sha, None, base, None, 'publisher',
                    {'title': 'Original', 'body': 'Original', 'prior': None}, execution)
    created = port.execute(effect, WriteAuthority(lambda: lambda: None))
    assert created.disposition == 'target' and created.pr is not None
    query = DeliveryRead(effect.source_ref, effect.target_ref, created.pr.number, ())
    before = port.delivery(query)
    source = json.loads(state.read_text())['source_graph']
    for method, endpoint, body in [
        ('POST', f'issues/{created.pr.number}/comments', '<!-- agent-run-status:original -->\nBefore'),
        ('PATCH', 'issues/comments/1', '<!-- agent-run-status:original -->\nAfter'),
    ]:
        payload = tmp_path / 'status.json'
        payload.write_text(json.dumps({'body': body}))
        command = [str(tmp_path / 'control-bin/gh'), 'api', 'repos/example/project/' + endpoint,
                   '--method', method, '--input', str(payload)]
        result = subprocess.run(command, cwd=git_repo, env=environment, text=True, capture_output=True, timeout=5)
        assert result.returncode == 0, result.stderr
        after = port.delivery(query)
        assert (after.pr, after.source, after.target) == (before.pr, before.source, before.target)
        assert before.checks is not None and after.checks is not None
        assert (after.checks.pr_number, after.checks.head_sha, after.checks.result, after.checks.details) == (
            before.checks.pr_number, before.checks.head_sha, before.checks.result, before.checks.details)
        assert after.checks.logs != before.checks.logs
        assert same_required_checks(asdict(before.checks), after.checks)
        assert json.loads(state.read_text())['source_graph'] == source


def test_managed_control_github_keeps_parent_and_ticket_rest_facts_separate(
    git_repo: Path, tmp_path: Path, monkeypatch,
) -> None:
    environment, state = _control_environment(git_repo, tmp_path, monkeypatch, codex=_CODEX, tickets=(2,))

    def api(number, suffix="", *, body=None):
        command = [str(tmp_path / "control-bin/gh"), "api", f"repos/example/project/issues/{number}" + suffix]
        if body is not None:
            payload = tmp_path / "request.json"
            payload.write_text(json.dumps(body))
            command += ["--method", "POST" if suffix == "/comments" else "PATCH", "--input", str(payload)]
        else:
            command += ["--paginate", "--slurp"] if suffix else []
        result = subprocess.run(command, cwd=git_repo, env=environment, text=True, capture_output=True, timeout=5)
        assert result.returncode == 0, (result.stdout, result.stderr)
        return json.loads(result.stdout)

    assert api(2) == {"number": 2, "node_id": "issue-node-2", "state": "open"}
    assert api(1) == {"number": 1, "node_id": "issue-node-1", "state": "open"}
    parent = api(1, "/comments", body={"body": "Original Parent completion evidence"})
    ticket = api(2, "/comments", body={"body": "Original Ticket completion evidence"})
    assert parent["id"] != ticket["id"]
    assert api(1, "/comments") == [[parent]]
    assert api(2, "/comments") == [[ticket]]
    assert api(2, body={"state": "closed"}) == {"state": "closed"}
    assert api(2)["state"] == "closed" and api(1)["state"] == "open"
    assert api(1, "/events") == [[]]
    ticket_events = api(2, "/events")[0]
    assert len(ticket_events) == 1 and ticket_events[0]["event"] == "closed"
    assert api(1, body={"state": "closed"}) == {"state": "closed"}
    parent_events = api(1, "/events")[0]
    assert len(parent_events) == 1 and parent_events[0]["event"] == "closed"
    assert parent_events[0]["id"] != ticket_events[0]["id"]
    assert api(2, "/events")[0] == ticket_events
    assert api(2, body={"state": "open"}) == {"state": "open"}
    assert api(2)["state"] == "open" and api(1)["state"] == "closed"
    assert [event["event"] for event in api(2, "/events")[0]] == ["closed", "reopened"]
    assert api(1, "/events")[0] == parent_events
    document = json.loads(state.read_text())
    assert document["source_graph"]["parent"]["state"] == "CLOSED"
    assert document["source_graph"]["issues"]["2"]["state"] == "OPEN"


@pytest.mark.parametrize('deleted', [False, True], ids=['actual-comment', 'stale-index'])
def test_abandon_comment_index_matches_actual_rest_receipts(git_repo, tmp_path, monkeypatch, deleted):
    environment, state = _control_environment(git_repo, tmp_path, monkeypatch, codex=_CODEX, tickets=(2,))
    install_currentness(tmp_path, state)
    _git(git_repo, 'checkout', '-b', 'candidate')
    _git(git_repo, 'commit', '--allow-empty', '-m', 'Candidate for retirement')
    _git(git_repo, 'push', str(tmp_path / 'origin.git'), 'HEAD:refs/heads/candidate')

    def api(endpoint, *, method='GET', body=None):
        command = [str(tmp_path / 'control-bin' / 'gh'), 'api', 'repos/example/project/' + endpoint]
        if method != 'GET':
            command += ['--method', method]
        if body is not None:
            payload = tmp_path / 'request.json'
            payload.write_text(json.dumps(body))
            command += ['--input', str(payload)]
        elif endpoint.endswith('/comments'):
            command += ['--paginate', '--slurp']
        result = subprocess.run(command, cwd=git_repo, env=environment, text=True, capture_output=True, timeout=5)
        assert result.returncode == 0, (result.stdout, result.stderr)
        return json.loads(result.stdout)

    pr = api('pulls', method='POST', body={'head': 'candidate', 'base': 'main', 'title': 'Original', 'body': 'Original'})
    number = pr['number']
    api(f'issues/{number}')  # The original per-Issue diagnostic view exists.
    marker = '<!-- agent-run-retirement:original-effect -->'
    comment = api(f'issues/{number}/comments', method='POST', body={'body': 'Retiring original Run.\n\n' + marker})
    api(f'pulls/{number}', method='PATCH', body={'state': 'closed'})
    # Actual external POST and readback, not a Core status or injected receipt.
    assert api(f'issues/{number}/comments') == [[comment]]
    assert comment['user']['login'] == 'publisher' and marker in comment['body']
    recovery = api('issues/2/comments', method='POST', body={'body': '<!-- agent-run-kernel:recovery-effect -->\nRecovery'})
    assert api('issues/2/comments') == [[recovery]]
    if deleted:
        api(f'issues/comments/{comment["id"]}', method='DELETE')
        removed = json.loads(state.read_text())
        assert comment not in removed['comments']
        assert removed['comments_by_issue'][str(number)] == []
        # Negative fixture input: inject a stale derived index after real DELETE.
        # A mirror must never manufacture a real comment.
        document = json.loads(state.read_text())
        document['comments'] = [row for row in document['comments'] if row['id'] != comment['id']]
        document['comments_by_issue'][str(number)] = [comment]
        state.write_text(json.dumps(document))
    actual = api(f'issues/{number}/comments')[0]
    assert actual == ([] if deleted else [comment])
    document = json.loads(state.read_text())
    assert document['comments_by_issue'][str(number)] == actual
    assert document['comments_by_issue']['2'] == [recovery]


@pytest.mark.parametrize('currentness', [False, True])
def test_comment_handler_commits_canonical_rows_and_mirrors_without_read_ticks(
    git_repo, tmp_path, monkeypatch, currentness,
):
    environment, state = _control_environment(git_repo, tmp_path, monkeypatch, codex=_CODEX, tickets=(2,))
    if currentness:
        install_currentness(tmp_path, state)

    def api(endpoint, *, method='GET', body=None):
        command = [str(tmp_path / 'control-bin/gh'), 'api', 'repos/example/project/' + endpoint,
                   '--method', method]
        if body is not None:
            payload = tmp_path / 'request.json'
            payload.write_text(json.dumps(body))
            command += ['--input', str(payload)]
        result = subprocess.run(command, cwd=git_repo, env=environment, text=True, capture_output=True, timeout=5)
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    original = json.loads(state.read_text())
    parent = api('issues/1/comments', method='POST', body={'body': 'Parent evidence'})
    after_parent = json.loads(state.read_text())
    ticket = api('issues/2/comments', method='POST', body={'body': 'Ticket evidence'})
    written = json.loads(state.read_text())
    assert written['comments'] == [parent, ticket]
    assert written['comments_by_issue'] == {'1': [parent], '2': [ticket]}
    assert after_parent['source_graph']['issues']['2'] == original['source_graph']['issues']['2']
    assert written['source_graph']['parent'] == after_parent['source_graph']['parent']
    if currentness:
        assert written['operation_clock'] == 2
        assert written['source_graph']['parent']['updated_at'] == parent['created_at']
        assert written['source_graph']['issues']['2']['updated_at'] == ticket['created_at']
    assert api('issues/1/comments') == [parent] and api('issues/2/comments') == [ticket]
    assert json.loads(state.read_text()) == written

    pr = api('pulls', method='POST', body={'head': 'main', 'base': 'main', 'title': 'Status', 'body': 'Original'})
    number = pr['number']
    status = api(f'issues/{number}/comments', method='POST', body={'body': '<!-- agent-run-status:original -->\nBefore'})
    before_patch = json.loads(state.read_text())
    changed = api(f'issues/comments/{status["id"]}', method='PATCH', body={'body': '<!-- agent-run-status:original -->\nAfter'})
    patched = json.loads(state.read_text())
    assert changed == {**status, 'body': '<!-- agent-run-status:original -->\nAfter'}
    assert patched['comments'][-1] == changed == patched['comments_by_issue'][str(number)][0]
    assert patched['source_graph'] == before_patch['source_graph']
    assert patched['status_writes'][-1] == {'method': 'PATCH', 'id': status['id'], 'body': changed['body']}
    if currentness:
        assert patched['operation_clock'] == before_patch['operation_clock'] + 1
        assert patched['prs'][0]['updated_at'] > status['created_at']
    assert api(f'issues/{number}/comments') == [changed]
    assert json.loads(state.read_text()) == patched
    api(f'issues/comments/{status["id"]}', method='DELETE')
    deleted = json.loads(state.read_text())
    assert deleted['comments'] == [parent, ticket]
    assert deleted['comments_by_issue'][str(number)] == []
    assert deleted['source_graph'] == patched['source_graph']
    assert api(f'issues/{number}/comments') == []
    assert json.loads(state.read_text()) == deleted
    replacement = api(f'issues/{number}/comments', method='POST', body={'body': 'New physical comment'})
    assert replacement['id'] > status['id']  # Deleted receipt IDs cannot name a later write.
    foreign = {**ticket, 'id': 90, 'issue_url': 'https://api.github.com/repos/foreign/project/issues/2'}
    legacy = {**ticket, 'id': 91}
    del legacy['issue_url']
    document = json.loads(state.read_text())
    document['comments'] += [foreign, legacy]
    state.write_text(json.dumps(document))
    assert api('issues/2/comments') == [ticket, legacy]
    readback = json.loads(state.read_text())
    assert readback['comments_by_issue']['2'] == [ticket, legacy]
    assert readback['comments'] == document['comments']
    assert readback.get('operation_clock') == document.get('operation_clock')
    assert readback['comment_sequence'] == document['comment_sequence']


@pytest.mark.parametrize('sequence,preloaded_id', [(None, 1), (2, 17)], ids=['missing-sequence', 'behind-sequence'])
def test_comment_delete_preserves_preloaded_id_high_water_without_get_advancing_it(
    git_repo, tmp_path, monkeypatch, sequence, preloaded_id,
):
    environment, state = _control_environment(git_repo, tmp_path, monkeypatch, codex=_CODEX, tickets=(2,))
    install_currentness(tmp_path, state)
    document = json.loads(state.read_text())
    comment = {'id': preloaded_id, 'body': 'Preloaded canonical evidence', 'user': {'login': 'publisher'},
               'issue_url': 'https://api.github.com/repos/example/project/issues/2',
               'created_at': '2026-10-21T10:00:00Z'}
    document['comments'] = [comment]
    if sequence is not None:
        document['comment_sequence'] = sequence
    state.write_text(json.dumps(document))

    def api(endpoint, method, body=None):
        command = [str(tmp_path / 'control-bin/gh'), 'api', 'repos/example/project/' + endpoint,
                   '--method', method]
        if body is not None:
            payload = tmp_path / 'request.json'
            payload.write_text(json.dumps(body))
            command += ['--input', str(payload)]
        result = subprocess.run(command, cwd=git_repo, env=environment, text=True, capture_output=True, timeout=5)
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    assert api('issues/2/comments', 'GET') == [comment]
    readback = json.loads(state.read_text())
    assert readback.get('comment_sequence') == sequence and readback['operation_clock'] == 0
    assert ('comment_sequence' in readback) == (sequence is not None)
    api(f'issues/comments/{preloaded_id}', 'DELETE')
    deleted = json.loads(state.read_text())
    assert deleted['comments'] == [] and deleted['comments_by_issue']['2'] == []
    assert api('issues/2/comments', 'GET') == []
    assert json.loads(state.read_text()) == deleted
    replacement = api('issues/2/comments', 'POST', {'body': 'New physical evidence'})
    assert replacement['id'] == preloaded_id + 1
    assert deleted['comment_sequence'] == preloaded_id
