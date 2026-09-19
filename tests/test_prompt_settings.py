"""Public CLI process boundary for global Markdown customization and preview."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from agent_run import prompt_resources
from agent_run.development_prompts import development_prompt


def cli(*arguments: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(Path(__file__).parents[1] / "src")
    return subprocess.run(
        [sys.executable, "-m", "agent_run", "prompts", *arguments],
        text=True, capture_output=True, check=False, env=environment, timeout=15,
    )


def test_init_preserves_edits_and_diff_is_read_only(tmp_path: Path) -> None:
    directory = prompt_resources.personal_method_directory()
    empty = cli("diff", "--json")
    assert empty.returncode == 0, empty.stdout + empty.stderr
    assert not directory.exists()
    initialized = cli("init", "--json")
    assert initialized.returncode == 0, initialized.stdout + initialized.stderr
    assert len(json.loads(initialized.stdout)["created"]) == 5
    custom = directory / "development-common.md"
    custom.write_text("个人开发方法独有标记\n", encoding="utf-8")
    (directory / "review.md").unlink()
    repeated = cli("init", "--json")
    assert repeated.returncode == 0, repeated.stdout + repeated.stderr
    assert json.loads(repeated.stdout)["created"] == ["review.md"]
    before = {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in directory.iterdir()}
    difference = cli("diff", "--json")
    assert difference.returncode == 0, difference.stdout + difference.stderr
    assert "+个人开发方法独有标记" in difference.stdout
    assert before == {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in directory.iterdir()}
    assert custom.read_text() == "个人开发方法独有标记\n"


def test_preview_uses_execution_assembler_and_frozen_resources(tmp_path: Path) -> None:
    directory = prompt_resources.personal_method_directory()
    directory.mkdir(parents=True)
    custom = directory / "development-common.md"
    custom.write_text("个人共用标记\n", encoding="utf-8")
    request = {"acceptance_scope": "ticket", "task_issue_url": "https://example.invalid/2",
               "parent_issue_url": "https://example.invalid/1"}
    request_file = tmp_path / "request.json"
    request_file.write_text(json.dumps(request))
    preview = cli("preview", "--request", str(request_file), "--role", "development", "--json")
    assert preview.returncode == 0, preview.stdout + preview.stderr
    assert json.loads(preview.stdout)["prompt"] == development_prompt(request)
    assert "个人共用标记" in preview.stdout
    request["_prompt_resources"] = prompt_resources.resolve_resources()
    request_file.write_text(json.dumps(request))
    custom.write_text("新Run标记\n", encoding="utf-8")
    frozen = cli("preview", "--request", str(request_file), "--role", "development", "--json")
    assert frozen.returncode == 0, frozen.stdout + frozen.stderr
    assert "个人共用标记" in frozen.stdout
    assert "新Run标记" not in frozen.stdout


def test_unreadable_override_reports_error(tmp_path: Path) -> None:
    directory = prompt_resources.personal_method_directory()
    directory.mkdir(parents=True)
    (directory / "review.md").mkdir()
    result = cli("diff", "--json")
    assert result.returncode != 0
    assert "review" in result.stdout + result.stderr


def test_output_repair_preview_is_read_only_and_snapshot_is_required_when_supplied(
    tmp_path: Path, capsys,
) -> None:
    from agent_run.cli import main
    from agent_run.prompt_context import structured_output_repair_prompt

    request = {"output_name": "Development result", "contract_error": "字段缺失"}
    request_file = tmp_path / "request.json"
    request_file.write_text(json.dumps(request))
    arguments = ["prompts", "preview", "--request", str(request_file),
                 "--role", "output-repair", "--json"]
    assert main(arguments) == 0
    prompt = json.loads(capsys.readouterr().out)["prompt"]
    assert prompt == structured_output_repair_prompt("Development result", "字段缺失")
    assert "字段缺失" in prompt
    assert not prompt_resources.personal_method_directory().exists()
    request["_prompt_resources"] = {}
    request_file.write_text(json.dumps(request))
    assert main(arguments) != 0
    assert "snapshot" in capsys.readouterr().out


@pytest.mark.parametrize("failure", ["invalid_utf8", "broken_symlink", "fifo", "missing_builtin"])
def test_resource_read_errors_reach_public_cli(tmp_path: Path, capsys, monkeypatch, failure: str) -> None:
    from agent_run.cli import main

    directory = prompt_resources.personal_method_directory()
    directory.mkdir(parents=True)
    method = directory / "review.md"
    if failure == "invalid_utf8":
        method.write_bytes(b"\xff")
    elif failure == "fifo":
        os.mkfifo(method)
    elif failure == "broken_symlink":
        method.symlink_to(tmp_path / "missing.md")
    else:
        monkeypatch.setattr(prompt_resources, "RESOURCE_ROOT", tmp_path / "missing-builtin")
    assert main(["prompts", "diff", "--json"]) != 0
    assert "Cannot read prompt resource" in capsys.readouterr().out
