from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


def test_resume_without_a_managed_workspace_explains_how_to_start(tmp_path: Path) -> None:
    environment = os.environ.copy()
    environment['PYTHONPATH'] = str(Path(__file__).parents[1] / 'src')
    result = subprocess.run(
        [sys.executable, '-m', 'agent_run', 'resume', '213',
         '--repo', 'example/project', '--json'],
        cwd=tmp_path, env=environment, text=True, capture_output=True, check=False,
    )
    assert result.returncode == 2
    diagnostic = json.loads(result.stdout)['diagnostics'][0]
    assert diagnostic['code'] == 'run_locator_missing'
    assert 'Runner 工作区' in diagnostic['message']
    assert 'run' in diagnostic['message']
    assert not (tmp_path / '.agent-run').exists()


@pytest.mark.parametrize("language", ["zh", "en"])
@pytest.mark.parametrize("arguments", [[], ["run"], ["resume"], ["prompts"], ["settings", "configure"]])
def test_current_command_help_uses_personal_language_without_starting_work(
    tmp_path: Path, monkeypatch, capsys, language: str, arguments: list[str],
) -> None:
    from agent_run.config.defaults import UserDefaultsStore
    from agent_run.core.kernel import Kernel
    from agent_run.cli.main import main

    store = UserDefaultsStore()
    store.configure(language=language)
    original = store.path.read_bytes()

    def forbidden(*_args, **_kwargs):
        pytest.fail("Help must not create a Run or start a process")

    monkeypatch.setattr(Kernel, "create", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    assert main([*arguments, "--help"]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.startswith("用法:" if language == "zh" else "usage:")
    assert ("显示帮助并退出" if language == "zh" else "show this help message and exit") in captured.out
    assert store.path.read_bytes() == original
    assert not (tmp_path / ".agent-run").exists()


@pytest.mark.parametrize("language", ["zh", "en"])
def test_invalid_choice_localizes_copy_and_preserves_argument_facts(
    monkeypatch, capsys, language: str,
) -> None:
    from agent_run.config.defaults import UserDefaultsStore
    from agent_run.cli.main import main

    store = UserDefaultsStore()
    store.configure(language=language)
    original = store.path.read_bytes()
    invalid = "user-value-用户原文"
    assert main(["settings", "configure", "--development-thread-policy", invalid]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert ("无效选项" if language == "zh" else "invalid choice") in captured.err
    assert invalid in captured.err
    assert "reuse" in captured.err and "new-per-attempt" in captured.err
    assert store.path.read_bytes() == original


@pytest.mark.parametrize("language", ["zh", "en"])
@pytest.mark.parametrize("as_json", [False, True], ids=["plain", "json"])
def test_missing_run_keeps_current_code_and_explains_discovery_before_creation(
    tmp_path: Path, monkeypatch, capsys, language: str, as_json: bool,
) -> None:
    from agent_run.config.defaults import UserDefaultsStore
    from agent_run.cli.main import main

    monkeypatch.chdir(tmp_path)
    store = UserDefaultsStore()
    store.configure(language=language)
    original = store.path.read_bytes()
    arguments = ["resume", "213", "--repo", "example/project"]
    assert main([*arguments, *(["--json"] if as_json else [])]) == 2
    captured = capsys.readouterr()
    assert captured.err == ""
    if as_json:
        diagnostic = json.loads(captured.out)["diagnostics"][0]
        assert diagnostic["code"] == "run_locator_missing"
        message = diagnostic["message"]
    else:
        assert "run_locator_missing" in captured.out
        message = captured.out
    assert ("找不到匹配的 Run" if language == "zh" else "No matching Run") in message
    assert ("Runner 工作区" if language == "zh" else "Runner workspace") in message
    assert "agent-run runs --repo OWNER/NAME" in message
    assert "agent-run run PARENT --repo OWNER/NAME" in message
    assert store.path.read_bytes() == original
    assert not (tmp_path / ".agent-run").exists()
