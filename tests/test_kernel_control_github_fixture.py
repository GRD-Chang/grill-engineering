"""The shared managed-control transport keeps each Issue's actual REST facts."""
import json
from pathlib import Path
import subprocess

from support.systemd_manager import _CODEX, _control_environment


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
