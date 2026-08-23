from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from agent_run.git import GitRepository
from agent_run.github import GitHubReadError
from agent_run.github_publish import GhGitHubPublisher


@pytest.fixture
def publisher(git_repo: Path) -> GhGitHubPublisher:
    return GhGitHubPublisher("example/project", GitRepository(git_repo))


def _project_gh_check_fields(
    arguments: tuple[str, ...], checks: list[dict[str, object]]
) -> subprocess.CompletedProcess[str]:
    """Model gh's contract: --json returns only the requested fields."""

    fields = arguments[arguments.index("--json") + 1].split(",")
    projected = [
        {field: check[field] for field in fields if field in check}
        for check in checks
    ]
    return subprocess.CompletedProcess(arguments, 0, json.dumps(projected), "")


def _stub_check_projection(
    publisher: GhGitHubPublisher,
    monkeypatch: pytest.MonkeyPatch,
    *,
    checks: list[dict[str, object]],
    live_pull_request: dict[str, object],
    required_contexts: dict[str, int | None],
) -> list[tuple[str, ...]]:
    calls: list[tuple[str, ...]] = []

    def fake_run(*arguments: str) -> subprocess.CompletedProcess[str]:
        calls.append(arguments)
        return _project_gh_check_fields(arguments, checks)

    monkeypatch.setattr(publisher, "_run", fake_run)
    monkeypatch.setattr(
        publisher,
        "live_pull_request",
        lambda _pr: dict(live_pull_request),
    )
    monkeypatch.setattr(
        publisher,
        "_required_check_contexts",
        lambda _branch: dict(required_contexts),
    )
    return calls


def test_required_checks_falls_back_when_gh_omits_ruleset_requirements(
    publisher: GhGitHubPublisher,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_run(*arguments: str) -> subprocess.CompletedProcess[str]:
        calls.append(arguments)
        if "--required" in arguments:
            return subprocess.CompletedProcess(
                arguments, 1, "", "no required checks reported on the 'ticket' branch"
            )
        if (
            arguments[0:1] == ("api",)
            and arguments[1].startswith(
                "repos/example/project/branches/agent-run%2Frun-1%2Frun/"
            )
            and arguments[1].endswith(
                "/protection/required_status_checks/contexts"
            )
        ):
            return subprocess.CompletedProcess(arguments, 404, "", "Not Found")
        if arguments[:2] == ("api", "repos/example/project/rulesets"):
            return subprocess.CompletedProcess(
                arguments,
                0,
                '[[{"id":7,"target":"branch","enforcement":"active"}]]',
                "",
            )
        if arguments[:2] == ("api", "repos/example/project/rulesets/7"):
            return subprocess.CompletedProcess(
                arguments,
                0,
                (
                    '{"conditions":{"ref_name":{"include":["refs/heads/agent-run/**"],'
                    '"exclude":[]}},"rules":[{"type":"required_status_checks",'
                    '"parameters":{"required_status_checks":[{"context":"test"}]}}]}'
                ),
                "",
            )
        if "description" in arguments[-1]:
            return _project_gh_check_fields(
                arguments,
                [
                    {
                        "name": "test",
                        "bucket": "fail",
                        "link": "required-link",
                        "workflow": "tests",
                        "description": "required failure",
                    },
                    {
                        "name": "optional",
                        "bucket": "fail",
                        "link": "optional-link",
                        "workflow": "optional",
                        "description": "optional failure",
                    },
                ],
            )
        return _project_gh_check_fields(
            arguments,
            [
                {"name": "test", "bucket": "pass"},
                {"name": "optional", "bucket": "fail"},
            ],
        )

    monkeypatch.setattr(publisher, "_run", fake_run)
    monkeypatch.setattr(
        publisher,
        "live_pull_request",
        lambda _pr: {"base_branch": "agent-run/run-1/run"},
    )

    assert publisher.required_checks(12) == "pass"
    assert publisher.required_check_evidence(12) == {
        "pr_number": 12,
        "checks": [
            {
                "name": "test",
                "bucket": "fail",
                "link": "required-link",
                "workflow": "tests",
                "description": "required failure",
            }
        ],
    }
    check_calls = [call for call in calls if call[:2] == ("pr", "checks")]
    assert "--required" in check_calls[0]
    assert "--required" not in check_calls[1]
    for call in check_calls:
        fields = call[call.index("--json") + 1].split(",")
        assert fields.count("name") == 1


def test_required_checks_empty_result_uses_the_same_projection_for_fallback(
    publisher: GhGitHubPublisher,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_run(*arguments: str) -> subprocess.CompletedProcess[str]:
        calls.append(arguments)
        checks = (
            []
            if "--required" in arguments
            else [{"name": "quality", "bucket": "pass"}]
        )
        return _project_gh_check_fields(arguments, checks)

    monkeypatch.setattr(publisher, "_run", fake_run)
    monkeypatch.setattr(
        publisher,
        "live_pull_request",
        lambda _pr: {"base_branch": "main"},
    )
    monkeypatch.setattr(
        publisher,
        "_required_check_contexts",
        lambda _branch: {"quality": None},
    )

    assert publisher.required_checks(12) == "pass"
    check_calls = [call for call in calls if call[:2] == ("pr", "checks")]
    assert len(check_calls) == 2
    assert "--required" in check_calls[0]
    assert "--required" not in check_calls[1]
    assert [call[call.index("--json") + 1] for call in check_calls] == [
        "bucket,name",
        "bucket,name",
    ]


@pytest.mark.parametrize(
    ("checks", "expected"),
    [
        ([{"name": "quality", "bucket": "pass"}], "pass"),
        ([{"name": "quality", "bucket": "fail"}], "fail"),
        ([{"name": "optional", "bucket": "pass"}], "pending"),
        (
            [
                {"name": "quality", "bucket": "pass"},
                {"name": "optional", "bucket": "fail"},
            ],
            "pass",
        ),
    ],
)
def test_required_checks_projects_ruleset_statuses_with_faithful_gh_fields(
    publisher: GhGitHubPublisher,
    monkeypatch: pytest.MonkeyPatch,
    checks: list[dict[str, object]],
    expected: str,
) -> None:
    calls = _stub_check_projection(
        publisher,
        monkeypatch,
        checks=checks,
        live_pull_request={"base_branch": "main"},
        required_contexts={"quality": None},
    )

    assert publisher.required_checks(12) == expected
    requested_fields = calls[0][calls[0].index("--json") + 1].split(",")
    assert requested_fields == ["bucket", "name"]
    assert len(requested_fields) == len(set(requested_fields))


@pytest.mark.parametrize("name", [None, "", 7])
@pytest.mark.parametrize("has_required_context", [True, False])
def test_required_checks_fail_closed_when_requested_name_is_malformed(
    publisher: GhGitHubPublisher,
    monkeypatch: pytest.MonkeyPatch,
    name: object,
    has_required_context: bool,
) -> None:
    check = {"bucket": "pass"}
    if name is not None:
        check["name"] = name
    _stub_check_projection(
        publisher,
        monkeypatch,
        checks=[check],
        live_pull_request={"base_branch": "main"},
        required_contexts={"quality": None} if has_required_context else {},
    )

    with pytest.raises(GitHubReadError) as raised:
        publisher.required_checks(12)

    assert raised.value.code == "github_invalid_response"


def test_required_checks_snapshot_requests_projection_name_once(
    publisher: GhGitHubPublisher,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _stub_check_projection(
        publisher,
        monkeypatch,
        checks=[
            {
                "bucket": "pass",
                "state": "SUCCESS",
                "name": "quality",
                "link": "https://github.com/example/project/actions/runs/1/job/2",
                "workflow": "CI",
                "description": "required check passed",
            }
        ],
        live_pull_request={"head_sha": "a" * 40, "base_branch": "main"},
        required_contexts={},
    )

    snapshot = publisher.required_checks_snapshot(12, expected_head_sha="a" * 40)

    assert snapshot["result"] == "pass"
    requested_fields = calls[0][calls[0].index("--json") + 1].split(",")
    assert requested_fields == [
        "bucket",
        "state",
        "name",
        "link",
        "workflow",
        "description",
    ]
    assert requested_fields.count("name") == 1
