"""The shared managed-control transport keeps each Issue's actual REST facts."""
import json
from pathlib import Path
import subprocess

import pytest

from support.managed_abandon_transport import install_currentness
from support.systemd_manager import _CODEX, _control_environment, _git


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
        if body is not None:
            payload = tmp_path / 'request.json'
            payload.write_text(json.dumps(body))
            command += ['--method', method, '--input', str(payload)]
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
        # Negative fixture input: remove the actual service row but retain a
        # stale derived index. A mirror must never manufacture a real comment.
        document = json.loads(state.read_text())
        document['comments'] = [row for row in document['comments'] if row['id'] != comment['id']]
        document['comments_by_issue'][str(number)] = [comment]
        state.write_text(json.dumps(document))
    actual = api(f'issues/{number}/comments')[0]
    assert actual == ([] if deleted else [comment])
    document = json.loads(state.read_text())
    assert document['comments_by_issue'][str(number)] == actual
    assert document['comments_by_issue']['2'] == [recovery]
