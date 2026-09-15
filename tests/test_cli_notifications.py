"""Real CLI notification delivery, bounded cancellation and acceptance recovery."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path

import pytest

from agent_run.notification_cards import card
from agent_run.notification_events import events
from agent_run.notifications import read_notifications
from agent_run.presentation_helpers import human_status_term
from agent_run.user_defaults import UserDefaultsStore
from cli_fixtures import run_agents
from conftest import write_fixture
from test_cli import load_only_run_state, run_cli, stdout_json
from test_cli_delivery import parent_publication, passing_acceptance, ticket


@pytest.mark.parametrize("drain", [True, False], ids=["representative-delivery", "bounded-cancel"])
def test_cli_delivery_sends_cards_and_queries_never_send(
    git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, drain: bool,
) -> None:
    binary = tmp_path / "bin"
    binary.mkdir()
    messages = tmp_path / "messages.jsonl"
    tool = binary / "lark-cli"
    tool.write_text(f"#!{sys.executable}\n" + '''import json, sys, socket, os
from pathlib import Path
args = sys.argv[1:]
if "whoami" in args:
    print(json.dumps({"profile":"test", "appId":"cli_test", "identity":"bot", "available":True}))
else:
    with Path(''' + repr(str(messages)) + ''').open("a") as stream:
        stream.write(json.dumps(json.loads(args[args.index("--content")+1]), ensure_ascii=False)+"\\n")
    if os.environ.get("NOTIFICATION_TEST_PORT"):
        with socket.create_connection(("127.0.0.1", int(os.environ["NOTIFICATION_TEST_PORT"]))) as connection:
            connection.sendall(b'S')
            connection.recv(1)
    print(json.dumps({"data":{"message_id":"om_test"}}))
''')
    tool.chmod(0o700)
    monkeypatch.setenv("PATH", str(binary) + os.pathsep + os.environ["PATH"])
    UserDefaultsStore().configure(notifications={"enabled": True, "profile": "test", "open_id": "ou_test", "app_id": "cli_test"})
    fixture = write_fixture(git_repo / "github.json", issues={"3": ticket()})
    agents = run_agents(git_repo / "agents.json")

    def invoke(*arguments: str) -> subprocess.CompletedProcess[str]:
        # This explicit entrypoint is imported only by the CLI/Executor, never
        # by every supervisor and fake CLI through sitecustomize.
        script = """import os, socket, sys, threading
import agent_run.notifications as notifications
from agent_run.cli import main
original_close = notifications.Notifications.close
original_send = notifications.send
release_sender = threading.Event()
port = os.environ.get('NOTIFICATION_TEST_PORT')
if port:
    def send(*args):
        release_sender.wait()
        return original_send(*args)
    notifications.send = send

def close(self):
    try:
        if self.thread is not None:
            if port:
                release_sender.set()
                with socket.create_connection(('127.0.0.1', int(port)), timeout=15) as ready:
                    ready.sendall(b'C')
                    assert ready.recv(1) == b'1', 'close readiness not acknowledged'
            else:
                with self.condition:
                    settled = lambda: bool(self.document.get('records')) or any(
                        item['outcome'] in {'failed', 'unknown'} for item in self.document.get('pending', []))
                    assert self.condition.wait_for(settled, timeout=15), 'representative send did not settle'
                    assert self.document.get('records'), self.document
    finally:
        release_sender.set()
        original_close(self)
notifications.Notifications.close = close
raise SystemExit(main(sys.argv[1:]))
"""
        source = str(Path(__file__).resolve().parents[1] / "src")
        environment = {**os.environ, "PYTHONPATH": source + os.pathsep + os.environ.get("PYTHONPATH", "")}

        def execute() -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [sys.executable, "-c", script, *arguments, "--json", "--github-fixture", str(fixture)],
                cwd=git_repo, env=environment, capture_output=True, text=True, check=False,
            )

        if drain:
            return execute()
        # Business preparation is outside the cancellation window. Both the
        # close boundary and the real CLI must report ready before we release
        # original_close and measure its bounded exit. No queue-wide drain.
        with socket.socket() as server:
            server.bind(("127.0.0.1", 0))
            server.listen()
            server.settimeout(30)
            environment["NOTIFICATION_TEST_PORT"] = str(server.getsockname()[1])
            connections: dict[bytes, socket.socket] = {}
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(execute)
                try:
                    for _ in range(2):
                        connection, _ = server.accept()
                        connection.settimeout(15)
                        kind = connection.recv(1)
                        connections[kind] = connection
                    assert set(connections) == {b'C', b'S'}
                    connections[b'C'].sendall(b'1')
                    return future.result(timeout=8)
                finally:
                    # Release test barriers before waiting for the CLI, even
                    # if an assertion fails. Production reaps the sender tree.
                    for connection in connections.values():
                        connection.close()

    result = invoke("run", "1", "--agent-fixture", str(agents))
    assert result.returncode == 0, result.stderr
    output = stdout_json(result)
    assert output["status"] == "run_approval_pending"
    current = load_only_run_state(git_repo)
    assert current["notifications"]["profile"] == "test"
    # A representative real request proves Executor/transport integration.
    # Complete card combinations use the actual persisted lifecycle facts;
    # production close is allowed to leave later notifications unsent.
    delivered = [json.loads(line) for line in messages.read_text().splitlines()]
    assert delivered and all(item["schema"] == "2.0" for item in delivered)
    assert delivered[0]["header"]["title"]["content"] == "任务已启动"
    cards = [card(event) for event in events(current)]
    titles = [item["header"]["title"]["content"] for item in cards]
    assert "任务已启动" in titles
    assert any("开发 Agent" in value and "启动" in value for value in titles), titles
    assert any("验收 Agent" in value and "启动" in value for value in titles), titles
    assert any("发布说明已准备" in value for value in titles), titles
    assert any("最终 PR" in value and "已创建" in value for value in titles), titles
    assert "等待人工批准" in titles
    approval_card = next(item for item in cards if item["header"]["title"]["content"] == "等待人工批准")
    assert "未配置合并前检查" in json.dumps(approval_card, ensure_ascii=False)
    assert "整体交付完成" not in titles
    assert all(item["schema"] == "2.0" for item in cards)
    journal = read_notifications(git_repo / ".agent-run", output["run_id"])
    assert journal["seen"]
    if drain:
        assert any(record["outcome"] == "success" for record in journal["records"])
    else:
        assert any(item["outcome"] == "unknown" for item in journal["pending"])
        assert any(item["outcome"] == "pending" for item in journal["pending"])
    for result, expected in (
        ("none", "未配置合并前检查"), ("pass", human_status_term("pass")),
        ("pending", human_status_term("pending")), ("fail", human_status_term("fail")),
        ("unknown", "暂时无法确认"),
    ):
        snapshot = deepcopy(current)
        snapshot["run_publication"]["required_checks_evidence"]["result"] = result
        approval = next(event for event in events(snapshot) if event.get("current"))
        assert f"检查：{expected}" in approval["summary"]
    before = messages.read_bytes() if messages.exists() else b""
    for command in ("status", "history"):
        queried = run_cli(git_repo, fixture, command, output["run_id"], "--json")
        assert queried.returncode == 0, queried.stderr
        assert "notifications" in stdout_json(queried)
    assert (messages.read_bytes() if messages.exists() else b"") == before
    assert read_notifications(git_repo / ".agent-run", output["run_id"]) == journal
    # Approval reuses the immutable Run configuration even after defaults change.
    UserDefaultsStore().configure(notifications={"enabled": False})
    approved = invoke("approve", output["run_id"])
    assert approved.returncode == 0, approved.stderr
    assert stdout_json(approved)["status"] == "completed"
    completed = load_only_run_state(git_repo)
    assert completed["notifications"]["enabled"] is True
    cards = [card(event) for event in events(completed)]
    assert sum(item["header"]["title"]["content"] == "整体交付完成" for item in cards) == 1


@pytest.mark.parametrize("scope", ["ticket", "parent", "run"])
def test_cli_blocked_acceptance_resume_projects_same_round(
    git_repo: Path, scope: str,
) -> None:
    # Each scope owns a different persisted blocked-result path, so all three
    # need a real run/resume boundary. Notification rendering stays in process.
    fixture = write_fixture(git_repo / "github.json", issues={} if scope == "parent" else {"3": ticket()})
    agents = run_agents(git_repo / "agents.json")
    data = json.loads(agents.read_text())
    if scope == "parent":
        data["publications"] = [parent_publication()]
    key = "run_reviews" if scope == "run" else "reviews"
    review = data[key][0]
    review["checks"]["e2e"].update(
        status="blocked", evidence="External test account unavailable; restore access.", findings=[],
    )
    agents.write_text(json.dumps(data))
    blocked = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))
    assert blocked.returncode == 2, blocked.stdout + blocked.stderr
    state = load_only_run_state(git_repo)
    assert state["status"] == "ready_for_human"
    before = events(state)
    blocked_events = [event for event in before if event["kind"] == "stage_end" and "验收受阻" in event["title"]]
    assert len(blocked_events) == 1, before
    result = blocked_events[0]
    assert result["round"] == 1
    assert result["phase"]
    assert isinstance(result["duration_seconds"], int)
    assert "人工" in result["next_step"]
    starts = {event["id"] for event in before if event["kind"] == "stage_start"}
    resumed_data = json.loads(run_agents(git_repo / "resume-agents.json").read_text())
    resumed_data["developments"] = []
    if scope == "parent":
        resumed_data["publications"] = [parent_publication()]
    if scope == "run":
        resumed_data["reviews"] = []
        resumed_data["publications"] = []
    resumed_data[key] = [{**passing_acceptance(review["thread_id"], "Restored external acceptance passed."),
                          "expected_thread_id": review["thread_id"]}]
    agents.write_text(json.dumps(resumed_data))
    resumed = run_cli(git_repo, fixture, "resume", state["run_id"], "--message", "Access restored.",
                      "--agent-fixture", str(agents))
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    after = events(load_only_run_state(git_repo))
    assert starts <= {event["id"] for event in after if event["kind"] == "stage_start"}
    assert not any("验收受阻" in event["title"] for event in after)
    passed = [event for event in after if event["kind"] == "stage_end" and event["phase"] == result["phase"]
              and event.get("object") == result.get("object")]
    assert len(passed) == 1, passed
    assert passed[0]["round"] == result["round"]
    assert passed[0]["id"] != result["id"]
    assert passed[0]["color"] == "green"
    assert "验收通过" in passed[0]["title"]
    assert "未知" not in passed[0]["summary"]
    assert load_only_run_state(git_repo)["status"] in {"run_approval_pending", "parent_approval_pending"}
