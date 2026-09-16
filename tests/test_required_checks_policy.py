from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from agent_run.git import GitRepository
from agent_run.github_fixture import FixtureGitHubPublisher
from agent_run.required_checks import annotate_configured_code_failures


def _git(root: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments], cwd=root, check=True, text=True, capture_output=True
    ).stdout.strip()


def _commit_policy(root: Path, value: str | None) -> str:
    path = root / "pyproject.toml"
    if value is None:
        path.unlink(missing_ok=True)
    else:
        path.write_text(value)
    _git(root, "add", "--all", "pyproject.toml")
    _git(root, "commit", "-qm", "change check policy")
    return _git(root, "rev-parse", "HEAD")


def _policy(step: str) -> str:
    return '[tool.agent-run.required-checks]\ncode-failure-steps = ["CI::quality::' + step + '"]\n'


def _check(head: str, step: str = "New tests") -> dict[str, Any]:
    return {
        "name": "quality", "workflow": "CI", "repairability": "untrusted annotation",
        "job": {
            "name": "quality", "workflow_name": "CI", "head_sha": head,
            "status": "completed", "conclusion": "failure",
            "steps": [{"name": step, "status": "completed", "conclusion": "failure"}],
        },
    }


def test_policy_is_bound_to_fetched_head_without_updating_root_checkout(
    git_repo: Path, tmp_path: Path,
) -> None:
    old_head = _commit_policy(git_repo, _policy("Old tests"))
    managed = tmp_path / "managed"
    _git(tmp_path, "clone", "--no-local", "--quiet", str(git_repo), str(managed))
    new_head = _commit_policy(git_repo, _policy("New tests"))
    assert GitRepository(managed).resolve_base("main", new_head) == new_head
    assert _git(managed, "rev-parse", "HEAD") == old_head
    assert (managed / "pyproject.toml").read_text() == _policy("Old tests")

    result = annotate_configured_code_failures([_check(new_head)], managed, expected_head_sha=new_head)
    assert result[0]["repairability"] == "code_failure"
    old_result = annotate_configured_code_failures(
        [_check(old_head, "Old tests")], managed, expected_head_sha=old_head,
    )
    assert old_result[0]["repairability"] == "code_failure"
    assert "repairability" not in annotate_configured_code_failures(
        [_check(new_head, "Old tests")], managed, expected_head_sha=new_head,
    )[0]
    assert _git(managed, "status", "--porcelain") == ""
    assert _git(managed, "rev-parse", "HEAD") == old_head


@pytest.mark.parametrize("policy", [None, "[project]\nname = 'no-policy'\n"])
def test_missing_policy_never_uses_worktree_configuration(git_repo: Path, policy: str | None) -> None:
    head = _commit_policy(git_repo, policy)
    (git_repo / "pyproject.toml").write_text(_policy("New tests"))
    result = annotate_configured_code_failures([_check(head)], git_repo, expected_head_sha=head)
    assert "repairability" not in result[0]


@pytest.mark.parametrize("head", ["f" * 40, None])
def test_unavailable_head_never_uses_worktree_configuration(git_repo: Path, head: str | None) -> None:
    (git_repo / "pyproject.toml").write_text(_policy("New tests"))
    result = annotate_configured_code_failures([_check(head or "unknown")], git_repo, expected_head_sha=head)
    assert "repairability" not in result[0]


def test_invalid_versioned_policy_preserves_configuration_error(git_repo: Path) -> None:
    head = _commit_policy(git_repo, '[tool.agent-run.required-checks]\ncode-failure-steps = "invalid"\n')
    (git_repo / "pyproject.toml").write_text(_policy("New tests"))
    with pytest.raises(ValueError, match="workflow::name::step"):
        annotate_configured_code_failures([_check(head)], git_repo, expected_head_sha=head)


def test_fixture_publisher_reads_the_repository_head_policy_not_fixture_directory(
    git_repo: Path, tmp_path: Path,
) -> None:
    head = _commit_policy(git_repo, _policy("New tests"))
    fixture = tmp_path / "github.json"
    fixture.write_text(json.dumps({
        "repository": "owner/project",
        "delivery": {
            "pull_requests": [{
                "number": 1, "branch": "main", "base_branch": "main",
                "head_sha": head, "state": "OPEN",
            }],
            "required_check_evidence": {"checks": [_check(head)]},
        },
    }))
    (tmp_path / "pyproject.toml").write_text(_policy("Old tests"))
    (git_repo / "pyproject.toml").write_text(_policy("Old tests"))
    publisher = FixtureGitHubPublisher(fixture, GitRepository(git_repo))
    evidence = publisher.required_check_evidence(1, expected_head_sha=head)
    assert evidence["checks"][0]["repairability"] == "code_failure"
