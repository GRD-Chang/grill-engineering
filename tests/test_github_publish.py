from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from agent_run.git import GitError, GitRepository
from agent_run.github import GitHubReadError
from agent_run.github_publish import (
    GhGitHubPublisher,
    _matches_ref,
    _render_agent_run_status,
)
from agent_run.required_checks import is_explicitly_repairable_code_failure


def test_live_pull_request_uses_the_requested_repository_as_its_base(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publisher = GhGitHubPublisher("example/project", GitRepository(git_repo))
    calls: list[tuple[str, ...]] = []

    def fake_json(*arguments: str, **_kwargs: object) -> object:
        calls.append(arguments)
        return {
            "headRefName": "ticket-1",
            "headRefOid": "a" * 40,
            "headRepository": {"nameWithOwner": "example/project"},
            "baseRefName": "main",
            "baseRefOid": "b" * 40,
            "mergeable": "MERGEABLE",
            "state": "OPEN",
            "mergeCommit": None,
        }

    monkeypatch.setattr(publisher, "_json", fake_json)

    live = publisher.live_pull_request(12)

    assert live["base_repository"] == "example/project"
    assert "baseRepository" not in calls[0][-1]


def test_real_publisher_applies_repository_owned_check_repairability(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (git_repo / "pyproject.toml").write_text(
        "[tool.agent-run.required-checks]\n"
        'code-failure-steps = ["tests::unit::Run tests"]\n',
        encoding="utf-8",
    )
    publisher = GhGitHubPublisher("example/project", GitRepository(git_repo))
    monkeypatch.setattr(
        publisher,
        "_checks",
        lambda _pr, _fields: [
            {
                "name": "unit",
                "workflow": "tests",
                "bucket": "fail",
                "state": "FAILURE",
                "description": "Tests failed.",
                "link": (
                    "https://github.com/example/project/actions/runs/22/job/33"
                ),
            }
        ],
    )
    monkeypatch.setattr(
        publisher,
        "live_pull_request",
        lambda _pr: {"head_sha": "a" * 40},
    )
    monkeypatch.setattr(
        publisher,
        "_json",
        lambda *_args, **_kwargs: {
            "id": 33,
            "head_sha": "a" * 40,
            "name": "unit",
            "workflow_name": "tests",
            "status": "completed",
            "conclusion": "failure",
            "html_url": "https://github.com/example/project/runs/22/jobs/33",
            "steps": [
                {
                    "name": "Run tests",
                    "status": "completed",
                    "conclusion": "failure",
                    "number": 6,
                }
            ],
        },
    )

    evidence = publisher.required_check_evidence(12, expected_head_sha="a" * 40)

    assert evidence["checks"][0]["repairability"] == "code_failure"
    assert is_explicitly_repairable_code_failure(evidence) is True


def test_real_publisher_does_not_mark_configured_job_platform_failure_repairable(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (git_repo / "pyproject.toml").write_text(
        "[tool.agent-run.required-checks]\n"
        'code-failure-steps = ["CI::quality::Run tests"]\n',
        encoding="utf-8",
    )
    publisher = GhGitHubPublisher("example/project", GitRepository(git_repo))
    monkeypatch.setattr(
        publisher,
        "_checks",
        lambda _pr, _fields: [
            {
                "name": "quality",
                "workflow": "CI",
                "bucket": "fail",
                "state": "FAILURE",
                "description": "The job failed.",
                "link": "https://github.com/example/project/actions/runs/22/job/33",
            }
        ],
    )
    monkeypatch.setattr(
        publisher,
        "live_pull_request",
        lambda _pr: {"head_sha": "a" * 40},
    )
    monkeypatch.setattr(
        publisher,
        "_json",
        lambda *_args, **_kwargs: {
            "id": 33,
            "head_sha": "a" * 40,
            "name": "quality",
            "workflow_name": "CI",
            "status": "completed",
            "conclusion": "failure",
            "html_url": "https://github.com/example/project/runs/22/jobs/33",
            "steps": [
                {
                    "name": "Install system dependencies",
                    "status": "completed",
                    "conclusion": "failure",
                    "number": 3,
                },
                {
                    "name": "Run tests",
                    "status": "completed",
                    "conclusion": "skipped",
                    "number": 6,
                },
            ],
        },
    )

    evidence = publisher.required_check_evidence(12, expected_head_sha="a" * 40)

    assert "repairability" not in evidence["checks"][0]
    assert is_explicitly_repairable_code_failure(evidence) is False


def test_supersession_status_renders_the_receipt_fields() -> None:
    rendered = _render_agent_run_status(
        {
            "scope": "superseded_generation",
            "generation": 2,
            "retirement": "closed",
            "close_nonce": "nonce-123",
            "next_action": "superseded by explicit requeue",
        }
    )

    assert (
        "## Superseded Change Generation\n\n- Scope: `superseded_generation`"
        in rendered
    )
    assert "- Generation: `2`" in rendered
    assert "- Retirement: `closed`" in rendered
    assert "- Close nonce: `nonce-123`" in rendered


def test_supersession_receipt_recognizes_its_rendered_comment(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publisher = GhGitHubPublisher("example/project", GitRepository(git_repo))
    comment = {
        "body": _render_agent_run_status(
            {
                "scope": "superseded_generation",
                "generation": 2,
                "retirement": "closing",
                "close_nonce": "nonce-123",
                "next_action": "superseded by explicit requeue",
            }
        ),
        "user": {"login": "agent-run-bot"},
    }
    monkeypatch.setattr(
        publisher, "live_pull_request", lambda _number: {"state": "CLOSED"}
    )

    def fake_json(*arguments: str, **_kwargs: object) -> object:
        if arguments == ("api", "user"):
            return {"login": "agent-run-bot"}
        if "comments" in arguments[1]:
            return [[comment]]
        if "events" in arguments[1]:
            return [
                [
                    {
                        "id": 10,
                        "event": "closed",
                        "created_at": "2026-01-01T00:00:00Z",
                        "actor": {"login": "agent-run-bot"},
                    }
                ]
            ]
        raise AssertionError(arguments)

    monkeypatch.setattr(publisher, "_json", fake_json)

    assert publisher.has_supersession_close_intent(12, 2, "nonce-123")
    assert publisher.has_supersession_close_receipt(12, 2, "nonce-123")


def test_supersession_status_scan_flattens_comment_pages(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publisher = GhGitHubPublisher("example/project", GitRepository(git_repo))
    calls: list[tuple[str, ...]] = []

    def fake_json(*arguments: str, **_kwargs: object) -> object:
        calls.append(arguments)
        if "comments" in arguments[1]:
            return [[{"id": 9, "body": "ordinary", "user": {"login": "x"}}], []]
        return {"id": 10}

    monkeypatch.setattr(publisher, "_json", fake_json)
    publisher.record_agent_run_status(
        12,
        {
            "scope": "superseded_generation",
            "generation": 2,
            "retirement": "closing",
            "close_nonce": "nonce-123",
            "next_action": "superseded by explicit requeue",
        },
    )

    assert "--slurp" in calls[0]


def test_publish_branch_refuses_remote_drift(
    git_repo: Path,
    tmp_path: Path,
) -> None:
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "clone", "--bare", str(git_repo), str(remote)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "remote", "add", "origin", str(remote)],
        cwd=git_repo,
        check=True,
    )
    base = _rev_parse(git_repo, "HEAD")
    subprocess.run(
        ["git", "push", "origin", f"{base}:refs/heads/ticket"],
        cwd=git_repo,
        check=True,
        capture_output=True,
    )
    (git_repo / "candidate.txt").write_text("candidate\n", encoding="utf-8")
    subprocess.run(["git", "add", "candidate.txt"], cwd=git_repo, check=True)
    subprocess.run(
        ["git", "commit", "-m", "candidate"],
        cwd=git_repo,
        check=True,
        capture_output=True,
    )
    candidate = _rev_parse(git_repo, "HEAD")
    updater = tmp_path / "updater"
    subprocess.run(
        ["git", "clone", str(remote), str(updater)],
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "config", "user.name", "Updater"], cwd=updater, check=True)
    subprocess.run(
        ["git", "config", "user.email", "updater@example.invalid"],
        cwd=updater,
        check=True,
    )
    subprocess.run(
        ["git", "checkout", "ticket"], cwd=updater, check=True, capture_output=True
    )
    (updater / "external.txt").write_text("external\n", encoding="utf-8")
    subprocess.run(["git", "add", "external.txt"], cwd=updater, check=True)
    subprocess.run(
        ["git", "commit", "-m", "external drift"],
        cwd=updater,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "push", "origin", "ticket"],
        cwd=updater,
        check=True,
        capture_output=True,
    )
    drift = _rev_parse(updater, "HEAD")

    with pytest.raises(GitError):
        GhGitHubPublisher("example/project", GitRepository(git_repo)).publish_branch(
            "ticket",
            candidate,
            expected_remote_sha=base,
        )

    assert _remote_head(git_repo, "ticket") == drift


def test_close_parent_issue_passes_repository_to_gh(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publisher = GhGitHubPublisher("example/project", GitRepository(git_repo))
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        publisher,
        "_json",
        lambda *_arguments: {"state": "OPEN", "comments": []},
    )
    monkeypatch.setattr(
        publisher, "_require", lambda *arguments: calls.append(arguments)
    )

    publisher.close_parent_issue(
        parent_number=1,
        run_id="run-1",
        pr_number=2,
        integrated_sha="abc123",
        delivery_type="Final Run",
    )

    assert calls[-1] == ("issue", "close", "1", "--repo", "example/project")


@pytest.mark.parametrize(
    ("intent_author", "closing_actor", "expected"),
    [
        ("agent-run-bot", "agent-run-bot", True),
        ("agent-run-bot", "maintainer", False),
        ("maintainer", "maintainer", False),
    ],
)
def test_ticket_close_ownership_requires_publisher_close_event(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    intent_author: str,
    closing_actor: str,
    expected: bool,
) -> None:
    publisher = GhGitHubPublisher("example/project", GitRepository(git_repo))

    def fake_json(*arguments: str) -> object:
        if arguments[:2] == ("api", "user"):
            return {"login": "agent-run-bot"}
        if arguments[:2] == ("issue", "view"):
            return {
                "state": "CLOSED",
                "updatedAt": "2026-08-11T10:00:00Z",
                "comments": [
                    {
                        "body": (
                            "<!-- agent-run:run-1:ticket-2:completed -->\n"
                            "<!-- agent-run:run-1:ticket-2:"
                            "publisher-close-intent:pr-3:sha-abc123 -->\n"
                            "<!-- agent-run:run-1:ticket-2:"
                            "publisher-close-baseline:pr-3:sha-abc123:0 -->"
                        ),
                        "createdAt": "2026-08-11T10:00:00Z",
                        "author": {"login": intent_author},
                    }
                ],
            }
        if arguments[:2] == (
            "api",
            "repos/example/project/issues/2/events",
        ):
            return [
                {
                    "id": 99,
                    "event": "closed",
                    "created_at": "2026-08-11T10:00:00Z",
                    "actor": {"login": closing_actor},
                }
            ]
        raise AssertionError(arguments)

    monkeypatch.setattr(publisher, "_json", fake_json)
    monkeypatch.setattr(publisher, "_require", lambda *arguments: None)

    assert (
        publisher.prepare_primary_ticket_close(
            ticket_number=2,
            run_id="run-1",
            pr_number=3,
            integrated_sha="abc123",
        )
        is not None
    ) is expected


def test_successful_close_waits_for_exact_event_before_completion(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publisher = GhGitHubPublisher("example/project", GitRepository(git_repo))
    calls: list[tuple[str, ...]] = []
    comments: list[dict[str, object]] = []
    events: list[dict[str, object]] = []
    closed = False
    projection_lagged = False

    def fake_json(*arguments: str) -> object:
        if arguments[:2] == ("issue", "view"):
            return {
                "state": "CLOSED" if closed else "OPEN",
                "updatedAt": (
                    "2026-08-11T10:00:01Z"
                    if closed
                    else (
                        "2026-08-11T10:00:03Z"
                        if projection_lagged
                        else "2026-08-11T10:00:00Z"
                    )
                ),
                "comments": comments,
            }
        if arguments[:2] == ("api", "user"):
            return {"login": "agent-run-bot"}
        if arguments[:2] == (
            "api",
            "repos/example/project/issues/2/events",
        ):
            return events
        raise AssertionError(arguments)

    def fake_require(*arguments: str) -> None:
        nonlocal closed
        calls.append(arguments)
        if arguments[:2] == ("issue", "comment"):
            comments.append(
                {
                    "body": arguments[-1],
                    "createdAt": "2026-08-11T10:00:00Z",
                    "author": {"login": "agent-run-bot"},
                }
            )
        if arguments[:2] == ("issue", "close"):
            closed = True

    monkeypatch.setattr(publisher, "_json", fake_json)
    monkeypatch.setattr(publisher, "_require", fake_require)

    intent = publisher.prepare_primary_ticket_close(
        ticket_number=2,
        run_id="run-1",
        pr_number=3,
        integrated_sha="abc123",
    )
    assert intent == {
        "actor": "agent-run-bot",
        "event_id": None,
        "intent_created_at": "2026-08-11T10:00:00Z",
        "baseline_event_id": 0,
        "intent_binding": "pr-3:sha-abc123",
    }
    projection_lagged = True
    with pytest.raises(GitHubReadError, match="close event"):
        publisher.close_primary_ticket(
            ticket_number=2,
            run_id="run-1",
            pr_number=3,
            integrated_sha="abc123",
            close_intent=intent,
        )
    assert calls[-1] == ("issue", "close", "2", "--repo", "example/project")
    events.append(
        {
            "id": 101,
            "event": "closed",
            "created_at": "2026-08-11T10:00:01Z",
            "actor": {"login": "agent-run-bot"},
        }
    )
    assert publisher.close_primary_ticket(
        ticket_number=2,
        run_id="run-1",
        pr_number=3,
        integrated_sha="abc123",
        close_intent=intent,
    ) == {
        "actor": "agent-run-bot",
        "event_id": 101,
        "created_at": "2026-08-11T10:00:01Z",
        "intent_created_at": "2026-08-11T10:00:00Z",
        "baseline_event_id": 0,
        "intent_binding": "pr-3:sha-abc123",
    }


def test_close_stops_when_ticket_changes_after_intent(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publisher = GhGitHubPublisher("example/project", GitRepository(git_repo))
    calls: list[tuple[str, ...]] = []
    dispatches: list[str] = []
    issue_views = 0

    def fake_json(*arguments: str) -> object:
        nonlocal issue_views
        if arguments[:2] == ("api", "user"):
            return {"login": "agent-run-bot"}
        if arguments[:2] == (
            "api",
            "repos/example/project/issues/2/events",
        ):
            return []
        if arguments[:2] == ("issue", "view"):
            issue_views += 1
            return {
                "state": "OPEN" if issue_views == 1 else "CLOSED",
                "comments": (
                    []
                    if issue_views == 1
                    else [
                        {
                            "body": (
                                "<!-- agent-run:run-1:ticket-2:"
                                "publisher-close-intent:pr-3:sha-abc123 -->\n"
                                "<!-- agent-run:run-1:ticket-2:"
                                "publisher-close-baseline:pr-3:sha-abc123:0 -->"
                            ),
                            "createdAt": "2026-08-11T10:00:00Z",
                            "author": {"login": "agent-run-bot"},
                        }
                    ]
                ),
            }
        raise AssertionError(arguments)

    monkeypatch.setattr(publisher, "_json", fake_json)
    monkeypatch.setattr(
        publisher, "_require", lambda *arguments: calls.append(arguments)
    )

    with pytest.raises(GitHubReadError, match="state changed"):
        publisher.close_primary_ticket(
            ticket_number=2,
            run_id="run-1",
            pr_number=3,
            integrated_sha="abc123",
            before_dispatch=lambda: dispatches.append("dispatch"),
        )

    assert not any(call[:2] == ("issue", "close") for call in calls)
    assert dispatches == []


def test_prepare_close_recovers_lost_comment_response(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publisher = GhGitHubPublisher("example/project", GitRepository(git_repo))
    comments: list[dict[str, object]] = []
    lose_response = True

    def fake_json(*arguments: str) -> object:
        if arguments[:2] == ("api", "user"):
            return {"login": "agent-run-bot"}
        if arguments[:2] == (
            "api",
            "repos/example/project/issues/2/events",
        ):
            return []
        if arguments[:2] == ("issue", "view"):
            return {
                "state": "OPEN",
                "updatedAt": "2026-08-11T10:00:00Z" if comments else "before",
                "comments": comments,
            }
        raise AssertionError(arguments)

    def fake_require(*arguments: str) -> None:
        nonlocal lose_response
        if arguments[:2] == ("issue", "comment"):
            comments.append(
                {
                    "body": arguments[-1],
                    "createdAt": "2026-08-11T10:00:00Z",
                    "author": {"login": "agent-run-bot"},
                }
            )
            if lose_response:
                lose_response = False
                raise OSError("lost comment response")

    monkeypatch.setattr(publisher, "_json", fake_json)
    monkeypatch.setattr(publisher, "_require", fake_require)

    with pytest.raises(OSError, match="lost comment response"):
        publisher.prepare_primary_ticket_close(
            ticket_number=2,
            run_id="run-1",
            pr_number=3,
            integrated_sha="abc123",
        )

    assert publisher.prepare_primary_ticket_close(
        ticket_number=2,
        run_id="run-1",
        pr_number=3,
        integrated_sha="abc123",
    ) == {
        "actor": "agent-run-bot",
        "event_id": None,
        "intent_created_at": "2026-08-11T10:00:00Z",
        "baseline_event_id": 0,
        "intent_binding": "pr-3:sha-abc123",
    }


def test_prepare_close_binds_intent_to_current_pr_and_commit(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publisher = GhGitHubPublisher("example/project", GitRepository(git_repo))
    comments: list[dict[str, object]] = [
        {
            "body": (
                "<!-- agent-run:run-1:ticket-2:completed -->\n"
                "<!-- agent-run:run-1:ticket-2:"
                "publisher-close-intent:pr-3:sha-old -->\n"
                "<!-- agent-run:run-1:ticket-2:"
                "publisher-close-baseline:pr-3:sha-old:0 -->"
            ),
            "createdAt": "2026-08-11T10:00:00Z",
            "author": {"login": "agent-run-bot"},
        }
    ]
    calls: list[tuple[str, ...]] = []

    def fake_json(*arguments: str) -> object:
        if arguments[:2] == ("api", "user"):
            return {"login": "agent-run-bot"}
        if arguments[:2] == (
            "api",
            "repos/example/project/issues/2/events",
        ):
            return []
        if arguments[:2] == ("issue", "view"):
            return {
                "state": "OPEN",
                "updatedAt": "2026-08-11T11:00:00Z",
                "comments": comments,
            }
        raise AssertionError(arguments)

    def fake_require(*arguments: str) -> None:
        calls.append(arguments)
        comments.append(
            {
                "body": arguments[-1],
                "createdAt": "2026-08-11T11:00:00Z",
                "author": {"login": "agent-run-bot"},
            }
        )

    monkeypatch.setattr(publisher, "_json", fake_json)
    monkeypatch.setattr(publisher, "_require", fake_require)

    intent = publisher.prepare_primary_ticket_close(
        ticket_number=2,
        run_id="run-1",
        pr_number=4,
        integrated_sha="new",
    )

    assert intent is not None
    assert intent["intent_binding"] == "pr-4:sha-new"
    assert len(calls) == 1
    assert "publisher-close-intent:pr-4:sha-new" in calls[0][-1]


def test_prepare_close_replaces_forged_current_intent(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publisher = GhGitHubPublisher("example/project", GitRepository(git_repo))
    marker_body = (
        "<!-- agent-run:run-1:ticket-2:completed -->\n"
        "<!-- agent-run:run-1:ticket-2:"
        "publisher-close-intent:pr-3:sha-abc123 -->\n"
        "<!-- agent-run:run-1:ticket-2:"
        "publisher-close-baseline:pr-3:sha-abc123:0 -->"
    )
    comments: list[dict[str, object]] = [
        {
            "body": marker_body,
            "createdAt": "2026-08-11T10:00:00Z",
            "author": {"login": "maintainer"},
        }
    ]
    calls: list[tuple[str, ...]] = []

    def fake_json(*arguments: str) -> object:
        if arguments[:2] == ("api", "user"):
            return {"login": "agent-run-bot"}
        if arguments[:2] == (
            "api",
            "repos/example/project/issues/2/events",
        ):
            return []
        if arguments[:2] == ("issue", "view"):
            return {
                "state": "OPEN",
                "updatedAt": "2026-08-11T11:00:00Z",
                "comments": comments,
            }
        raise AssertionError(arguments)

    def fake_require(*arguments: str) -> None:
        calls.append(arguments)
        comments.append(
            {
                "body": arguments[-1],
                "createdAt": "2026-08-11T11:00:00Z",
                "author": {"login": "agent-run-bot"},
            }
        )

    monkeypatch.setattr(publisher, "_json", fake_json)
    monkeypatch.setattr(publisher, "_require", fake_require)

    intent = publisher.prepare_primary_ticket_close(
        ticket_number=2,
        run_id="run-1",
        pr_number=3,
        integrated_sha="abc123",
    )

    assert intent is not None
    assert intent["intent_binding"] == "pr-3:sha-abc123"
    assert len(calls) == 1


def test_ticket_close_ownership_rejects_later_external_reclose(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publisher = GhGitHubPublisher("example/project", GitRepository(git_repo))

    def fake_json(*arguments: str) -> object:
        if arguments[:2] == ("issue", "view"):
            return {"state": "CLOSED", "comments": []}
        if arguments[:2] == (
            "api",
            "repos/example/project/issues/2/events",
        ):
            return [
                {
                    "id": 99,
                    "event": "closed",
                    "created_at": "2026-08-11T10:00:00Z",
                    "actor": {"login": "agent-run-bot"},
                },
                {
                    "id": 100,
                    "event": "reopened",
                    "created_at": "2026-08-11T10:00:00Z",
                    "actor": {"login": "maintainer"},
                },
                {
                    "id": 101,
                    "event": "closed",
                    "created_at": "2026-08-11T10:00:00Z",
                    "actor": {"login": "maintainer"},
                },
            ]
        raise AssertionError(arguments)

    monkeypatch.setattr(publisher, "_json", fake_json)

    assert not publisher.ticket_closed_by_run(
        ticket_number=2,
        run_id="run-1",
        recorded_ownership={"event_id": 99, "actor": "agent-run-bot"},
    )


def test_ticket_close_ownership_flattens_paginated_events(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publisher = GhGitHubPublisher("example/project", GitRepository(git_repo))

    def fake_json(*arguments: str) -> object:
        if arguments[:2] == ("issue", "view"):
            return {
                "state": "CLOSED",
                "updatedAt": "2026-08-11T11:00:00Z",
                "comments": [
                    {
                        "body": (
                            "<!-- agent-run:run-1:ticket-2:"
                            "publisher-close-intent:pr-3:sha-abc123 -->\n"
                            "<!-- agent-run:run-1:ticket-2:"
                            "publisher-close-baseline:pr-3:sha-abc123:0 -->"
                        ),
                        "createdAt": "2026-08-11T10:00:00Z",
                        "author": {"login": "agent-run-bot"},
                    }
                ],
            }
        if arguments[:2] == ("api", "user"):
            return {"login": "agent-run-bot"}
        return [
            [
                {
                    "id": 101,
                    "event": "closed",
                    "created_at": "2026-08-11T10:00:00Z",
                    "actor": {"login": "agent-run-bot"},
                }
            ],
            [
                {
                    "id": 202,
                    "event": "closed",
                    "created_at": "2026-08-11T11:00:00Z",
                    "actor": {"login": "agent-run-bot"},
                }
            ],
        ]

    monkeypatch.setattr(publisher, "_json", fake_json)

    assert publisher.ticket_closed_by_run(
        ticket_number=2,
        run_id="run-1",
        recorded_ownership={"event_id": 202, "actor": "agent-run-bot"},
    )


def test_provisional_close_rejects_later_same_actor_reclose(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publisher = GhGitHubPublisher("example/project", GitRepository(git_repo))

    def fake_json(*arguments: str) -> object:
        if arguments[:2] == ("issue", "view"):
            return {
                "state": "CLOSED",
                "comments": [
                    {
                        "body": (
                            "<!-- agent-run:run-1:ticket-2:"
                            "publisher-close-intent:pr-3:sha-abc123 -->\n"
                            "<!-- agent-run:run-1:ticket-2:"
                            "publisher-close-baseline:pr-3:sha-abc123:0 -->"
                        ),
                        "createdAt": "2026-08-11T10:00:00Z",
                        "author": {"login": "agent-run-bot"},
                    }
                ],
            }
        if arguments[:2] == ("api", "user"):
            return {"login": "agent-run-bot"}
        return [
            {
                "id": 99,
                "event": "closed",
                "created_at": "2026-08-11T10:00:01Z",
                "actor": {"login": "agent-run-bot"},
            },
            {
                "id": 100,
                "event": "reopened",
                "created_at": "2026-08-11T10:00:02Z",
                "actor": {"login": "maintainer"},
            },
            {
                "id": 101,
                "event": "closed",
                "created_at": "2026-08-11T10:00:03Z",
                "actor": {"login": "agent-run-bot"},
            },
        ]

    monkeypatch.setattr(publisher, "_json", fake_json)

    assert not publisher.ticket_closed_by_run(
        ticket_number=2,
        run_id="run-1",
        recorded_ownership={
            "actor": "agent-run-bot",
            "event_id": None,
            "intent_created_at": "2026-08-11T10:00:00Z",
            "baseline_event_id": 0,
            "intent_binding": "pr-3:sha-abc123",
        },
    )
    assert not publisher.ticket_closed_by_run(
        ticket_number=2,
        run_id="run-1",
        recorded_ownership=None,
    )


def test_provisional_close_waits_for_post_intent_event(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publisher = GhGitHubPublisher("example/project", GitRepository(git_repo))

    def fake_json(*arguments: str) -> object:
        if arguments[:2] == ("issue", "view"):
            return {
                "state": "CLOSED",
                "updatedAt": "2026-08-11T10:00:00Z",
                "comments": [],
            }
        return [
            {
                "id": 50,
                "event": "closed",
                "created_at": "2026-08-11T09:00:00Z",
                "actor": {"login": "agent-run-bot"},
            }
        ]

    monkeypatch.setattr(publisher, "_json", fake_json)

    with pytest.raises(GitHubReadError, match="transition after the close intent"):
        publisher.ticket_closed_by_run(
            ticket_number=2,
            run_id="run-1",
            recorded_ownership={
                "actor": "agent-run-bot",
                "event_id": None,
                "intent_created_at": "2026-08-11T10:00:00Z",
                "baseline_event_id": 50,
                "intent_binding": "pr-3:sha-abc123",
            },
        )


def test_close_watermark_excludes_same_second_history(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publisher = GhGitHubPublisher("example/project", GitRepository(git_repo))

    def fake_json(*arguments: str) -> object:
        if arguments[:2] == ("issue", "view"):
            return {
                "state": "CLOSED",
                "updatedAt": "2026-08-11T10:00:00Z",
                "comments": [],
            }
        return [
            {
                "id": 99,
                "event": "closed",
                "created_at": "2026-08-11T10:00:00Z",
                "actor": {"login": "agent-run-bot"},
            },
            {
                "id": 100,
                "event": "reopened",
                "created_at": "2026-08-11T10:00:00Z",
                "actor": {"login": "maintainer"},
            },
            {
                "id": 101,
                "event": "closed",
                "created_at": "2026-08-11T10:00:00Z",
                "actor": {"login": "agent-run-bot"},
            },
        ]

    monkeypatch.setattr(publisher, "_json", fake_json)

    assert publisher.ticket_close_ownership(
        ticket_number=2,
        run_id="run-1",
        recorded_ownership={
            "actor": "agent-run-bot",
            "event_id": None,
            "intent_created_at": "2026-08-11T10:00:00Z",
            "baseline_event_id": 100,
            "intent_binding": "pr-3:sha-abc123",
        },
    ) == {
        "actor": "agent-run-bot",
        "event_id": 101,
        "created_at": "2026-08-11T10:00:00Z",
        "intent_created_at": "2026-08-11T10:00:00Z",
        "baseline_event_id": 100,
        "intent_binding": "pr-3:sha-abc123",
    }


def test_close_watermark_waits_while_new_close_event_is_hidden(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publisher = GhGitHubPublisher("example/project", GitRepository(git_repo))

    def fake_json(*arguments: str) -> object:
        if arguments[:2] == ("issue", "view"):
            return {
                "state": "CLOSED",
                "updatedAt": "2026-08-11T10:00:01Z",
                "comments": [],
            }
        return [
            {
                "id": 99,
                "event": "closed",
                "created_at": "2026-08-11T10:00:00Z",
                "actor": {"login": "agent-run-bot"},
            },
            {
                "id": 100,
                "event": "reopened",
                "created_at": "2026-08-11T10:00:00Z",
                "actor": {"login": "maintainer"},
            },
        ]

    monkeypatch.setattr(publisher, "_json", fake_json)

    with pytest.raises(GitHubReadError, match="close intent baseline"):
        publisher.ticket_close_ownership(
            ticket_number=2,
            run_id="run-1",
            recorded_ownership={
                "actor": "agent-run-bot",
                "event_id": None,
                "intent_created_at": "2026-08-11T10:00:00Z",
                "baseline_event_id": 100,
                "intent_binding": "pr-3:sha-abc123",
            },
        )


def test_close_rejects_any_intervening_transition_after_watermark(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publisher = GhGitHubPublisher("example/project", GitRepository(git_repo))

    def fake_json(*arguments: str) -> object:
        if arguments[:2] == ("issue", "view"):
            return {
                "state": "CLOSED",
                "updatedAt": "2026-08-11T10:00:03Z",
                "comments": [],
            }
        return [
            {
                "id": 101,
                "event": "closed",
                "created_at": "2026-08-11T10:00:01Z",
                "actor": {"login": "maintainer"},
            },
            {
                "id": 102,
                "event": "reopened",
                "created_at": "2026-08-11T10:00:02Z",
                "actor": {"login": "maintainer"},
            },
            {
                "id": 103,
                "event": "closed",
                "created_at": "2026-08-11T10:00:03Z",
                "actor": {"login": "agent-run-bot"},
            },
        ]

    monkeypatch.setattr(publisher, "_json", fake_json)

    assert (
        publisher.ticket_close_ownership(
            ticket_number=2,
            run_id="run-1",
            recorded_ownership={
                "actor": "agent-run-bot",
                "event_id": None,
                "intent_created_at": "2026-08-11T10:00:00Z",
                "baseline_event_id": 100,
                "intent_binding": "pr-3:sha-abc123",
            },
        )
        is None
    )


@pytest.mark.parametrize("event_id", [None, 99])
def test_close_rejects_intent_from_another_pr_generation(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, event_id: int | None
) -> None:
    publisher = GhGitHubPublisher("example/project", GitRepository(git_repo))
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        publisher,
        "_json",
        lambda *arguments: (_ for _ in ()).throw(AssertionError(arguments)),
    )
    monkeypatch.setattr(
        publisher, "_require", lambda *arguments: calls.append(arguments)
    )

    with pytest.raises(GitHubReadError, match="different PR generation"):
        publisher.close_primary_ticket(
            ticket_number=2,
            run_id="run-1",
            pr_number=4,
            integrated_sha="new",
            close_intent={
                "actor": "agent-run-bot",
                "event_id": event_id,
                "intent_created_at": "2026-08-11T10:00:00Z",
                "baseline_event_id": 100,
                "intent_binding": "pr-3:sha-old",
            },
        )

    assert calls == []


def test_close_rechecks_exact_ownership_before_completion(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publisher = GhGitHubPublisher("example/project", GitRepository(git_repo))

    def fake_json(*arguments: str) -> object:
        if arguments[:2] == ("issue", "view"):
            return {
                "state": "OPEN",
                "comments": [],
                "updatedAt": "2026-08-11T10:00:02Z",
            }
        return [
            {
                "id": 99,
                "event": "closed",
                "created_at": "2026-08-11T10:00:01Z",
                "actor": {"login": "agent-run-bot"},
            },
            {
                "id": 100,
                "event": "reopened",
                "created_at": "2026-08-11T10:00:02Z",
                "actor": {"login": "maintainer"},
            },
        ]

    monkeypatch.setattr(publisher, "_json", fake_json)

    with pytest.raises(GitHubReadError, match="no longer current"):
        publisher.close_primary_ticket(
            ticket_number=2,
            run_id="run-1",
            pr_number=3,
            integrated_sha="abc123",
            close_intent={
                "actor": "agent-run-bot",
                "event_id": 99,
                "created_at": "2026-08-11T10:00:01Z",
                "intent_created_at": "2026-08-11T10:00:00Z",
                "baseline_event_id": 0,
                "intent_binding": "pr-3:sha-abc123",
            },
        )


@pytest.mark.parametrize("event_id", [None, 99])
def test_recovery_rejects_ownership_from_another_pr_generation(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, event_id: int | None
) -> None:
    publisher = GhGitHubPublisher("example/project", GitRepository(git_repo))
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        publisher,
        "_json",
        lambda *arguments: (_ for _ in ()).throw(AssertionError(arguments)),
    )
    monkeypatch.setattr(
        publisher, "_require", lambda *arguments: calls.append(arguments)
    )

    with pytest.raises(GitHubReadError, match="different PR generation"):
        publisher.recover_abandoned_ticket(
            ticket_number=2,
            run_id="run-1",
            pr_number=4,
            integrated_sha="new",
            expected_ownership={
                "actor": "agent-run-bot",
                "event_id": event_id,
                "intent_created_at": "2026-08-11T10:00:00Z",
                "baseline_event_id": 0,
                "intent_binding": "pr-3:sha-old",
            },
        )

    assert calls == []


def test_open_ticket_without_post_intent_transition_is_not_owned(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publisher = GhGitHubPublisher("example/project", GitRepository(git_repo))

    def fake_json(*arguments: str) -> object:
        if arguments[:2] == ("issue", "view"):
            return {
                "state": "OPEN",
                "comments": [],
                "updatedAt": "2026-08-11T10:00:00Z",
            }
        return []

    monkeypatch.setattr(publisher, "_json", fake_json)

    assert (
        publisher.ticket_close_ownership(
            ticket_number=2,
            run_id="run-1",
            recorded_ownership={
                "actor": "agent-run-bot",
                "event_id": None,
                "intent_created_at": "2026-08-11T10:00:00Z",
                "baseline_event_id": 0,
                "intent_binding": "pr-3:sha-abc123",
            },
        )
        is None
    )


def test_dispatched_close_waits_for_open_ticket_timeline_to_converge(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publisher = GhGitHubPublisher("example/project", GitRepository(git_repo))

    def fake_json(*arguments: str) -> object:
        if arguments[:2] == ("issue", "view"):
            return {
                "state": "OPEN",
                "comments": [],
                "updatedAt": "2026-08-11T10:00:00Z",
            }
        return []

    monkeypatch.setattr(publisher, "_json", fake_json)

    with pytest.raises(GitHubReadError, match="attempted Ticket close"):
        publisher.ticket_close_ownership(
            ticket_number=2,
            run_id="run-1",
            recorded_ownership={
                "actor": "agent-run-bot",
                "event_id": None,
                "intent_created_at": "2026-08-11T10:00:00Z",
                "baseline_event_id": 0,
                "intent_binding": "pr-3:sha-abc123",
                "dispatch_attempted": True,
            },
        )


def test_closed_ticket_waits_for_exact_close_event(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publisher = GhGitHubPublisher("example/project", GitRepository(git_repo))

    def fake_json(*arguments: str) -> object:
        if arguments[:2] == ("issue", "view"):
            return {
                "state": "CLOSED",
                "comments": [],
                "updatedAt": "2026-08-11T10:00:01Z",
            }
        return []

    monkeypatch.setattr(publisher, "_json", fake_json)

    with pytest.raises(GitHubReadError) as error:
        publisher.ticket_close_ownership(
            ticket_number=2,
            run_id="run-1",
            recorded_ownership={
                "actor": "agent-run-bot",
                "event_id": None,
                "intent_created_at": "2026-08-11T10:00:00Z",
                "baseline_event_id": 0,
                "intent_binding": "pr-3:sha-abc123",
                "dispatch_attempted": True,
            },
        )

    assert error.value.code == "ticket_close_ownership_pending"


def test_close_ownership_waits_for_transition_actor(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publisher = GhGitHubPublisher("example/project", GitRepository(git_repo))

    def fake_json(*arguments: str) -> object:
        if arguments[:2] == ("issue", "view"):
            return {
                "state": "CLOSED",
                "comments": [],
                "updatedAt": "2026-08-11T10:00:01Z",
            }
        return [
            {
                "id": 101,
                "event": "closed",
                "created_at": "2026-08-11T10:00:01Z",
                "actor": None,
            }
        ]

    monkeypatch.setattr(publisher, "_json", fake_json)

    with pytest.raises(GitHubReadError, match="transition actor") as error:
        publisher.ticket_close_ownership(
            ticket_number=2,
            run_id="run-1",
            recorded_ownership={
                "actor": "agent-run-bot",
                "event_id": None,
                "intent_created_at": "2026-08-11T10:00:00Z",
                "baseline_event_id": 100,
                "intent_binding": "pr-3:sha-abc123",
                "dispatch_attempted": True,
            },
        )

    assert error.value.code == "ticket_close_ownership_pending"


def test_explicit_reopen_resolves_older_missing_close_actor(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publisher = GhGitHubPublisher("example/project", GitRepository(git_repo))

    def fake_json(*arguments: str) -> object:
        if arguments[:2] == ("issue", "view"):
            return {
                "state": "OPEN",
                "comments": [],
                "updatedAt": "2026-08-11T10:00:02Z",
            }
        return [
            {
                "id": 101,
                "event": "closed",
                "created_at": "2026-08-11T10:00:01Z",
                "actor": None,
            },
            {
                "id": 102,
                "event": "reopened",
                "created_at": "2026-08-11T10:00:02Z",
                "actor": {"login": "maintainer"},
            },
        ]

    monkeypatch.setattr(publisher, "_json", fake_json)

    assert (
        publisher.ticket_close_ownership(
            ticket_number=2,
            run_id="run-1",
            recorded_ownership={
                "actor": "agent-run-bot",
                "event_id": None,
                "intent_created_at": "2026-08-11T10:00:00Z",
                "baseline_event_id": 100,
                "intent_binding": "pr-3:sha-abc123",
                "dispatch_attempted": True,
            },
        )
        is None
    )


def test_latest_close_with_missing_actor_remains_pending(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publisher = GhGitHubPublisher("example/project", GitRepository(git_repo))

    def fake_json(*arguments: str) -> object:
        if arguments[:2] == ("issue", "view"):
            return {
                "state": "CLOSED",
                "comments": [],
                "updatedAt": "2026-08-11T10:00:02Z",
            }
        return [
            {
                "id": 101,
                "event": "reopened",
                "created_at": "2026-08-11T10:00:01Z",
                "actor": {"login": "maintainer"},
            },
            {
                "id": 102,
                "event": "closed",
                "created_at": "2026-08-11T10:00:02Z",
                "actor": None,
            },
        ]

    monkeypatch.setattr(publisher, "_json", fake_json)

    with pytest.raises(GitHubReadError, match="transition actor"):
        publisher.ticket_close_ownership(
            ticket_number=2,
            run_id="run-1",
            recorded_ownership={
                "actor": "agent-run-bot",
                "event_id": None,
                "intent_created_at": "2026-08-11T10:00:00Z",
                "baseline_event_id": 100,
                "intent_binding": "pr-3:sha-abc123",
                "dispatch_attempted": True,
            },
        )


def test_close_retry_does_not_overwrite_external_reopen(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publisher = GhGitHubPublisher("example/project", GitRepository(git_repo))
    calls: list[tuple[str, ...]] = []

    def fake_json(*arguments: str) -> object:
        if arguments[:2] == ("api", "user"):
            return {"login": "agent-run-bot"}
        if arguments[:2] == ("issue", "view"):
            return {
                "state": "OPEN",
                "comments": [
                    {
                        "body": (
                            "<!-- agent-run:run-1:ticket-2:completed -->\n"
                            "<!-- agent-run:run-1:ticket-2:"
                            "publisher-close-intent:pr-3:sha-abc123 -->\n"
                            "<!-- agent-run:run-1:ticket-2:"
                            "publisher-close-baseline:pr-3:sha-abc123:0 -->"
                        ),
                        "createdAt": "2026-08-11T10:00:00Z",
                        "author": {"login": "agent-run-bot"},
                    }
                ],
            }
        return [
            {
                "id": 99,
                "event": "closed",
                "created_at": "2026-08-11T10:00:01Z",
                "actor": {"login": "agent-run-bot"},
            },
            {
                "id": 100,
                "event": "reopened",
                "created_at": "2026-08-11T10:00:02Z",
                "actor": {"login": "maintainer"},
            },
        ]

    monkeypatch.setattr(publisher, "_json", fake_json)
    monkeypatch.setattr(
        publisher, "_require", lambda *arguments: calls.append(arguments)
    )

    with pytest.raises(
        GitHubReadError, match="changed after the Publisher close intent"
    ):
        publisher.close_primary_ticket(
            ticket_number=2,
            run_id="run-1",
            pr_number=3,
            integrated_sha="abc123",
        )

    assert not any(call[:2] == ("issue", "close") for call in calls)


def test_abandonment_rechecks_exact_close_before_reopen(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publisher = GhGitHubPublisher("example/project", GitRepository(git_repo))
    calls: list[tuple[str, ...]] = []

    def fake_json(*arguments: str) -> object:
        if arguments[:2] == ("issue", "view"):
            return {
                "state": "CLOSED",
                "updatedAt": "2026-08-11T10:00:01Z",
                "comments": [],
            }
        return [
            {
                "id": 99,
                "event": "closed",
                "created_at": "2026-08-11T10:00:01Z",
                "actor": {"login": "agent-run-bot"},
            },
            {
                "id": 100,
                "event": "reopened",
                "created_at": "2026-08-11T10:00:02Z",
                "actor": {"login": "maintainer"},
            },
            {
                "id": 101,
                "event": "closed",
                "created_at": "2026-08-11T10:00:03Z",
                "actor": {"login": "maintainer"},
            },
        ]

    monkeypatch.setattr(publisher, "_json", fake_json)
    monkeypatch.setattr(
        publisher, "_require", lambda *arguments: calls.append(arguments)
    )

    assert not publisher.recover_abandoned_ticket(
        ticket_number=2,
        run_id="run-1",
        pr_number=3,
        integrated_sha="abc123",
        expected_ownership={
            "actor": "agent-run-bot",
            "event_id": 99,
            "intent_binding": "pr-3:sha-abc123",
        },
    )
    assert not any(call[:2] == ("issue", "comment") for call in calls)
    assert not any(call[:2] == ("issue", "reopen") for call in calls)


def test_abandonment_waits_when_issue_is_newer_than_visible_events(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publisher = GhGitHubPublisher("example/project", GitRepository(git_repo))
    calls: list[tuple[str, ...]] = []

    def fake_json(*arguments: str) -> object:
        if arguments[:2] == ("issue", "view"):
            return {
                "state": "CLOSED",
                "updatedAt": "2026-08-11T10:00:03Z",
                "comments": [],
            }
        return [
            {
                "id": 99,
                "event": "closed",
                "created_at": "2026-08-11T10:00:01Z",
                "actor": {"login": "agent-run-bot"},
            }
        ]

    monkeypatch.setattr(publisher, "_json", fake_json)
    monkeypatch.setattr(
        publisher, "_require", lambda *arguments: calls.append(arguments)
    )

    with pytest.raises(GitHubReadError, match="newer than the recorded close"):
        publisher.recover_abandoned_ticket(
            ticket_number=2,
            run_id="run-1",
            pr_number=3,
            integrated_sha="abc123",
            expected_ownership={
                "actor": "agent-run-bot",
                "event_id": 99,
                "intent_binding": "pr-3:sha-abc123",
            },
        )
    assert calls == []


@pytest.mark.parametrize(
    ("marker_author", "reopen_actor", "expected"),
    [
        ("maintainer", "maintainer", False),
        ("agent-run-bot", "agent-run-bot", True),
    ],
)
def test_open_recovery_requires_publisher_marker_and_reopen(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    marker_author: str,
    reopen_actor: str,
    expected: bool,
) -> None:
    publisher = GhGitHubPublisher("example/project", GitRepository(git_repo))

    def fake_json(*arguments: str) -> object:
        if arguments[:2] == ("issue", "view"):
            return {
                "state": "OPEN",
                "updatedAt": "2026-08-11T10:00:03Z",
                "comments": [
                    {
                        "body": "<!-- agent-run:run-1:ticket-2:abandoned -->",
                        "createdAt": "2026-08-11T10:00:02Z",
                        "author": {"login": marker_author},
                    }
                ],
            }
        return [
            {
                "id": 99,
                "event": "closed",
                "created_at": "2026-08-11T10:00:01Z",
                "actor": {"login": "agent-run-bot"},
            },
            {
                "id": 100,
                "event": "reopened",
                "created_at": "2026-08-11T10:00:03Z",
                "actor": {"login": reopen_actor},
            },
        ]

    monkeypatch.setattr(publisher, "_json", fake_json)

    assert (
        publisher.recover_abandoned_ticket(
            ticket_number=2,
            run_id="run-1",
            pr_number=3,
            integrated_sha="abc123",
            expected_ownership={
                "actor": "agent-run-bot",
                "event_id": 99,
                "intent_binding": "pr-3:sha-abc123",
            },
        )
        is expected
    )


def test_open_recovery_records_comment_after_lost_reopen_response(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publisher = GhGitHubPublisher("example/project", GitRepository(git_repo))
    calls: list[tuple[str, ...]] = []

    def fake_json(*arguments: str) -> object:
        if arguments[:2] == ("issue", "view"):
            return {
                "state": "OPEN",
                "updatedAt": "2026-08-11T10:00:03Z",
                "comments": [],
            }
        return [
            {
                "id": 99,
                "event": "closed",
                "created_at": "2026-08-11T10:00:01Z",
                "actor": {"login": "agent-run-bot"},
            },
            {
                "id": 100,
                "event": "reopened",
                "created_at": "2026-08-11T10:00:03Z",
                "actor": {"login": "agent-run-bot"},
            },
        ]

    monkeypatch.setattr(publisher, "_json", fake_json)
    monkeypatch.setattr(
        publisher, "_require", lambda *arguments: calls.append(arguments)
    )

    assert publisher.recover_abandoned_ticket(
        ticket_number=2,
        run_id="run-1",
        pr_number=3,
        integrated_sha="abc123",
        expected_ownership={
            "actor": "agent-run-bot",
            "event_id": 99,
            "intent_binding": "pr-3:sha-abc123",
        },
    )
    assert [call[:2] for call in calls] == [("issue", "comment")]


def test_durable_close_event_survives_deleted_intent_comment(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publisher = GhGitHubPublisher("example/project", GitRepository(git_repo))

    def fake_json(*arguments: str) -> object:
        if arguments[:2] == ("issue", "view"):
            return {
                "state": "CLOSED",
                "updatedAt": "2026-08-11T10:00:01Z",
                "comments": [],
            }
        return [
            {
                "id": 101,
                "event": "closed",
                "created_at": "2026-08-11T10:00:01Z",
                "actor": {"login": "agent-run-bot"},
            }
        ]

    monkeypatch.setattr(publisher, "_json", fake_json)

    assert publisher.ticket_closed_by_run(
        ticket_number=2,
        run_id="run-1",
        recorded_ownership={"event_id": 101, "actor": "agent-run-bot"},
    )


def test_publish_branch_accepts_retry_after_successful_push(
    git_repo: Path,
    tmp_path: Path,
) -> None:
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "clone", "--bare", str(git_repo), str(remote)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "remote", "add", "origin", str(remote)],
        cwd=git_repo,
        check=True,
    )
    base = _rev_parse(git_repo, "HEAD")
    subprocess.run(
        ["git", "push", "origin", f"{base}:refs/heads/ticket"],
        cwd=git_repo,
        check=True,
        capture_output=True,
    )
    (git_repo / "candidate.txt").write_text("candidate\n", encoding="utf-8")
    subprocess.run(["git", "add", "candidate.txt"], cwd=git_repo, check=True)
    subprocess.run(
        ["git", "commit", "-m", "candidate"],
        cwd=git_repo,
        check=True,
        capture_output=True,
    )
    candidate = _rev_parse(git_repo, "HEAD")
    publisher = GhGitHubPublisher("example/project", GitRepository(git_repo))

    publisher.publish_branch("ticket", candidate, expected_remote_sha=base)
    publisher.publish_branch("ticket", candidate, expected_remote_sha=base)

    assert _remote_head(git_repo, "ticket") == candidate


def test_ticket_ref_creation_is_expected_absent_cas_with_exact_readback(
    git_repo: Path, tmp_path: Path
) -> None:
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "clone", "--bare", str(git_repo), str(remote)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "remote", "add", "origin", str(remote)],
        cwd=git_repo,
        check=True,
    )
    base = _rev_parse(git_repo, "HEAD")

    GhGitHubPublisher("example/project", GitRepository(git_repo)).ensure_ticket_branch(
        ticket_number=3,
        branch="agent-run/run-1/ticket-3",
        base_branch="main",
        expected_base_sha=base,
        expected_remote_sha=base,
        recovery_remote_sha=base,
    )

    assert _remote_head(git_repo, "agent-run/run-1/ticket-3") == base


def test_run_ref_authority_uses_expected_absent_cas_and_rejects_foreign_ref(
    git_repo: Path, tmp_path: Path
) -> None:
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "clone", "--bare", str(git_repo), str(remote)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "remote", "add", "origin", str(remote)],
        cwd=git_repo,
        check=True,
    )
    base = _rev_parse(git_repo, "HEAD")
    branch = "agent-run/run-1/run"
    publisher = GhGitHubPublisher("example/project", GitRepository(git_repo))

    publisher._ensure_remote_run_branch(branch, base)

    assert _remote_head(git_repo, branch) == base
    (git_repo / "foreign.txt").write_text("foreign\n", encoding="utf-8")
    subprocess.run(["git", "add", "foreign.txt"], cwd=git_repo, check=True)
    subprocess.run(
        ["git", "commit", "-m", "foreign ref"],
        cwd=git_repo,
        check=True,
        capture_output=True,
    )
    foreign = _rev_parse(git_repo, "HEAD")
    subprocess.run(
        ["git", "push", "--force", "origin", f"{foreign}:refs/heads/{branch}"],
        cwd=git_repo,
        check=True,
        capture_output=True,
    )

    with pytest.raises(GitError, match="Run ref has a foreign identity"):
        publisher._ensure_remote_run_branch(branch, base)

    assert _remote_head(git_repo, branch) == foreign


@pytest.mark.parametrize(
    "identity",
    [
        {"head_repository": "foreign/project"},
        {"head_branch": "foreign-run-ref"},
    ],
)
def test_final_run_pr_foreign_identity_is_not_reused_or_edited(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, identity: dict[str, str]
) -> None:
    publisher = GhGitHubPublisher("example/project", GitRepository(git_repo))
    writes: list[tuple[str, ...]] = []
    monkeypatch.setattr(publisher, "_json", lambda *_arguments: [{"number": 12}])
    monkeypatch.setattr(
        publisher,
        "live_pull_request",
        lambda _number: {
            "state": "OPEN",
            "head_branch": "agent-run/run-1/run",
            "head_sha": "a" * 40,
            "head_repository": "example/project",
            "base_branch": "main",
            "base_sha": "b" * 40,
            "base_repository": "example/project",
            **identity,
        },
    )
    monkeypatch.setattr(publisher, "_require", lambda *args: writes.append(args))

    with pytest.raises(GitHubReadError, match="durable identity"):
        publisher.ensure_run_pr(
            branch="agent-run/run-1/run",
            base_branch="main",
            expected_head_sha="a" * 40,
            expected_base_sha="b" * 40,
            title="current title",
            body="current body",
        )

    assert writes == []


@pytest.mark.parametrize(
    "branch",
    ["agent-run/run-1/parent", "agent-run-repair/run-1/1"],
)
def test_change_ref_authority_recovers_exact_existing_ref(
    git_repo: Path, tmp_path: Path, branch: str
) -> None:
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "clone", "--bare", str(git_repo), str(remote)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "remote", "add", "origin", str(remote)],
        cwd=git_repo,
        check=True,
    )
    base = _rev_parse(git_repo, "HEAD")

    GhGitHubPublisher("example/project", GitRepository(git_repo)).ensure_change_branch(
        branch=branch,
        base_branch="main",
        expected_base_sha=base,
        expected_remote_sha=base,
        recovery_remote_sha=base,
    )

    assert _remote_head(git_repo, branch) == base


def test_ticket_pr_wrong_base_fails_closed_without_pr_write(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publisher = GhGitHubPublisher("example/project", GitRepository(git_repo))
    writes: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        publisher,
        "_json",
        lambda *_arguments: [
            {
                "number": 12,
                "head": {
                    "ref": "ticket-3",
                    "sha": "expected-head",
                    "repo": {"full_name": "example/project"},
                },
                "base": {
                    "ref": "wrong-base",
                    "sha": "base",
                    "repo": {"full_name": "example/project"},
                },
            }
        ],
    )
    monkeypatch.setattr(publisher, "_require", lambda *args: writes.append(args))

    with pytest.raises(GitHubReadError, match="durable ref and base identity"):
        publisher.ensure_ticket_pr(
            branch="ticket-3",
            base_branch="agent-run/run-1",
            title="title",
            body="body",
            primary_ticket=3,
            expected_head_sha="expected-head",
            expected_base_sha="base",
        )

    assert writes == []


def test_existing_change_pr_refreshes_current_narrative_after_identity_readback(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publisher = GhGitHubPublisher("example/project", GitRepository(git_repo))
    pulls = [
        {
            "number": 12,
            "title": "old candidate title",
            "body": "old candidate body",
            "head": {
                "ref": "agent-run-repair/run-1/1",
                "sha": "a" * 40,
                "repo": {"full_name": "example/project"},
            },
            "base": {
                "ref": "agent-run/run-1/run",
                "sha": "b" * 40,
                "repo": {"full_name": "example/project"},
            },
        }
    ]
    writes: list[tuple[str, ...]] = []

    monkeypatch.setattr(publisher, "_json", lambda *_arguments: pulls)

    def fake_require(*arguments: str) -> None:
        writes.append(arguments)
        assert arguments[:2] == ("pr", "edit")
        pulls[0]["title"] = arguments[arguments.index("--title") + 1]
        pulls[0]["body"] = arguments[arguments.index("--body") + 1]

    monkeypatch.setattr(publisher, "_require", fake_require)

    assert publisher.ensure_change_pr(
        branch="agent-run-repair/run-1/1",
        base_branch="agent-run/run-1/run",
        title="fix(run): repair required checks",
        body="Delivery Type: Run Repair\n\nsecond publication evidence",
        expected_head_sha="a" * 40,
        expected_base_sha="b" * 40,
    ) == 12

    assert writes == [
        (
            "pr",
            "edit",
            "12",
            "--repo",
            "example/project",
            "--title",
            "fix(run): repair required checks",
            "--body",
            "Delivery Type: Run Repair\n\nsecond publication evidence",
        )
    ]
    assert pulls[0]["title"] == "fix(run): repair required checks"
    assert pulls[0]["body"] == "Delivery Type: Run Repair\n\nsecond publication evidence"


def test_ticket_pr_wrong_base_is_rejected_before_ref_publication(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publisher = GhGitHubPublisher("example/project", GitRepository(git_repo))
    writes: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        publisher,
        "_json",
        lambda *_arguments: [
            {
                "number": 12,
                "head": {
                    "ref": "ticket-3",
                    "sha": "expected-head",
                    "repo": {"full_name": "example/project"},
                },
                "base": {
                    "ref": "wrong-base",
                    "sha": "base",
                    "repo": {"full_name": "example/project"},
                },
            }
        ],
    )
    monkeypatch.setattr(
        "agent_run.github_publish.run_write_command",
        lambda arguments, **_kwargs: writes.append(tuple(arguments)),
    )

    with pytest.raises(GitHubReadError, match="durable ref and base identity"):
        publisher.verify_ticket_pr_before_publish(
            branch="ticket-3",
            base_branch="agent-run/run-1",
            expected_head_sha="expected-head",
            expected_base_sha="base",
        )

    assert writes == []


def test_sync_run_branch_recovers_remote_integration_locally(
    git_repo: Path,
    tmp_path: Path,
) -> None:
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "clone", "--bare", str(git_repo), str(remote)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "remote", "add", "origin", str(remote)],
        cwd=git_repo,
        check=True,
    )
    base = _rev_parse(git_repo, "HEAD")
    subprocess.run(["git", "branch", "run", base], cwd=git_repo, check=True)
    (git_repo / "candidate.txt").write_text("candidate\n", encoding="utf-8")
    subprocess.run(["git", "add", "candidate.txt"], cwd=git_repo, check=True)
    subprocess.run(
        ["git", "commit", "-m", "feat(test): remote integration"],
        cwd=git_repo,
        check=True,
        capture_output=True,
    )
    integrated = _rev_parse(git_repo, "HEAD")
    subprocess.run(
        ["git", "push", "origin", f"{integrated}:refs/heads/run"],
        cwd=git_repo,
        check=True,
        capture_output=True,
    )
    assert _rev_parse(git_repo, "run") == base

    GhGitHubPublisher("example/project", GitRepository(git_repo)).sync_run_branch(
        run_branch="run", integrated_sha=integrated
    )

    assert _rev_parse(git_repo, "run") == integrated


def test_ticket_pr_recovery_only_reuses_open_prs(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publisher = GhGitHubPublisher("example/project", GitRepository(git_repo))
    reads: list[tuple[str, ...]] = []
    writes: list[tuple[str, ...]] = []
    exact_pull = {
        "number": 12,
        "head": {
            "ref": "ticket-3",
            "sha": "a" * 40,
            "repo": {"full_name": "example/project"},
        },
        "base": {
            "ref": "agent-run/run-1",
            "sha": "b" * 40,
            "repo": {"full_name": "example/project"},
        },
    }

    def fake_json(*arguments: str) -> object:
        reads.append(arguments)
        return [] if len(reads) == 1 else [exact_pull]

    def fake_require(*arguments: str) -> None:
        writes.append(arguments)

    monkeypatch.setattr(publisher, "_json", fake_json)
    monkeypatch.setattr(publisher, "_require", fake_require)

    number = publisher.ensure_ticket_pr(
        branch="ticket-3",
        base_branch="agent-run/run-1",
        title="fix(delivery): handle a later revision",
        body="Primary Ticket: #3",
        primary_ticket=3,
        expected_head_sha="a" * 40,
        expected_base_sha="b" * 40,
    )

    assert reads[0][:2] == ("api", "repos/example/project/pulls")
    assert "state=open" in reads[0]
    assert writes[0][:2] == ("pr", "create")
    assert number == 12


def test_agent_run_status_comment_is_updated_in_place(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publisher = GhGitHubPublisher("example/project", GitRepository(git_repo))
    comments: list[dict[str, object]] = []

    def fake_json(*arguments: str) -> object:
        if arguments[:1] != ("api",):
            raise AssertionError(arguments)
        if "--paginate" in arguments:
            return comments
        body = next(
            argument[5:] for argument in arguments if argument.startswith("body=")
        )
        if "PATCH" in arguments:
            comments[0]["body"] = body
        else:
            comments.append({"id": 9, "body": body})
        return {"id": 9}

    monkeypatch.setattr(publisher, "_json", fake_json)
    pending = {
        "scope": "ticket-3",
        "base_sha": "a" * 40,
        "candidate_sha": "b" * 40,
        "validation_outcome": "pass",
        "lane_statuses": {"e2e": "pass", "standards": "pass", "spec": "pass"},
        "required_checks": "pending",
        "next_action": "wait for Required Checks",
    }
    publisher.record_agent_run_status(12, pending)
    publisher.record_agent_run_status(
        12, {**pending, "required_checks": "pass", "next_action": "merge"}
    )

    assert len(comments) == 1
    body = str(comments[0]["body"])
    assert "<!-- agent-run:agent-run-status -->" in body
    assert "Required Checks: `pass`" in body
    assert "```json" not in body


def test_squash_merge_uses_supported_pr_merge_and_live_result(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publisher = GhGitHubPublisher("example/project", GitRepository(git_repo))
    captured: list[str] = []

    def fake_run(*arguments: str) -> subprocess.CompletedProcess[str]:
        captured.extend(arguments)
        return subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr(publisher, "_run", fake_run)
    monkeypatch.setattr(
        publisher,
        "live_pull_request",
        lambda _pr: {
            "state": "MERGED",
            "integrated_sha": "a" * 40,
        },
    )

    integrated = publisher.squash_merge(
        pr_number=11,
        expected_head_sha="b" * 40,
        run_branch="agent-run/run",
        commit_message="feat(test): supported merge",
    )

    assert integrated == "a" * 40
    assert captured == [
        "pr",
        "merge",
        "11",
        "--repo",
        "example/project",
        "--squash",
        "--match-head-commit",
        "b" * 40,
        "--subject",
        "feat(test): supported merge",
    ]


def test_squash_merge_recovers_when_command_response_is_lost(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publisher = GhGitHubPublisher("example/project", GitRepository(git_repo))
    monkeypatch.setattr(
        publisher,
        "_run",
        lambda *arguments: subprocess.CompletedProcess(
            arguments, 1, "", "connection reset"
        ),
    )
    monkeypatch.setattr(
        publisher,
        "live_pull_request",
        lambda _pr: {
            "state": "MERGED",
            "integrated_sha": "c" * 40,
        },
    )

    integrated = publisher.squash_merge(
        pr_number=11,
        expected_head_sha="b" * 40,
        run_branch="agent-run/run",
        commit_message="feat(test): response recovery",
    )

    assert integrated == "c" * 40


def test_required_checks_falls_back_when_gh_omits_ruleset_requirements(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publisher = GhGitHubPublisher("example/project", GitRepository(git_repo))
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
            return subprocess.CompletedProcess(
                arguments,
                0,
                (
                    '[{"name":"test","bucket":"fail","link":"required-link",'
                    '"workflow":"tests","description":"required failure"},'
                    '{"name":"optional","bucket":"fail","link":"optional-link",'
                    '"workflow":"optional","description":"optional failure"}]'
                ),
                "",
            )
        return subprocess.CompletedProcess(
            arguments,
            0,
            '[{"name":"test","bucket":"pass"},{"name":"optional","bucket":"fail"}]',
            "",
        )

    monkeypatch.setattr(publisher, "_run", fake_run)
    monkeypatch.setattr(
        publisher,
        "live_pull_request",
        lambda _pr: {"base_branch": "agent-run/run-1/run"},
    )

    assert publisher.required_checks(12) == "pass"
    assert "--required" in calls[0]
    assert "--required" not in calls[-1]
    assert "name" in calls[-1][-1]
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


def test_ruleset_required_check_not_yet_reported_is_pending(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publisher = GhGitHubPublisher("example/project", GitRepository(git_repo))
    monkeypatch.setattr(
        publisher,
        "live_pull_request",
        lambda _pr: {"base_branch": "agent-run/run-1/run"},
    )
    monkeypatch.setattr(
        publisher,
        "_ruleset_required_contexts",
        lambda _branch: {"test": None},
    )
    monkeypatch.setattr(
        publisher, "_branch_protection_required_contexts", lambda _branch: []
    )
    monkeypatch.setattr(publisher, "_json", lambda *_arguments, **_kwargs: [])

    assert publisher._ruleset_checks(12, "bucket") == [
        {"name": "test", "bucket": "pending"}
    ]


def test_partial_required_check_reporting_projects_missing_context_as_pending(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publisher = GhGitHubPublisher("example/project", GitRepository(git_repo))
    monkeypatch.setattr(
        publisher,
        "live_pull_request",
        lambda _pr: {"base_branch": "main"},
    )
    monkeypatch.setattr(
        publisher,
        "_ruleset_required_contexts",
        lambda _branch: {"A": None, "B": None},
    )
    monkeypatch.setattr(
        publisher, "_branch_protection_required_contexts", lambda _branch: []
    )

    def fake_run(*arguments: str) -> subprocess.CompletedProcess[str]:
        assert "--required" in arguments
        return subprocess.CompletedProcess(
            arguments, 0, '[{"name":"A","bucket":"pass"}]', ""
        )

    monkeypatch.setattr(publisher, "_run", fake_run)

    assert publisher._checks(12, "bucket,name") == [
        {"name": "A", "bucket": "pass"},
        {"name": "B", "bucket": "pending"},
    ]
    assert publisher.required_checks(12) == "pending"


def test_branch_protection_required_check_not_yet_reported_is_pending(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publisher = GhGitHubPublisher("example/project", GitRepository(git_repo))

    def fake_run(*arguments: str) -> subprocess.CompletedProcess[str]:
        if "--required" in arguments:
            return subprocess.CompletedProcess(
                arguments, 1, "", "no required checks reported"
            )
        if arguments[:2] == (
            "api",
            "repos/example/project/rulesets",
        ):
            return subprocess.CompletedProcess(arguments, 0, "[[]]", "")
        if arguments[0:1] == ("api",) and arguments[1].endswith(
            "/protection/required_status_checks/contexts"
        ):
            return subprocess.CompletedProcess(arguments, 0, '["test"]', "")
        return subprocess.CompletedProcess(
            arguments, 0, '[{"name":"optional","bucket":"pass"}]', ""
        )

    monkeypatch.setattr(publisher, "_run", fake_run)
    monkeypatch.setattr(
        publisher,
        "live_pull_request",
        lambda _pr: {"base_branch": "agent-run/run-1/run"},
    )

    assert publisher.required_checks(12) == "pending"


def test_unrecognized_required_check_bucket_is_supervised_as_unknown(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publisher = GhGitHubPublisher("example/project", GitRepository(git_repo))
    monkeypatch.setattr(
        publisher,
        "_checks",
        lambda _pr_number, _fields: [{"name": "test", "bucket": "mystery"}],
    )

    assert publisher.required_checks(12) == "unknown"


def test_required_checks_cli_failure_is_classified_as_a_read_failure(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publisher = GhGitHubPublisher("example/project", GitRepository(git_repo))
    monkeypatch.setattr(
        publisher,
        "_run",
        lambda *_arguments: subprocess.CompletedProcess(
            _arguments, 2, "", "temporary gh pr checks failure"
        ),
    )

    with pytest.raises(GitHubReadError) as raised:
        publisher.required_checks(12)

    assert raised.value.code == "github_read_failed"
    assert raised.value.message == "temporary gh pr checks failure"


def test_ruleset_branch_globs_do_not_cross_path_segments() -> None:
    assert _matches_ref("refs/heads/release/v1", "refs/heads/release/*")
    assert not _matches_ref("refs/heads/release/v1/patch", "refs/heads/release/*")
    assert _matches_ref("refs/heads/release/v1/patch", "refs/heads/release/**")


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (
            {
                "data": {
                    "createLinkedBranch": {
                        "linkedBranch": {
                            "ref": {
                                "name": "agent-run/run-1/ticket-3",
                                "target": {"oid": "head"},
                            }
                        }
                    }
                }
            },
            "linked",
        ),
        ({"data": {"createLinkedBranch": {"linkedBranch": None}}}, "unavailable"),
        (
            {
                "data": {
                    "createLinkedBranch": {
                        "linkedBranch": {
                            "ref": {"name": "wrong", "target": {"oid": "head"}}
                        }
                    }
                }
            },
            "unavailable",
        ),
    ],
)
def test_linked_branch_display_uses_graphql_and_requires_exact_readback(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    response: dict[str, object],
    expected: str,
) -> None:
    publisher = GhGitHubPublisher("example/project", GitRepository(git_repo))
    commands: list[tuple[str, ...]] = []
    options: list[dict[str, object]] = []
    before_config = subprocess.run(
        ["git", "config", "--local", "--list"],
        cwd=git_repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout
    monkeypatch.setattr(publisher, "_json", lambda *_arguments: {"id": "I_1"})

    def fake_run(*arguments: str, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(arguments)
        options.append(_kwargs)
        return subprocess.CompletedProcess(arguments, 0, json.dumps(response), "")

    monkeypatch.setattr(publisher, "_run", fake_run)

    assert publisher.link_issue_branch_display(
        issue_number=3, branch="agent-run/run-1/ticket-3", head_sha="head"
    ) == expected
    assert commands and commands[0][:2] == ("api", "graphql")
    assert all("develop" not in command for command in commands)
    assert options == [{"retry": False}]
    after_config = subprocess.run(
        ["git", "config", "--local", "--list"],
        cwd=git_repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout
    assert after_config == before_config


def _rev_parse(repository: Path, reference: str) -> str:
    return subprocess.run(
        ["git", "rev-parse", reference],
        cwd=repository,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()


def _remote_head(repository: Path, branch: str) -> str:
    output = subprocess.run(
        ["git", "ls-remote", "--heads", "origin", f"refs/heads/{branch}"],
        cwd=repository,
        text=True,
        capture_output=True,
        check=True,
    ).stdout
    return output.split()[0]
