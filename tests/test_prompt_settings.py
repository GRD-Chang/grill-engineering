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
    assert len(json.loads(initialized.stdout)["created"]) == 4
    custom = directory / "development.md"
    custom.write_text("个人开发方法独有标记\n", encoding="utf-8")
    (directory / "acceptance.md").unlink()
    repeated = cli("init", "--json")
    assert repeated.returncode == 0, repeated.stdout + repeated.stderr
    assert json.loads(repeated.stdout)["created"] == ["acceptance.md"]
    before = {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in directory.iterdir()}
    difference = cli("diff", "--json")
    assert difference.returncode == 0, difference.stdout + difference.stderr
    assert "+个人开发方法独有标记" in difference.stdout
    assert before == {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in directory.iterdir()}
    assert custom.read_text() == "个人开发方法独有标记\n"


def test_preview_uses_execution_assembler_and_frozen_resources(tmp_path: Path) -> None:
    directory = prompt_resources.personal_method_directory()
    directory.mkdir(parents=True)
    custom = directory / "development.md"
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
    assert set(json.loads(frozen.stdout)["sources"].values()) == {"request_snapshot"}


@pytest.mark.parametrize("language", ["zh", "en"])
def test_unreadable_override_reports_error(tmp_path: Path, language: str) -> None:
    from agent_run.user_defaults import UserDefaultsStore

    UserDefaultsStore().configure(language=language)
    directory = prompt_resources.personal_method_directory()
    directory.mkdir(parents=True)
    (directory / "acceptance.md").mkdir()
    result = cli("diff", "--json")
    assert result.returncode != 0
    assert "acceptance" in result.stdout + result.stderr
    human = cli("diff")
    assert human.returncode != 0
    assert ("无法读取 Prompt 资源" if language == "zh" else "Cannot read prompt resource") in human.stdout
    assert ("普通 Markdown 文件" if language == "zh" else "regular Markdown file") in human.stdout


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


@pytest.mark.parametrize("failure", ["invalid_utf8", "broken_symlink", "fifo", "missing_builtin", "empty"])
def test_resource_read_errors_reach_public_cli(tmp_path: Path, capsys, monkeypatch, failure: str) -> None:
    from agent_run.cli import main

    directory = prompt_resources.personal_method_directory()
    directory.mkdir(parents=True)
    method = directory / "acceptance.md"
    if failure == "invalid_utf8":
        method.write_bytes(b"\xff")
    elif failure == "empty":
        method.write_text(" \n", encoding="utf-8")
    elif failure == "fifo":
        os.mkfifo(method)
    elif failure == "broken_symlink":
        method.symlink_to(tmp_path / "missing.md")
    else:
        monkeypatch.setattr(prompt_resources, "RESOURCE_ROOT", tmp_path / "missing-builtin")
    assert main(["prompts", "diff", "--json"]) != 0
    output = capsys.readouterr().out
    assert "Cannot read prompt resource" in output


@pytest.mark.parametrize("language", ["zh", "en"])
def test_language_methods_are_independent_and_preview_matches(tmp_path: Path, language: str) -> None:
    from agent_run.cli import main

    assert main(["settings", "configure", "--language", language, "--json"]) == 0
    other = "en" if language == "zh" else "zh"
    other_directory = prompt_resources.personal_method_directory(other)
    other_directory.mkdir(parents=True)
    other_method = other_directory / "acceptance.md"
    other_method.write_text("OTHER LANGUAGE CUSTOM METHOD", encoding="utf-8")
    assert prompt_resources.resolve_resources()["methods/acceptance"] == prompt_resources.builtin_resources(language)["methods/acceptance"]
    initialized = cli("init", "--json")
    assert initialized.returncode == 0, initialized.stdout + initialized.stderr
    assert len(json.loads(initialized.stdout)["created"]) == 4
    assert other_method.read_text() == "OTHER LANGUAGE CUSTOM METHOD"
    directory = prompt_resources.personal_method_directory()
    method = directory / "acceptance.md"
    method.write_text("SELECTED LANGUAGE CUSTOM METHOD", encoding="utf-8")
    before = method.stat().st_mtime_ns
    assert cli("init", "--json").returncode == 0
    assert method.stat().st_mtime_ns == before
    diff = cli("diff", "--json")
    assert diff.returncode == 0
    assert "+SELECTED LANGUAGE CUSTOM METHOD" in diff.stdout
    assert "OTHER LANGUAGE CUSTOM METHOD" not in diff.stdout
    request = {"acceptance_scope": "ticket", "language": language,
               "task_issue_url": "https://example.invalid/2"}
    request_file = tmp_path / "request.json"
    request_file.write_text(json.dumps(request))
    preview = cli("preview", "--role", "development", "--request", str(request_file), "--json")
    assert preview.returncode == 0, preview.stdout + preview.stderr
    assert json.loads(preview.stdout)["prompt"] == development_prompt(request)


def test_bilingual_resource_manifest_and_format_fields_match() -> None:
    from string import Formatter

    chinese = prompt_resources.builtin_resources("zh")
    english = prompt_resources.builtin_resources("en")
    assert chinese.keys() == english.keys()
    formatter = Formatter()
    for key in chinese:
        assert english[key].strip(), key
        # Only resources interpolated by assemblers use Python format fields;
        # JSON examples in output contracts intentionally contain literal braces.
        if "{}" in chinese[key]:
            assert [field for _, field, _, _ in formatter.parse(chinese[key])] == [
                field for _, field, _, _ in formatter.parse(english[key])
            ], key

    # Short CLI, history and notification copy shares the same resource contract.
    catalogs = [
        json.loads((prompt_resources.RESOURCE_ROOT.parent / language / "messages.json").read_text())
        for language in ("zh", "en")
    ]
    assert catalogs[0].keys() == catalogs[1].keys()
    for key, chinese_copy in catalogs[0].items():
        english_copy = catalogs[1][key]
        assert chinese_copy.strip() and english_copy.strip(), key
        assert {
            field for _, field, _, _ in formatter.parse(chinese_copy) if field is not None
        } == {
            field for _, field, _, _ in formatter.parse(english_copy) if field is not None
        }, key


@pytest.mark.parametrize("language", ["zh", "en"])
def test_legacy_files_are_preserved_and_sources_are_explicit(tmp_path: Path, language: str) -> None:
    from agent_run.user_defaults import UserDefaultsStore

    UserDefaultsStore().configure(language=language)
    directory = prompt_resources.personal_method_directory()
    directory.mkdir(parents=True)
    old_names = ("development-common", "development-initial", "development-repair", "review", "publication")
    for name in old_names:
        (directory / f"{name}.md").write_text(f"OLD {name}\n", encoding="utf-8")
    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in directory.iterdir()}
    difference = cli("diff", "--json")
    assert difference.returncode == 0, difference.stdout + difference.stderr
    result = json.loads(difference.stdout)
    assert result["legacy_files"] == {
        "development-common.md": ["development.md", "repair.md"],
        "development-initial.md": ["development.md"],
        "development-repair.md": ["repair.md"],
        "review.md": ["acceptance.md"], "publication.md": ["publishing.md"],
    }
    assert all(not value for value in result["differences"].values())
    assert all(source.startswith(f"builtin/{language}/") for source in result["sources"].values())
    assert ("手动" if language == "zh" else "manually") in result["notice"]
    request_file = tmp_path / "request.json"
    request_file.write_text(json.dumps({"language": language, "task_issue_url": "https://example.invalid/2"}))
    preview = cli("preview", "--request", str(request_file), "--role", "review", "--json")
    assert preview.returncode == 0, preview.stdout + preview.stderr
    assert "OLD review" not in json.loads(preview.stdout)["prompt"]
    assert "review.md" in preview.stderr
    assert before == {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in directory.iterdir()}
    initialized = cli("init", "--json")
    assert initialized.returncode == 0, initialized.stdout + initialized.stderr
    result = json.loads(initialized.stdout)
    assert set(result["created"]) == {"development.md", "repair.md", "acceptance.md", "publishing.md"}
    assert all(source == str(directory / name) for name, source in result["sources"].items())
    assert all((directory / name).read_bytes() == content and (directory / name).stat().st_mtime_ns == modified
               for name, (content, modified) in before.items())


def test_preview_and_diff_do_not_execute_workers_or_write_runs(tmp_path: Path, monkeypatch, capsys) -> None:
    from agent_run.cli import main
    from agent_run.codex import CodexCliBackend
    from agent_run.state import StateStore

    def forbidden(*args, **kwargs):
        pytest.fail("Prompt inspection must not execute a Worker or persist Run state")

    monkeypatch.setattr(CodexCliBackend, "_invoke", forbidden)
    monkeypatch.setattr(StateStore, "save_run", forbidden)
    request_file = tmp_path / "request.json"
    request_file.write_text(json.dumps({"task_issue_url": "https://example.invalid/2"}))
    assert main(["prompts", "diff", "--json"]) == 0
    capsys.readouterr()
    assert main(["prompts", "preview", "--request", str(request_file), "--role", "development", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["prompt"]
