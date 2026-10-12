"""公开 CLI 正常退出时保留真实投递、重试记录和只读查询边界。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent_run.notifications import read_notifications
from agent_run.user_defaults import UserDefaultsStore
from support.cli import load_only_run_state, stdout_json
from support.cli_fixtures import run_agents
from support.cli_payloads import parent_publication
from support.inprocess_cli import invoke_cli_inprocess
from support.seeding import write_fixture
from support.workspace import managed_state

from support.state import snapshot_runs


@pytest.mark.parametrize("state_read_error", [False, True], ids=["lost-receipts", "read-recovery"])
def test_public_run_finishes_important_retry_and_exposes_attempts_without_sending(
    git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state_read_error: bool,
) -> None:
    messages = tmp_path / "messages.jsonl"
    binary = tmp_path / "bin"
    binary.mkdir()
    tool = binary / "lark-cli"
    tool.write_text(f"#!{sys.executable}\n" + '''import json, os, sys
from pathlib import Path
args = sys.argv[1:]
if 'whoami' in args:
    print(json.dumps({'profile':'test','appId':'cli_test','identity':'bot','available':True}))
else:
    path = Path(''' + repr(str(messages)) + ''')
    key = args[args.index('--idempotency-key') + 1]
    card = json.loads(args[args.index('--content') + 1])
    title = card['header']['title']['content']
    prior = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
    attempt = 1 + sum(item['key'] == key for item in prior)
    with path.open('a') as stream:
        stream.write(json.dumps({'key': key, 'title': title, 'attempt': attempt,
                                 'processes': [os.getpid(), os.getppid()]}) + '\\n')
    # A lost ordinary receipt is not replayed; an important one is retried.
    if (title == 'Task created' and not os.environ.get('NOTIFICATION_TEST_STATE_READ_FAILURE')) or (title.startswith('Please approve merging PR') and attempt < 3):
        raise SystemExit(0)
    print(json.dumps({'data': {'message_id': 'om_test'}}))
''')
    tool.chmod(0o700)
    monkeypatch.setenv("PATH", str(binary) + os.pathsep + os.environ["PATH"])
    UserDefaultsStore().configure(language="en", notifications={
        "enabled": True, "profile": "test", "open_id": "ou_test", "app_id": "cli_test",
    })
    fixture = write_fixture(git_repo / "github.json", issues={})
    agents = run_agents(git_repo / "agents.json")
    data = json.loads(agents.read_text())
    data["publications"] = [parent_publication()]
    agents.write_text(json.dumps(data))
    # Preserve the real CLI/Executor, sender and transport. The fault case
    # raises once at its read callback after the first success was persisted.
    script = """import os, sys
from pathlib import Path
from agent_run import notification_outbox
from agent_run.notifications import Notifications
from agent_run.cli import main
notification_outbox.RETRY_DELAY_SECONDS = 0.01
if os.environ.get('NOTIFICATION_TEST_STATE_READ_FAILURE'):
    messages = Path(os.environ['NOTIFICATION_TEST_MESSAGES'])
    marker = messages.with_suffix('.fault')
    original_init = Notifications.__init__
    def initialize(self, root, state, *, load_state=None):
        def load():
            records = notification_outbox.read_notifications(root, state['run_id']).get('records', [])
            if any(item['kind'] == 'run_start' and item['outcome'] == 'success' for item in records) and not marker.exists():
                marker.write_text('temporary state read failure')
                raise OSError('temporary state read failure')
            return load_state()
        original_init(self, root, state, load_state=load if load_state else None)
    Notifications.__init__ = initialize
raise SystemExit(main(sys.argv[1:]))
"""
    if state_read_error:
        monkeypatch.setenv("NOTIFICATION_TEST_STATE_READ_FAILURE", "1")
        monkeypatch.setenv("NOTIFICATION_TEST_MESSAGES", str(messages))
    source = str(Path(__file__).resolve().parents[1] / "src")
    result = subprocess.run(
        [sys.executable, "-c", script, "run", "1", "--notification-mode", "concise",
         "--agent-fixture", str(agents), "--github-fixture", str(fixture), "--json"],
        cwd=git_repo, env={**os.environ, "PYTHONPATH": source}, capture_output=True,
        text=True, check=False, timeout=25,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert stdout_json(result)["status"] == "parent_approval_pending"
    if state_read_error:
        assert messages.with_suffix(".fault").read_text() == "temporary state read failure"
        assert "temporary state read failure" in result.stderr
    state = load_only_run_state(git_repo)
    root = managed_state(git_repo)
    journal = read_notifications(root, state["run_id"])
    assert not journal["pending"]
    assert not journal.get("unavailable_reason")
    important, = [record for record in journal["records"] if record["important"]]
    assert important["kind"] == "boundary"
    assert important["outcome"] == "success" and important["exhausted"] is False
    assert important["attempts"] == 3
    attempts = important["attempt_history"]
    assert [attempt["number"] for attempt in attempts] == [1, 2, 3]
    assert [attempt["outcome"] for attempt in attempts] == ["unknown", "unknown", "success"]
    assert all(important["created_at"] <= attempt["started_at"] <= attempt["finished_at"]
               <= important["deadline_at"] for attempt in attempts)
    requests = [json.loads(line) for line in messages.read_text().splitlines()]
    important_requests = [item for item in requests if item["title"].startswith("Please approve merging PR")]
    assert [item["attempt"] for item in important_requests] == [1, 2, 3]
    assert len({item["key"] for item in important_requests}) == 1
    assert sum(item["title"] == "Task created" for item in requests) == 1
    start, = [record for record in journal["records"] if record["kind"] == "run_start"]
    assert start["outcome"] == ("success" if state_read_error else "unknown") and start["attempts"] == 1
    assert start["exhausted"] is False
    assert all(not Path(f"/proc/{pid}").exists()
               for item in requests for pid in item["processes"])
    before = {path.relative_to(root): path.read_bytes() for path in root.rglob("*.json")}
    runs_before = snapshot_runs(root)
    sent_before = messages.read_bytes()
    for command in ("status", "history"):
        queried = invoke_cli_inprocess(git_repo, fixture, command, state["run_id"], "--json")
        assert queried.returncode == 0, queried.stdout + queried.stderr
        assert stdout_json(queried)["notifications"] == journal
    assert {path.relative_to(root): path.read_bytes() for path in root.rglob("*.json")} == before
    assert snapshot_runs(root) == runs_before
    assert messages.read_bytes() == sent_before
