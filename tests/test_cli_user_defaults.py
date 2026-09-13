from __future__ import annotations

import json
import re
import subprocess
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from typing import Any

import pytest

from agent_run import cli
from agent_run.agent_profiles import AgentProfileStore
from agent_run.user_defaults import UserDefaultsStore
from conftest import write_fixture
from test_cli import load_only_run_state, run_cli, stdout_json
from test_cli_delivery import human_blocker_step


def settings(*arguments: str) -> tuple[int, dict[str, Any]]:
    output = StringIO()
    with redirect_stdout(output):
        code = cli.main(["settings", *arguments, "--json"])
    return code, json.loads(output.getvalue())


@pytest.mark.parametrize("case", ["initializing", "missing", "starting_only", "applied", "corrupt"])
def test_run_settings_distinguishes_initialization_from_missing_or_corrupt_profile(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, case: str,
) -> None:
    from agent_run.state import StateStore
    from test_run_lifecycle import _file_snapshot

    monkeypatch.chdir(git_repo)
    root = git_repo / ".agent-run"
    state = {
        "run_id": "run-settings", "repository": "example/project",
        "parent": {"number": 1}, "status": "starting", "schema_version": 1,
        "base_resolution_pending": True, "currentness_resolution_pending": True,
        "agent_invocation_history": [], "active_agent_invocation": None,
    }
    if case == "missing":
        state["status"] = "active"
    elif case == "starting_only":
        state.pop("base_resolution_pending")
    elif case == "applied":
        state["action_application_receipt"] = {"action_id": "already-applied"}
    StateStore(root).save_run("run-settings", state)
    if case == "corrupt":
        (root / "profiles").mkdir()
        (root / "profiles/run-settings.json").write_text("broken")
    before = _file_snapshot(root)

    code, output = settings("show", "--run", "run-settings")

    if case == "initializing":
        assert code == 0
        assert output["source"] == "initializing"
        assert output["profile"] is None
        assert "初始化" in output["message"]
        human = StringIO()
        with redirect_stdout(human):
            assert cli.main(["settings", "show", "--run", "run-settings"]) == 0
        assert "初始化" in human.getvalue()
        assert "缺少 Agent Execution Profile" not in human.getvalue()
    else:
        assert code == 2
        assert output["result"] == "error"
        assert output.get("source") != "initializing"
    assert _file_snapshot(root) == before


def test_public_settings_file_cli_and_new_old_run_execution(
    git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two creations are needed to prove defaults affect execution, not just JSON."""
    monkeypatch.chdir(git_repo)
    store = UserDefaultsStore()
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(json.dumps({
        "policy": {"development_thread_policy": "new-per-attempt", "ticket_review_rounds": 2, "invocation_deadlines": {"development": "11m"}},
        "profile": {"development_model": "old-model", "development_effort": "high"},
    }))
    code, shown = settings("show")
    assert code == 0
    assert shown["policy"]["invocation_deadlines"]["development"] == 660
    assert shown["profile"]["profiles"]["publication"]["model"] == "old-model"
    assert shown["policy"]["development_thread_policy"] == "new-per-attempt"
    fixture = write_fixture(git_repo / "github.json", issues={})
    agents = tmp_path / "agents.json"
    agents.write_text(json.dumps({"developments": [human_blocker_step("original-thread")]}))
    started = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))
    assert started.returncode == 2, started.stdout + started.stderr
    original = load_only_run_state(git_repo)
    run_id = original["run_id"]
    profile = AgentProfileStore(git_repo / ".agent-run").load(run_id)
    assert profile is not None
    assert profile["bindings"][0]["model"] == "old-model"
    assert original["agent_invocation_history"][0]["deadline_seconds"] == 660

    code, saved = settings("configure", "--development-model", "new-model", "--development-deadline", "13m", "--development-thread-policy", "reuse")
    assert code == 0
    assert saved["notice"] == "仅影响之后创建的新 Run，已有 Run 保持原设置"
    document = json.loads(store.path.read_text())
    assert document["profile"]["development_effort"] == "high"
    assert document["policy"]["ticket_review_rounds"] == 2
    assert document["policy"]["development_thread_policy"] == "reuse"
    code, actual = settings("show", "--run", run_id)
    assert code == 0
    assert actual["scope"] == "run"
    assert actual["policy"]["development_thread_policy"] == "new-per-attempt"
    assert actual["policy"]["invocation_deadlines"]["development"] == 660
    assert actual["profile"]["profiles"]["development"]["model"] == "old-model"
    agents.write_text(json.dumps({"developments": [
        {**human_blocker_step("original-thread"), "expected_thread_id": "original-thread"}
    ]}))
    resumed = run_cli(git_repo, fixture, "resume", run_id, "--message", "Access restored", "--agent-fixture", str(agents))
    assert resumed.returncode == 2, resumed.stdout + resumed.stderr
    original_after = load_only_run_state(git_repo)
    assert [i["deadline_seconds"] for i in original_after["agent_invocation_history"]] == [660, 660], resumed.stdout + resumed.stderr
    frozen = AgentProfileStore(git_repo / ".agent-run").load(run_id)
    assert frozen is not None
    assert frozen["bindings"] == profile["bindings"]

    second_repo = tmp_path / "second"
    subprocess.run(["git", "clone", "--local", str(git_repo), str(second_repo)], check=True, capture_output=True)
    second_fixture = write_fixture(second_repo / "github.json", issues={})
    agents.write_text(json.dumps({"developments": [human_blocker_step("new-thread")]}))
    created = run_cli(second_repo, second_fixture, "run", "1", "--agent-fixture", str(agents))
    assert created.returncode == 2, created.stdout + created.stderr
    new_state = load_only_run_state(second_repo)
    new_profile = AgentProfileStore(second_repo / ".agent-run").load(new_state["run_id"])
    assert new_profile is not None
    assert new_profile["bindings"][0]["model"] == "new-model"
    assert new_profile["bindings"][0]["reasoning_effort"] == "high"
    assert new_state["agent_invocation_history"][0]["deadline_seconds"] == 780
    assert new_state["policy_snapshot"]["ticket_review_rounds"] == 2
    assert new_state["policy_snapshot"]["development_thread_policy"] == "reuse"


def test_public_settings_validation_legacy_and_atomic_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    store = UserDefaultsStore()
    store.legacy_path.parent.mkdir(parents=True, exist_ok=True)
    store.legacy_path.write_text('{"ticket_review_rounds": 7}')
    code, initial = settings("show")
    assert code == 0 and initial["source"] == "legacy-delivery-policy"
    assert not store.path.exists()
    code, saved = settings("configure", "--preset", "premium")
    assert code == 0 and saved["policy"]["ticket_review_rounds"] == 7
    previous = store.path.read_bytes()
    code, rejected = settings("configure", "--development-effort", "invalid")
    assert code == 2 and rejected["result"] == "error"
    assert store.path.read_bytes() == previous

    def fail_replace(*args: object) -> None:
        raise OSError("injected write failure")

    with monkeypatch.context() as scoped:
        scoped.setattr("agent_run.user_defaults.os.replace", fail_replace)
        code, rejected = settings("configure", "--ticket-review-rounds", "9")
    assert code == 2 and rejected["result"] == "error"
    assert store.path.read_bytes() == previous
    assert not list(store.path.parent.glob("*.tmp"))
    store.path.write_text('{"profile":{"review_effort":"invalid"}}')
    code, rejected = settings("show")
    assert code == 2
    assert "review_effort" in json.dumps(rejected)
    assert store.path.read_text() == '{"profile":{"review_effort":"invalid"}}'


def test_settings_human_output_and_complete_example(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    output = StringIO()
    with redirect_stdout(output):
        assert cli.main(["settings", "configure", "--ticket-review-rounds", "4"]) == 0
    assert "仅影响之后创建的新 Run，已有 Run 保持原设置" in output.getvalue()
    assert '"ticket_review_rounds": 4' in output.getvalue()
    example = Path(__file__).parents[1] / "docs/examples/user-defaults.json"
    document = json.loads(example.read_text())
    resolved = UserDefaultsStore().describe(document)
    from agent_run.agent_profiles import resolve_profiles
    from agent_run.delivery_policy import default_delivery_policy

    assert resolved["policy"] == default_delivery_policy().snapshot()
    for role, profile in resolve_profiles()["profiles"].items():
        for field in ("model", "reasoning_effort", "reference"):
            assert resolved["profile"]["profiles"][role][field] == profile[field]
    guide = example.parent.parent / "user-defaults.md"
    for block in re.findall(r"```json\n(.*?)\n```", guide.read_text(), re.DOTALL):
        independent = UserDefaultsStore().describe({"profile": json.loads(block)})
        assert independent["profile"]["profiles"]["publication"]["reference"] is None


@pytest.mark.parametrize("arguments,personal", [
    (["--ticket-review-rounds", "2"], {}),
    (["--development-thread-policy", "new-per-attempt"], {}),
    (["--development-model", "explicit-model"], {"profile": {"review_model": "personal-review"}}),
])
def test_creation_action_replay_preserves_frozen_configuration(
    arguments: list[str], personal: dict[str, Any], tmp_path: Path,
) -> None:
    """Protect semantic admission payloads without running another delivery loop."""
    from agent_run.delivery_policy import resolve_delivery_policy

    parsed = cli.build_parser().parse_args(["run", "228", *arguments])
    explicit = cli._profile_configuration(parsed)
    store = UserDefaultsStore()
    store.path.parent.mkdir(parents=True, exist_ok=True)
    resolved = store.resolve_creation(preset=explicit[0], overrides=explicit[1], document=personal)
    policy = resolve_delivery_policy(command_overrides=cli._policy_overrides(parsed))
    first = cli._run_action_payload(parsed, policy, resolved)
    store.path.write_text("broken")
    assert cli._run_payload_for_existing_action(parsed, first, None, explicit) == first
    changed = cli.build_parser().parse_args(["run", "228", "--review-model", "different-model"])
    assert cli._run_payload_for_existing_action(
        changed, first, None, cli._profile_configuration(changed)
    ) != first


def test_thread_policy_cannot_be_changed_for_existing_run() -> None:
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args([
            "resume", "229", "--development-thread-policy", "new-per-attempt",
        ])


@pytest.mark.parametrize("value", [None, False, 1, "invalid", [], {}])
def test_thread_policy_file_rejects_invalid_values(value: Any) -> None:
    store = UserDefaultsStore()
    store.path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps({"policy": {"development_thread_policy": value}})
    store.path.write_text(content)
    code, rejected = settings("show")
    assert code == 2
    assert "development_thread_policy" in json.dumps(rejected)
    assert store.path.read_text() == content
