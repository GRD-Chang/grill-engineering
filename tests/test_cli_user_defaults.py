from __future__ import annotations

import json
import re
import subprocess
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from typing import Any

import pytest

from support.workspace import managed_state, prepare_workspace

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
    prepare_workspace(git_repo)
    subprocess.run(["git", "remote", "add", "origin", "https://github.com/example/project.git"], cwd=git_repo, check=True)
    root = managed_state(git_repo)
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
    from agent_run.prompt_resources import personal_method_directory

    methods = personal_method_directory()
    methods.mkdir(parents=True)
    common = methods / "development.md"
    common.write_text("旧任务个人方法 token=literal-example\n", encoding="utf-8")
    store = UserDefaultsStore()
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(json.dumps({
        "policy": {"development_thread_policy": "new-per-attempt", "ticket_review_rounds": 2, "invocation_deadlines": {"development": "11m"}},
        "profile": {"development_model": "old-model", "development_effort": "high"},
        "notifications": {"enabled": True, "open_id": "ou_old", "profile": "work", "app_id": "cli_old"},
    }))
    code, shown = settings("show")
    assert code == 0
    assert shown["policy"]["invocation_deadlines"]["development"] == 660
    assert shown["profile"]["profiles"]["publication"]["model"] == "old-model"
    assert shown["policy"]["development_thread_policy"] == "new-per-attempt"
    fixture = write_fixture(git_repo / "github.json", issues={})
    agents = tmp_path / "agents.json"
    agents.write_text(json.dumps({"developments": [human_blocker_step("original-thread")]}))
    started = run_cli(git_repo, fixture, "run", "1", "--no-notifications", "--agent-fixture", str(agents))
    assert started.returncode == 2, started.stdout + started.stderr
    original = load_only_run_state(git_repo)
    assert original["notifications"]["enabled"] is False
    assert original["creation_configuration"]["notifications"] == original["notifications"]
    assert original["prompt_resources"]["methods/development"] == "旧任务个人方法 token=literal-example\n"
    common.write_text("新任务个人方法\n", encoding="utf-8")
    run_id = original["run_id"]
    profile = AgentProfileStore(managed_state(git_repo)).load(run_id)
    assert profile is not None
    assert profile["bindings"][0]["model"] == "old-model"
    assert original["agent_invocation_history"][0]["deadline_seconds"] == 660

    code, saved = settings("configure", "--development-model", "new-model", "--development-deadline", "13m", "--development-thread-policy", "reuse", "--no-notifications", "--notification-open-id", "ou_new")
    assert code == 0
    assert saved["notice"] == "仅影响之后创建的新 Run，已有 Run 保持原设置"
    document = json.loads(store.path.read_text())
    assert document["profile"]["development_effort"] == "high"
    assert document["policy"]["ticket_review_rounds"] == 2
    assert document["policy"]["development_thread_policy"] == "reuse"
    code, actual = settings("show", "--run", run_id)
    assert code == 0
    assert actual["scope"] == "run"
    assert actual["notifications"] == original["notifications"]
    assert actual["policy"]["development_thread_policy"] == "new-per-attempt"
    assert actual["policy"]["invocation_deadlines"]["development"] == 660
    assert actual["profile"]["profiles"]["development"]["model"] == "old-model"
    agents.write_text(json.dumps({"developments": [
        {**human_blocker_step("original-thread"), "expected_thread_id": "original-thread"}
    ]}))
    resumed = run_cli(git_repo, fixture, "resume", run_id, "--message", "Access restored", "--agent-fixture", str(agents))
    assert resumed.returncode == 2, resumed.stdout + resumed.stderr
    original_after = load_only_run_state(git_repo)
    assert original_after["prompt_resources"] == original["prompt_resources"]
    assert all("prompt_resources" not in item for item in original_after["agent_invocation_history"])
    assert [i["deadline_seconds"] for i in original_after["agent_invocation_history"]] == [660, 660], resumed.stdout + resumed.stderr
    frozen = AgentProfileStore(managed_state(git_repo)).load(run_id)
    assert frozen is not None
    assert frozen["bindings"] == profile["bindings"]

    second_repo = tmp_path / "second"
    subprocess.run(["git", "clone", "--local", str(git_repo), str(second_repo)], check=True, capture_output=True)
    second_fixture = write_fixture(second_repo / "github.json", issues={}, repository="example/second")
    agents.write_text(json.dumps({"developments": [human_blocker_step("new-thread")]}))
    created = run_cli(second_repo, second_fixture, "run", "1", "--agent-fixture", str(agents))
    assert created.returncode == 2, created.stdout + created.stderr
    new_state = load_only_run_state(second_repo)
    assert new_state["prompt_resources"]["methods/development"] == "新任务个人方法\n"
    assert new_state["notifications"]["enabled"] is False
    assert new_state["notifications"]["open_id"] == "ou_new"
    new_profile = AgentProfileStore(managed_state(second_repo)).load(new_state["run_id"])
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
    assert "子任务验收轮数: 4" in output.getvalue()
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
        payload = json.loads(block)
        if "acceptance_scope" in payload:
            from agent_run.development_prompts import development_prompt
            assert payload["task_issue_url"] in development_prompt(payload)
            continue
        independent = UserDefaultsStore().describe({"profile": payload})
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


def test_notification_settings_are_optional_and_bound_to_an_app() -> None:
    code, initial = settings("show")
    assert code == 0
    assert initial["notifications"]["enabled"] is False
    code, configured = settings(
        "configure", "--notifications", "--notification-open-id", "ou_recipient",
        "--notification-profile", "work", "--notification-app-id", "cli_application",
    )
    assert code == 0
    assert configured["notifications"] == {
        "enabled": True, "open_id": "ou_recipient", "profile": "work", "app_id": "cli_application", "mode": "concise",
    }
    settings("configure", "--no-notifications")
    assert UserDefaultsStore().load()["notifications"]["open_id"] == "ou_recipient"


def test_bad_notification_settings_do_not_block_run_configuration() -> None:
    from agent_run.user_defaults import notification_snapshot

    store = UserDefaultsStore()
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text('{"notifications":{"enabled":"yes"}}')
    document = store.load()
    snapshot = notification_snapshot(document["notifications"])
    assert snapshot["enabled"] is False
    assert "notifications.enabled" in snapshot["unavailable_reason"]
    assert "unavailable_reason" not in notification_snapshot(document["notifications"], disabled=True)
    assert store.resolve_creation(document=document)[0] == "economy"


def test_notification_creation_replay_preserves_snapshot() -> None:
    from agent_run.delivery_policy import resolve_delivery_policy

    parsed = cli.build_parser().parse_args(["run", "228"])
    payload = cli._run_action_payload(parsed, resolve_delivery_policy(), (None, {}))
    payload["notifications"] = {"enabled": True, "open_id": "ou_old", "profile": "work", "app_id": "cli_app"}
    UserDefaultsStore().path.parent.mkdir(parents=True, exist_ok=True)
    UserDefaultsStore().path.write_text("broken")
    assert cli._run_payload_for_existing_action(parsed, payload, None, (None, {})) == payload
    changed = cli.build_parser().parse_args(["run", "228", "--review-model", "new-model"])
    updated = cli._run_payload_for_existing_action(changed, payload, None, cli._profile_configuration(changed))
    assert updated["notifications"] == payload["notifications"]


@pytest.mark.parametrize("mode", ["concise", "detailed"])
def test_notification_mode_configuration_and_override(mode: str) -> None:
    from agent_run.user_defaults import notification_snapshot

    code, configured = settings("configure", "--notification-mode", mode)
    assert code == 0
    assert configured["notifications"]["enabled"] is True
    assert configured["notifications"]["mode"] == mode
    legacy = {"enabled": True, "open_id": "ou_old"}
    assert notification_snapshot(legacy)["mode"] == "concise"
    assert "mode" not in legacy
    assert notification_snapshot(legacy, mode=mode)["mode"] == mode
    settings("configure", "--no-notifications")
    assert settings("show")[1]["notifications"]["enabled"] is False
    assert settings("show")[1]["notifications"]["mode"] == mode
    assert notification_snapshot({"mode": "invalid"})["unavailable_reason"]


@pytest.mark.parametrize("arguments, enabled, mode", [
    ([], True, "concise"),
    (["--notification-mode", "detailed"], True, "detailed"),
    (["--notification-mode", "concise"], True, "concise"),
    (["--no-notifications"], False, "concise"),
])
def test_new_run_freezes_notification_mode(
    git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    arguments: list[str], enabled: bool, mode: str,
) -> None:
    monkeypatch.chdir(git_repo)
    # No app binding: exercise creation without calling an external account.
    UserDefaultsStore().configure(notifications={"enabled": True})
    fixture = write_fixture(git_repo / "github.json", issues={})
    agents = tmp_path / "agents.json"
    agents.write_text(json.dumps({"developments": [human_blocker_step("thread")]}))
    started = run_cli(git_repo, fixture, "run", "1", *arguments, "--agent-fixture", str(agents))
    assert started.returncode == 2, started.stdout + started.stderr
    state = load_only_run_state(git_repo)
    assert state["notifications"]["enabled"] is enabled
    assert state["notifications"]["mode"] == mode
    settings("configure", "--notification-mode", "detailed" if mode == "concise" else "concise")
    assert settings("show", "--run", state["run_id"])[1]["notifications"] == state["notifications"]
    # Attaching to an existing run cannot replace its immutable snapshot.
    attached = run_cli(git_repo, fixture, "run", "1", "--notification-mode", "detailed")
    assert attached.returncode == 2, attached.stdout + attached.stderr
    assert load_only_run_state(git_repo)["notifications"] == state["notifications"]


@pytest.mark.parametrize("ticket,language", [(False, "zh"), (True, "en")])
def test_cli_restart_passes_frozen_methods_to_real_prompt_boundary(
    git_repo: Path, tmp_path: Path, ticket: bool, language: str,
) -> None:
    """Keep CLI/restart/files real; replace only the existing Worker process seam."""
    import os
    import shutil
    import sys
    from agent_run import prompt_resources

    assert settings("configure", "--language", language)[0] == 0
    methods = prompt_resources.personal_method_directory()
    methods.mkdir(parents=True)
    custom = methods / "development.md"
    custom.write_text("创建时个人开发方法", encoding="utf-8")
    builtin = tmp_path / "builtin" / "zh"
    shutil.copytree(prompt_resources.RESOURCE_ROOT.parent, builtin.parent)
    selected_builtin = builtin.parent / language
    resume_resource = selected_builtin / "internal/development-resume.md"
    resume_resource.write_text(resume_resource.read_text() + "\n创建时内部续接方法", encoding="utf-8")
    capture = tmp_path / "prompts.jsonl"
    driver = tmp_path / "cli-worker-capture.py"
    driver.write_text('''import json, subprocess, sys
from pathlib import Path
from agent_run import cli, codex, prompt_resources
prompt_resources.RESOURCE_ROOT = Path(sys.argv.pop(1))
capture = Path(sys.argv.pop(1))
class Backend(codex.CodexCliBackend):
    def __init__(self, **kwargs):
        super().__init__(credential_provider=lambda: "isolated-reader", **kwargs)
def worker(arguments, **options):
    with capture.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(options["prompt"], ensure_ascii=False) + "\\n")
    Path(arguments[arguments.index("--output-last-message") + 1]).write_text(json.dumps({
        "result_kind": "human_blocker", "summary": None,
        "human_blockers": ["测试边界：等待人工回复"]
    }), encoding="utf-8")
    return subprocess.CompletedProcess(arguments, 0,
        '{"type":"thread.started","thread_id":"isolated-prompt-thread"}\\n', "")
cli.CodexCliBackend = Backend
codex.run_worker_process = worker
raise SystemExit(cli.main(sys.argv[1:]))
''', encoding="utf-8")
    fixture = write_fixture(git_repo / "github.json", issues={"2": {
        "number": 2, "title": "Prompt integration", "body": "Implement this task.",
        "state": "OPEN", "labels": ["ready-for-agent"], "blocked_by": [],
    }} if ticket else {})
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(Path(__file__).parents[1] / "src")

    def call(repo: Path, github: Path, *arguments: str) -> str:
        result = subprocess.run(
            [sys.executable, str(driver), str(builtin), str(capture), *arguments,
             "--github-fixture", str(github), "--json"], cwd=repo, env=environment,
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 2, result.stdout + result.stderr
        return json.loads(capture.read_text().splitlines()[-1])

    initial = call(git_repo, fixture, "run", "1", "--no-notifications")
    assert "创建时个人开发方法" in initial
    state = load_only_run_state(git_repo)
    assert state["language"] == language
    other_language = "en" if language == "zh" else "zh"
    assert settings("configure", "--language", other_language)[0] == 0
    other_methods = prompt_resources.personal_method_directory()
    other_methods.mkdir(parents=True)
    (other_methods / "development.md").write_text("另一语言新任务方法", encoding="utf-8")
    custom.write_text("修改后个人开发方法", encoding="utf-8")
    resume_resource.write_text("修改后内部续接方法", encoding="utf-8")
    resumed = call(git_repo, fixture, "resume", state["run_id"], "--message", "继续核验")
    assert "创建时内部续接方法" in resumed
    assert "修改后内部续接方法" not in resumed
    assert "继续核验" in resumed
    restarted = call(git_repo, fixture, "resume", state["run_id"], "--new-thread", "--message", "重新核验")
    assert "创建时个人开发方法" in restarted
    assert "修改后个人开发方法" not in restarted
    assert "重新核验" in restarted
    second = tmp_path / "second"
    subprocess.run(["git", "clone", "--local", str(git_repo), str(second)], check=True, capture_output=True)
    other_fixture = write_fixture(second / "github.json", issues={}, repository="example/second")
    fresh = call(second, other_fixture, "run", "1", "--no-notifications")
    assert "另一语言新任务方法" in fresh
    assert "修改后个人开发方法" not in fresh
    assert "创建时个人开发方法" not in fresh
    assert load_only_run_state(second)["language"] == other_language
    assert load_only_run_state(git_repo)["language"] == language


@pytest.mark.parametrize("language,notice,help_text", [
    ("zh", "仅影响之后创建的新 Run", "设置新 Run 语言"),
    ("en", "Only affects future Runs", "Language for future Runs"),
])
def test_public_language_setting_help_and_validation(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    language: str, notice: str, help_text: str,
) -> None:
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    assert settings("show")[1]["language"] == "zh"
    code, saved = settings("configure", "--language", language, "--ticket-review-rounds", "7")
    assert code == 0
    assert saved["language"] == language
    assert notice in saved["notice"]
    assert saved["language_source"] == "user-defaults"
    assert settings("show")[1]["language"] == language
    assert UserDefaultsStore().load()["policy"]["ticket_review_rounds"] == 7
    with pytest.raises(SystemExit) as exited:
        cli.main(["settings", "configure", "--help"])
    assert exited.value.code == 0
    assert help_text in capsys.readouterr().out
    previous = UserDefaultsStore().path.read_bytes()
    code, error = settings("configure", "--language", "fr")
    assert code == 2
    assert "language" in json.dumps(error, ensure_ascii=False)
    assert ("must be" if language == "en" else "必须是") in json.dumps(error, ensure_ascii=False)
    assert UserDefaultsStore().path.read_bytes() == previous
    settings("configure", "--development-model", "preserved-language")
    assert settings("show")[1]["language"] == language


@pytest.mark.parametrize("value", [None, False, 1, "fr", [], {}])
def test_language_file_validation_preserves_original(value: object) -> None:
    store = UserDefaultsStore()
    store.path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps({"language": value})
    store.path.write_text(raw)
    code, error = settings("show")
    assert code == 2
    assert "language" in json.dumps(error)
    assert store.path.read_text() == raw


def test_short_copy_selects_frozen_run_or_personal_language() -> None:
    from agent_run.messages import selected_language, text

    assert selected_language() == "zh"
    UserDefaultsStore().configure(language="en")
    assert selected_language() == "en"
    assert selected_language({"language": "zh"}) == "zh"
    assert text("settings.personal_title", language=selected_language()) == "Personal Run defaults"
    assert text("settings.personal_title", language=selected_language({"language": "zh"})) == "个人运行默认配置"


@pytest.mark.parametrize("language", ["zh", "en"])
def test_cli_help_settings_errors_and_method_receipt_follow_personal_language(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], language: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    assert cli.main(["settings", "configure", "--language", language]) == 0
    configured = capsys.readouterr().out
    assert ("个人运行默认配置" if language == "zh" else "Personal Run defaults") in configured
    assert ("语言: " if language == "zh" else "Language: ") + language in configured
    for arguments in (["--help"], ["run", "--help"], ["settings", "configure", "--help"]):
        with pytest.raises(SystemExit) as exit_info:
            cli.main(arguments)
        assert exit_info.value.code == 0
        output = capsys.readouterr().out
        assert ("用法:" if language == "zh" else "usage:") in output
        assert ("显示此帮助并退出" if language == "zh" else "show this help message and exit") in output
        if language == "en":
            assert not re.search(r"[\u4e00-\u9fff]", output)
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["run", "0"])
    assert exit_info.value.code == 2
    assert ("必须是正整数" if language == "zh" else "Must be a positive integer") in capsys.readouterr().err
    with pytest.raises(SystemExit):
        cli.main(["status", "--nonexistent"])
    assert ("无法识别的参数" if language == "zh" else "unrecognized arguments") in capsys.readouterr().err
    assert cli.main(["prompts", "init"]) == 0
    initialized = capsys.readouterr().out
    assert ("已创建:" if language == "zh" else "Created:") in initialized
    assert "development.md" in initialized
    assert cli.main(["run", "1", "--repo", "invalid-repository"]) == 2
    failed = capsys.readouterr().out
    assert ("命令状态: 未执行" if language == "zh" else "Command status: Not executed") in failed
    if language == "en":
        assert not re.search(r"[\u4e00-\u9fff]", failed)


@pytest.mark.parametrize("language", ["zh", "en"])
def test_run_configuration_receipt_keeps_frozen_language_after_personal_change(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], language: str,
) -> None:
    from conftest import seed_run
    from support.inprocess_cli import invoke_cli_inprocess
    from test_run_lifecycle import _file_snapshot

    monkeypatch.chdir(git_repo)
    UserDefaultsStore().configure(language=language)
    fixture = write_fixture(git_repo / "github.json", issues={})
    started = seed_run(git_repo, fixture)
    run_id = stdout_json(started)["run_id"]
    UserDefaultsStore().configure(language="zh" if language == "en" else "en")
    result = invoke_cli_inprocess(git_repo, fixture, "configure", run_id, "--development-model", "custom-model")
    assert result.returncode == 0, result.stdout + result.stderr
    assert ("配置状态: 已保存" if language == "zh" else "Configuration: Agent Profile Revision") in result.stdout
    assert ("未启动、停止或推进" if language == "zh" else "was not started, stopped, or advanced") in result.stdout
    before = _file_snapshot(managed_state(git_repo))
    assert cli.main(["settings", "show", "--run", run_id]) == 0
    shown = capsys.readouterr().out
    assert ("执行配置:" if language == "zh" else "Execution profile:") in shown
    assert "custom-model" in shown
    assert _file_snapshot(managed_state(git_repo)) == before


@pytest.mark.parametrize("language", ["zh", "en"])
@pytest.mark.parametrize("arguments,expected_zh,expected_en,audit", [
    ([], "需要至少一个显式配置选项", "requires an explicit option", "configure requires an explicit option"),
    (["--notification-open-id", ""], "必须是非空字符串", "must be a non-empty string", "notifications.open_id 必须是非空字符串"),
])
def test_configuration_errors_localize_without_changing_audit_or_file(
    capsys: pytest.CaptureFixture[str], language: str, arguments: list[str],
    expected_zh: str, expected_en: str, audit: str,
) -> None:
    store = UserDefaultsStore()
    store.configure(language=language)
    before = store.path.read_bytes()
    assert cli.main(["settings", "configure", *arguments]) == 2
    output = capsys.readouterr().out
    assert (expected_zh if language == "zh" else expected_en) in output
    assert cli.main(["settings", "configure", *arguments, "--json"]) == 2
    diagnostic = json.loads(capsys.readouterr().out)["diagnostics"][0]
    assert diagnostic["message"] == audit + "；请排除上述原因后重试；已有交付请先查询 status 确认状态。"
    assert store.path.read_bytes() == before


@pytest.mark.parametrize("language", ["zh", "en"])
@pytest.mark.parametrize("arguments,expected_zh,expected_en,audit", [
    (["--preset", "bad"], "未知 Agent 执行预设 'bad'", "unknown Agent Execution Preset 'bad'",
     "unknown Agent Execution Preset 'bad'; choose one of: economy, premium"),
    (["--development-effort", "bad"], "development_effort 必须是以下值之一", "development_effort must be one of",
     "development_effort must be one of: high, low, max, medium, minimal, ultra, xhigh"),
    (["--development-deadline", "bad"], "development deadline 必须是正时长", "development deadline must be a positive duration",
     "development deadline must be a positive duration"),
    (["--development-model", ""], "development_model 必须是非空模型标识", "development_model must be a non-empty model identifier",
     "development_model must be a non-empty model identifier"),
    (["--publication-from-development", "--publication-model", "custom"], "不能与发布角色的覆盖配置同时使用", "cannot be combined with Publication overrides",
     "publication_from_development cannot be combined with Publication overrides"),
])
def test_profile_and_policy_cli_errors_are_bilingual_and_keep_original_audit(
    capsys: pytest.CaptureFixture[str], language: str, arguments: list[str],
    expected_zh: str, expected_en: str, audit: str,
) -> None:
    store = UserDefaultsStore()
    store.configure(language=language)
    before = store.path.read_bytes()
    assert cli.main(["settings", "configure", *arguments]) == 2
    output = capsys.readouterr().out
    assert (expected_zh if language == "zh" else expected_en) in output
    assert cli.main(["settings", "configure", *arguments, "--json"]) == 2
    diagnostic = json.loads(capsys.readouterr().out)["diagnostics"][0]
    assert diagnostic["message"] == audit + "；请排除上述原因后重试；已有交付请先查询 status 确认状态。"
    assert store.path.read_bytes() == before
