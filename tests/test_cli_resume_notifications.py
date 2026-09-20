"""Public Resume with persisted Runs and an isolated Feishu executable."""
from __future__ import annotations

import json
import os
import select
import subprocess
import sys
from pathlib import Path

import pytest

from agent_run.notifications import Notifications, read_notifications
from agent_run.messages import text
from agent_run.user_defaults import UserDefaultsStore
from cli_fixtures import run_agents
from conftest import write_fixture
from support.inprocess_cli import invoke_cli_inprocess
from support.workspace import managed_state
from test_cli import load_only_run_state, run_cli
from test_cli_delivery import parent_publication
from test_run_existing_executor import _GATED_ACTION


@pytest.mark.parametrize("outcome", ["preflight", "launch_failure", "human_action", "started", "controller", "running", "preflight_human"])
def test_public_resume_reports_actual_result(git_repo: Path, tmp_path: Path,
                                             monkeypatch: pytest.MonkeyPatch, outcome: str) -> None:
    messages = tmp_path / "cards.jsonl"
    binary = tmp_path / "bin"
    binary.mkdir()
    tool = binary / "lark-cli"
    tool.write_text(f"#!{sys.executable}\n" + """import json, sys
from pathlib import Path
args = sys.argv[1:]
if 'whoami' in args:
    print(json.dumps({'profile':'test','appId':'cli_test','identity':'bot','available':True}))
else:
    with Path(""" + repr(str(messages)) + """).open('a') as stream:
        stream.write(args[args.index('--content')+1]+'\\n')
    print(json.dumps({'data':{'message_id':'om_test'}}))
""")
    tool.chmod(0o700)
    monkeypatch.setenv("PATH", str(binary) + os.pathsep + os.environ["PATH"])
    UserDefaultsStore().configure(language="en", notifications={"enabled": True, "profile": "test",
        "open_id": "ou_test", "app_id": "cli_test"})
    fixture = write_fixture(git_repo / "github.json", issues={},
        delivery={"required_checks": ["none", "pending"]} if outcome == "controller" else {},
        supervision_clock_multiplier=120)
    agents = run_agents(git_repo / "agents.json")
    data = json.loads(agents.read_text())
    data["publications"] = [parent_publication()]
    if outcome != "controller":
        data["reviews"][0]["checks"]["e2e"].update(status="blocked", evidence="External account needed.", findings=[])
    agents.write_text(json.dumps(data))
    initial = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))
    assert initial.returncode == (0 if outcome == "controller" else 2), initial.stdout + initial.stderr
    state = load_only_run_state(git_repo)
    if outcome == "controller":
        approval = run_cli(git_repo, fixture, "approve", state["run_id"])
        assert approval.returncode == 2, approval.stdout + approval.stderr
        state = load_only_run_state(git_repo)
        assert state["status"] == "supervision_timeout"
        github_data = json.loads(fixture.read_text())
        github_data["delivery"].update(required_checks=["pass"], check_position=0)
        fixture.write_text(json.dumps(github_data))
    else:
        assert state["status"] == "ready_for_human"
    before_cards = len(messages.read_text().splitlines()) if messages.exists() else 0
    before_invocations = state.get("agent_invocation_history", [])
    data["developments"] = []
    data["reviews"][0]["expected_thread_id"] = data["reviews"][0]["thread_id"]
    if outcome == "launch_failure":
        data["reviews"][0]["expected_thread_id"] = "wrong-thread"
    if outcome == "started":
        data["reviews"][0]["checks"]["e2e"].update(status="pass", evidence="Access restored.")
    agents.write_text(json.dumps(data))
    if outcome == "preflight_human":
        github_data = json.loads(fixture.read_text())
        github_data["parent"]["body"] += " Updated authoritative requirement."
        fixture.write_text(json.dumps(github_data))
    original_observe, original_close = Notifications.observe, Notifications.close

    def drain(sender: Notifications) -> None:
        if sender.thread is not None:
            with sender.condition:
                assert sender.condition.wait_for(lambda: not sender.document["pending"], timeout=10)

    def observe(sender: Notifications, *args, **kwargs) -> None:
        original_observe(sender, *args, **kwargs)
        drain(sender)

    def close(sender: Notifications) -> None:
        try:
            drain(sender)
        finally:
            original_close(sender)

    monkeypatch.setattr(Notifications, "observe", observe)
    monkeypatch.setattr(Notifications, "close", close)
    if outcome == "running":
        ready_read, ready_write = os.pipe()
        release_read, release_write = os.pipe()
        process = subprocess.Popen([
            sys.executable, "-c", _GATED_ACTION, str(ready_write), str(release_read), "completed",
            "resume", state["run_id"], "--json", "--message", "Access restored.",
            "--github-fixture", str(fixture), "--agent-fixture", str(agents),
        ], cwd=git_repo, pass_fds=(ready_write, release_read), stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True,
            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")})
        os.close(ready_write)
        os.close(release_read)
        try:
            assert select.select([ready_read], [], [], 10)[0] == [ready_read]
            assert os.read(ready_read, 1) == b"1"
            before = read_notifications(managed_state(git_repo), state["run_id"])
            cards_before = messages.read_bytes() if messages.exists() else b""
            attached = invoke_cli_inprocess(git_repo, fixture, "resume", state["run_id"], "--json",
                "--message", "Access restored.", "--agent-fixture", str(agents))
            assert attached.returncode == 0, attached.stdout + attached.stderr
            assert json.loads(attached.stdout)["action"]["submission"] == "attached"
            assert read_notifications(managed_state(git_repo), state["run_id"]) == before
            assert (messages.read_bytes() if messages.exists() else b"") == cards_before
        finally:
            os.write(release_write, b"1")
            os.close(release_write)
            os.close(ready_read)
            try:
                process.communicate(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=5)
        return
    response = [] if outcome == "controller" else ["--message", " " if outcome == "preflight" else "Access restored."]
    result = invoke_cli_inprocess(git_repo, fixture, "resume", state["run_id"], "--json",
        *response, "--agent-fixture", str(agents))
    assert result.returncode == (0 if outcome in {"started", "controller"} else 2), result.stdout + result.stderr
    document = read_notifications(managed_state(git_repo), state["run_id"])
    assert document["resume_results"], document
    cards = [json.loads(line) for line in messages.read_text().splitlines()[before_cards:]]
    titles = [card["header"]["title"]["content"] for card in cards]
    expected = "failed" if outcome in {"preflight", "launch_failure"} else "started" if outcome == "controller" else "human_action" if outcome == "preflight_human" else outcome
    assert titles.count(text("notification.event.resume_" + expected, language="en")) == 1, titles
    if outcome in {"preflight", "launch_failure"}:
        assert text("notification.event.resume_started", language="en") not in titles
        assert text("notification.event.execution_failed", language="en") not in titles
    if outcome == "human_action":
        assert text("notification.event.human_title", language="en") not in titles
    if outcome == "controller":
        final = load_only_run_state(git_repo)
        assert final["status"] == "completed"
        assert final.get("agent_invocation_history", []) == before_invocations
