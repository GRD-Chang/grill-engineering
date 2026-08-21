from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Agent Run Tests"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "agent-run-tests@example.invalid"],
        cwd=repo,
        check=True,
    )
    (repo / "README.md").write_text("# fixture\n", encoding="utf-8")
    (repo / "pyproject.toml").write_text(
        "[tool.agent-run.required-checks]\n"
        'code-failure-steps = [\n'
        '  "fixture-ci::fixture-required-check::Run tests",\n'
        '  "CI::quality::Run tests",\n'
        ']\n',
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "README.md", "pyproject.toml"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True, capture_output=True)
    return repo


def write_fixture(path: Path, *, issues: dict[str, Any], **overrides: Any) -> Path:
    default_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=path.parent,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    fixture: dict[str, Any] = {
        "repository": "example/project",
        "default_branch": "main",
        "default_head_sha": default_head,
        "parent": {
            "number": 1,
            "title": "Parent spec",
            "body": "Deliver the ticket set.",
            "sub_issues": [int(number) for number in issues],
            "sub_issue_order_reliable": True,
        },
        "issues": issues,
    }
    fixture.update(overrides)
    path.write_text(json.dumps(fixture), encoding="utf-8")
    return path
