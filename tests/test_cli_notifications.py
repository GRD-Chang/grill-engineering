"""Real CLI notification delivery, bounded cancellation and acceptance recovery."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime
from pathlib import Path

import pytest

from support.workspace import managed_state
from support.inprocess_cli import invoke_cli_inprocess

from agent_run.notification_cards import card
from agent_run.notification_events import events, recovery_event
from agent_run.notifications import read_notifications
from agent_run.presentation_helpers import human_status_term
from agent_run.user_defaults import UserDefaultsStore
from cli_fixtures import run_agents
from conftest import write_fixture
from test_cli import load_only_run_state, run_cli, stdout_json
from test_cli_delivery import parent_publication, passing_acceptance, repair_acceptance, ticket


@pytest.mark.parametrize("language,drain,scope,mode", [
    ("zh", True, "ticket", "detailed"),
    ("en", True, "ticket", "detailed"),
    ("zh", False, "ticket", "detailed"),
    ("zh", True, "ticket", "concise"),
    ("en", True, "parent", "concise"),
], ids=["zh-delivery", "en-delivery", "bounded-cancel", "concise-repairs", "parent-concise"])
def test_cli_delivery_sends_cards_and_queries_never_send(
    git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, drain: bool, language: str,
    scope: str, mode: str,
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
    UserDefaultsStore().configure(language=language, notifications={"enabled": True, "profile": "test", "open_id": "ou_test", "app_id": "cli_test"})
    fixture = write_fixture(git_repo / "github.json", issues={} if scope == "parent" else {"3": ticket()})
    agents = run_agents(git_repo / "agents.json")
    data = json.loads(agents.read_text())
    if scope == "parent":
        data["publications"] = [parent_publication()]
        data["reviews"] = [repair_acceptance("parent-reviewer-1"),
                           repair_acceptance("parent-reviewer-2"),
                           passing_acceptance("parent-reviewer-3", "Complete requirement passed.")]
        for ordinal in (1, 2):
            data["developments"].append({
                "expected_thread_id": "ticket-developer-3", "thread_id": "ticket-developer-3",
                "summary": f"Parent repair {ordinal}",
                "write_files": {"parent-repair.txt": f"round {ordinal}\n"},
            })
    elif mode == "concise":
        # Two failed checks in the same overall acceptance produce two repair
        # rounds, but only one concise transition into repair.
        data["run_reviews"] = [repair_acceptance("run-reviewer-1"),
                               repair_acceptance("repair-reviewer-1"),
                               passing_acceptance("repair-reviewer-2", "Repaired candidate passed.")]
        for ordinal in (1, 2):
            data["developments"].append({
                "expected_thread_id": None if ordinal == 1 else "repair-developer",
                "thread_id": "repair-developer", "summary": f"Repair {ordinal}",
                "write_files": {"repair.txt": f"round {ordinal}\n"},
            })
        data["publications"].append(deepcopy(data["publications"][0]))
    elif drain:
        data["reviews"] = [repair_acceptance("ticket-reviewer-1"),
                           repair_acceptance("ticket-reviewer-2"),
                           passing_acceptance("ticket-reviewer-3", "Ticket repairs passed.")]
        for ordinal in (1, 2):
            data["developments"].append({
                "expected_thread_id": "ticket-developer-3", "thread_id": "ticket-developer-3",
                "summary": f"Ticket repair {ordinal}",
                "write_files": {"ticket-repair.txt": f"round {ordinal}\n"},
            })
    agents.write_text(json.dumps(data))

    def invoke(*arguments: str) -> subprocess.CompletedProcess[str]:
        # This explicit entrypoint is imported only by the CLI/Executor, never
        # by every supervisor and fake CLI through sitecustomize.
        script = """import os, socket, sys, threading
import agent_run.notifications as notifications
from agent_run.cli import main
original_close = notifications.Notifications.close
original_send = notifications.send
original_observe = notifications.Notifications.observe
# Synchronize only the isolated fast transport. This makes selected business
# events inspectable without assuming production exit drains every message.
def observe(self, *args, **kwargs):
    state = args[0]
    for event in notifications.events(state):
        if not kwargs.get('recovering') and event['kind'] == 'ticket_started' and event['id'] not in self.document.get('seen', []):
            active = state.get('active_agent_invocation') or {}
            assert active.get('reported_thread_id') and active.get('status') == 'running', active
    original_observe(self, *args, **kwargs)
    if not os.environ.get('NOTIFICATION_TEST_PORT') and self.enabled and self.thread is not None:
        with self.condition:
            assert self.condition.wait_for(lambda: not self.document['pending'], timeout=15), self.document
notifications.Notifications.observe = observe
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

    result = invoke("run", "1", "--notification-mode", mode, "--agent-fixture", str(agents))
    assert result.returncode == 0, result.stderr
    output = stdout_json(result)
    assert output["status"] == ("parent_approval_pending" if scope == "parent" else "run_approval_pending")
    current = load_only_run_state(git_repo)
    assert current["notifications"]["profile"] == "test"
    # A representative real request proves Executor/transport integration.
    # Complete card combinations use the actual persisted lifecycle facts;
    # production close is allowed to leave later notifications unsent.
    delivered = [json.loads(line) for line in messages.read_text().splitlines()]
    assert delivered and all(item["schema"] == "2.0" for item in delivered)
    assert current["language"] == language
    def localized(zh: str, en: str) -> str:
        return zh if language == "zh" else en

    assert delivered[0]["header"]["title"]["content"] == localized("任务已开始", "Task started")
    assert localized("开始时间", "Started at") in str(delivered[0])
    assert localized("打开 Issue", "Open Issue") in str(delivered[0])
    cards = [card(event) for event in events(current)]
    titles = [item["header"]["title"]["content"] for item in cards]
    assert localized("任务已开始", "Task started") in titles
    if mode == "detailed":
        assert sum(event["kind"] == "ticket_started" for event in events(current)) == 1
        development_starts = [event for event in events(current)
                              if event["kind"] == "stage_start" and event.get("role") == "development"]
        assert len(development_starts) == (2 if drain else 0)
        assert all(event["round"] in {2, 3} for event in development_starts)
        assert any(localized("开始开发子任务", "Started developing ticket") in value for value in titles), titles
        assert any(localized("正在验收", "Reviewing") in value for value in titles), titles
        assert any(localized("说明已准备好", "description is ready") in value for value in titles), titles
    else:
        projected = events(current)
        kinds = [event["kind"] for event in projected]
        assert kinds.count("acceptance_passed") == 1
        assert kinds.count("ticket_started") == (1 if scope == "ticket" else 0)
        assert kinds.count("acceptance_started") == (1 if scope == "ticket" else 0)
        assert kinds.count("acceptance_repair") == (1 if scope == "ticket" else 0)
        assert "stage_start" not in kinds and "stage_end" not in kinds
        # Inspect requests actually accepted by the isolated CLI, not merely
        # render another copy of the projection or assert an old total count.
        delivered_titles = [item["header"]["title"]["content"] for item in delivered]
        for event in projected:
            assert delivered_titles.count(event["title"]) == 1, delivered_titles
        passed = next(event for event in projected if event["kind"] == "acceptance_passed")
        approval = next(event for event in projected if event.get("current"))
        assert passed["id"] != approval["id"]
        assert delivered_titles.index(passed["title"]) < delivered_titles.index(approval["title"])
        if scope == "ticket":
            assert current["run_acceptance"]["repair_cycle"]["code_modification_attempts"] == 2
        else:
            assert len(delivered_titles) == 3  # Start, formal pass, approval; repairs stay quiet.
    assert not any("最终 PR" in value and "已创建" in value for value in titles), titles
    assert any(localized("请批准合并 PR", "Please approve merging PR") in value for value in titles)
    approval_card = next(item for item in cards if item["header"]["title"]["content"]  .startswith(localized("请批准合并 PR", "Please approve merging PR")))
    assert localized("未配置合并前检查", "No pre-merge checks configured") in json.dumps(approval_card, ensure_ascii=False)
    assert localized("任务已完成", "Task completed") not in titles
    assert all(item["schema"] == "2.0" for item in cards)
    journal = read_notifications(managed_state(git_repo), output["run_id"])
    assert journal["seen"]
    if drain:
        assert any(record["outcome"] == "success" for record in journal["records"])
    else:
        assert any(item["outcome"] == "unknown" for item in journal["pending"])
        assert any(item["outcome"] == "pending" for item in journal["pending"])
    for result, expected in (
        ("none", localized("未配置合并前检查", "No pre-merge checks configured")), ("pass", localized(human_status_term("pass"), "Passed")),
        ("pending", localized(human_status_term("pending"), "Pending")), ("fail", localized(human_status_term("fail"), "Failed")),
        ("unknown", localized("暂时无法确认", "Cannot currently confirm")),
    ):
        snapshot = deepcopy(current)
        snapshot["parent_job" if scope == "parent" else "run_publication"]["required_checks_evidence"]["result"] = result
        approval = next(event for event in events(snapshot) if event.get("current"))
        assert expected in approval["summary"]
    before = messages.read_bytes() if messages.exists() else b""
    # Existing Runs keep their language after a personal preference change.
    UserDefaultsStore().configure(language="en" if language == "zh" else "zh")
    state_root = managed_state(git_repo)
    durable_before = {
        path.relative_to(state_root): path.read_bytes()
        for path in state_root.rglob("*.json")
    }
    for command in ("status", "history"):
        queried = run_cli(git_repo, fixture, command, output["run_id"], "--json")
        assert queried.returncode == 0, queried.stderr
        assert "notifications" in stdout_json(queried)
        if command == "history" and scope == "ticket" and drain:
            history = stdout_json(queried)
            completion = next(event for event in events(current) if event["kind"] == "ticket_completed")
            actual_card = next(item for item in delivered
                               if item["header"]["title"]["content"] == completion["title"])
            assert actual_card == card(completion)
            rendered = json.dumps(actual_card, ensure_ascii=False)
            assert current["ticket_jobs"]["3"]["phase"] in {"completed", "merged"}
            assert ticket()["title"] in rendered
            assert completion["summary"] in rendered
            assert completion["query"] in rendered
            invocations = [item for item in history["agent_invocations"]
                           if item["work_subject"] == "ticket:3"]
            expected_rounds = 3 if mode == "detailed" else 1
            for role, label in (("development", localized("开发投入", "Development effort")),
                                ("review", localized("验收投入", "Review effort"))):
                attempts = [item for item in history["semantic_agent_attempts"]
                            if item["work_subject"] == "ticket:3"
                            and item["role"] == ("reviewer" if role == "review" else role)]
                assert len({item["attempt_id"] for item in attempts}) == expected_rounds
                facts = completion["effort"][role]
                assert facts["rounds"] == expected_rounds
                role_invocations = [item for item in invocations
                                    if item["role"] == ("fresh_acceptance" if role == "review" else role)]
                seconds = sum(int((datetime.fromisoformat(item["ended_at"])
                                   - datetime.fromisoformat(item["started_at"])).total_seconds())
                              for item in role_invocations)
                assert facts["execution_seconds"] == seconds
                assert label in rendered
                assert localized(f"{expected_rounds} 轮", f"{expected_rounds} rounds") in rendered
                assert len(facts["configurations"]) == 1
                configuration = facts["configurations"][0]
                assert configuration["rounds"] == expected_rounds
                assert configuration["execution_seconds"] == seconds
                assert {(item["model"], item["reasoning_effort"]) for item in role_invocations} == {
                    (configuration["model"], configuration["reasoning_effort"])}
                assert configuration["model"] in rendered
                assert configuration["reasoning_effort"] in rendered
            assert any(item["role"] == "publication" for item in invocations)
            assert completion["total_seconds"] == sum(
                int((datetime.fromisoformat(item["ended_at"])
                     - datetime.fromisoformat(item["started_at"])).total_seconds())
                for item in invocations)
        arguments = ("--plain", "--details") if command == "history" else ("--plain",)
        human = invoke_cli_inprocess(git_repo, fixture, command, output["run_id"], *arguments)
        assert human.returncode == 0, human.stderr
        assert localized("下一步", "Next") in human.stdout
        assert "agent-run approve" in human.stdout
    assert {
        path.relative_to(state_root): path.read_bytes()
        for path in state_root.rglob("*.json")
    } == durable_before
    assert (messages.read_bytes() if messages.exists() else b"") == before
    assert read_notifications(managed_state(git_repo), output["run_id"]) == journal
    # Approval reuses the immutable Run configuration even after defaults change.
    UserDefaultsStore().configure(language="en" if language == "zh" else "zh", notifications={"enabled": False})
    delivered_before_approval = len(messages.read_text().splitlines())
    approved = invoke("approve", output["run_id"])
    assert approved.returncode == 0, approved.stderr
    assert stdout_json(approved)["status"] == "completed"
    completed = load_only_run_state(git_repo)
    assert completed["notifications"]["enabled"] is True
    assert completed["language"] == language
    approval_deliveries = [json.loads(line) for line in messages.read_text().splitlines()[delivered_before_approval:]]
    assert approval_deliveries
    if mode == "concise":
        restarted_titles = [item["header"]["title"]["content"] for item in approval_deliveries]
        assert restarted_titles == [localized("任务已完成", "Task completed")]
    for delivered_card in approval_deliveries:
        assert localized("打开 PR", "Open PR") in str(delivered_card)
    cards = [card(event) for event in events(completed)]
    assert sum(item["header"]["title"]["content"] == localized("任务已完成", "Task completed") for item in cards) == 1


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
    blocked = run_cli(git_repo, fixture, "run", "1", "--notification-mode", "detailed", "--agent-fixture", str(agents))
    assert blocked.returncode == 2, blocked.stdout + blocked.stderr
    state = load_only_run_state(git_repo)
    assert state["status"] == "ready_for_human"
    expected_number = 3 if scope == "ticket" else 1
    expected_title = state["ticket_graph"]["tickets"]["3"]["title"] if scope == "ticket" else state["parent"]["title"]
    for mode in ("concise", "detailed"):
        snapshot = deepcopy(state)
        snapshot["notifications"]["mode"] = mode
        pending = next(event for event in events(snapshot) if event.get("current"))
        recovered = recovery_event(snapshot, [pending])
        assert recovered is not None
        for notification in (pending, recovered):
            assert notification["task_number"] == expected_number
            assert notification["task_title"] == expected_title
            assert notification["url"] == f"https://github.com/example/project/issues/{expected_number}"
            rendered = card(notification)
            assert rendered["header"]["subtitle"]["content"] == f"example/project · #{expected_number}"
            assert expected_title in str(rendered)
            assert notification["url"] in str(rendered)
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
    passed = [event for event in after if event["kind"] in {"stage_end", "acceptance_passed"} and event.get("attempt_id") == result["attempt_id"]]
    assert len(passed) == 1, passed
    assert passed[0]["round"] == result["round"]
    assert passed[0]["id"] != result["id"]
    assert passed[0]["color"] == "green"
    assert "验收通过" in passed[0]["title"]
    assert "未知" not in passed[0]["summary"]
    assert load_only_run_state(git_repo)["status"] in {"run_approval_pending", "parent_approval_pending"}
