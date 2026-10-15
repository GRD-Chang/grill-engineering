"""App authors participate in publication and unknown-write reconciliation."""
from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from agent_run.git import GitRepository
from agent_run.github import GitHubReadError
from agent_run.github_auth import GitHubCredentialError, resolve_app_login
from agent_run.github_auth_profile import GitHubAppProfile
from agent_run.github_publish import GhGitHubPublisher


@pytest.fixture
def app_publisher(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> GhGitHubPublisher:
    key = tmp_path / "app.pem"
    key.write_text("test-key")
    key.chmod(0o600)
    monkeypatch.setattr("agent_run.github_auth._create_app_jwt", lambda *a, **kw: "test-jwt")

    def response(request, timeout):
        assert request.full_url == "https://api.github.com/app"
        assert request.headers["Authorization"] == "Bearer test-jwt"
        assert timeout == 15
        return io.BytesIO(json.dumps({"id": 123, "slug": "delivery"}).encode())

    monkeypatch.setattr("agent_run.github_auth.urllib.request.urlopen", response)
    identity = {"source": "app", "app_profile": {
        "app_id": "123", "installation_id": "456", "private_key_path": str(key),
    }, "app_read_only": False, "app_current_repository_only": False}
    return GhGitHubPublisher("example/project", GitRepository(tmp_path, runner_identity=identity))


@pytest.mark.parametrize("app", [{"id": 999, "slug": "delivery"}, {"id": 123},
                                  {"id": 123, "slug": "bad/slug"}])
def test_app_actor_rejects_wrong_or_missing_metadata(app_publisher, monkeypatch, app):
    monkeypatch.setattr("agent_run.github_auth.urllib.request.urlopen",
                        lambda *_a, **_kw: io.BytesIO(json.dumps(app).encode()))
    profile = app_publisher.git.runner_identity["app_profile"]
    with pytest.raises(GitHubCredentialError, match="identity is invalid"):
        resolve_app_login(GitHubAppProfile("123", "456", Path(profile["private_key_path"])))


def test_app_publication_recovers_lost_response_and_ignores_foreign_comment(app_publisher, monkeypatch):
    comments = []
    writes = []

    def api(*arguments, **kwargs):
        assert arguments[:2] != ("api", "user")
        if "-f" not in arguments:
            return [comments]
        assert "PATCH" not in arguments  # Never edit the copied foreign marker.
        body = next(arg[5:] for arg in arguments if arg.startswith("body="))
        writes.append(body)
        comments.append({"id": 2, "body": body, "user": {"login": "delivery[bot]"}})
        raise OSError("lost publication response")

    monkeypatch.setattr(app_publisher, "_json", api)
    record = {"run_id": "run-1", "candidate_sha": "abc"}
    with pytest.raises(OSError, match="lost publication"):
        app_publisher.record_run_publication(10, record)
    # A second operation with the same body but another author must not satisfy recovery.
    owned = comments.pop()
    comments.append({**owned, "id": 1, "user": {"login": "other[bot]"}})
    with pytest.raises(OSError, match="lost publication"):
        app_publisher.record_run_publication(10, record)
    assert len(writes) == 2
    app_publisher.record_run_publication(10, record)
    assert len(writes) == 2


@pytest.mark.parametrize("event_actor,expected", [("delivery[bot]", True), ("other[bot]", False)])
def test_app_supersession_requires_own_intent_and_event(app_publisher, monkeypatch, event_actor, expected):
    body = ("<!-- agent-run:agent-run-status -->\n## Superseded Change Generation\n\n"
            "- Scope: `superseded_generation`\n- Generation: `2`\n"
            "- Retirement: `closing`\n- Close nonce: `nonce`")

    def api(*arguments, **kwargs):
        assert arguments[:2] != ("api", "user")
        if arguments[1].endswith("comments"):
            return [[{"body": body, "user": {"login": "delivery[bot]"}}]]
        if arguments[1].endswith("events"):
            return [[{"id": 4, "event": "closed", "actor": {"login": event_actor}}]]
        raise AssertionError(arguments)

    monkeypatch.setattr(app_publisher, "_json", api)
    monkeypatch.setattr(app_publisher, "live_pull_request", lambda _: {"state": "CLOSED"})
    assert app_publisher.has_supersession_close_receipt(10, 2, "nonce") is expected


def test_app_ticket_close_rechecks_prepared_actor_before_dispatch(app_publisher, monkeypatch):
    monkeypatch.setattr(app_publisher, "_require", lambda *_: pytest.fail("must not write"))
    with pytest.raises(GitHubReadError, match="different Publisher identity"):
        app_publisher.close_primary_ticket(ticket_number=2, run_id="run-1", pr_number=3,
                                           integrated_sha="abc", close_intent={
                                               "actor": "other[bot]", "intent_binding": "pr-3:sha-abc",
                                           })


@pytest.mark.parametrize("closing_actor", ["delivery[bot]", "maintainer"])
def test_app_close_lost_response_reconciles_actor_without_second_write(app_publisher, monkeypatch, closing_actor):
    comments = []
    events = []
    writes = []
    closed = False
    stamp = "2026-09-28T10:00:00Z"

    def api(*arguments, **kwargs):
        assert arguments[:2] != ("api", "user")
        if arguments[:2] == ("issue", "view"):
            return {"state": "CLOSED" if closed else "OPEN", "updatedAt": stamp}
        if arguments[1].endswith("comments"):
            return [comments]
        if arguments[1].endswith("events"):
            return events
        raise AssertionError(arguments)

    def write(*arguments):
        nonlocal closed
        writes.append(arguments[:2])
        if arguments[:2] == ("issue", "comment"):
            comments.append({"body": arguments[-1], "created_at": stamp,
                             "user": {"login": "delivery[bot]"}})
        elif arguments[:2] == ("issue", "close"):
            closed = True
            events.append({"id": 1, "event": "closed", "created_at": stamp,
                           "actor": {"login": closing_actor}})
            raise OSError("lost close response")
        else:
            raise AssertionError(arguments)

    monkeypatch.setattr(app_publisher, "_json", api)
    monkeypatch.setattr(app_publisher, "_require", write)
    authority = dict(ticket_number=2, run_id="run-1", pr_number=3, integrated_sha="abc")
    intent = app_publisher.prepare_primary_ticket_close(**authority)
    with pytest.raises(OSError, match="lost close"):
        app_publisher.close_primary_ticket(**authority, close_intent=intent)
    if closing_actor == "delivery[bot]":
        ownership = app_publisher.close_primary_ticket(**authority, close_intent=intent)
        assert ownership["actor"] == closing_actor
        assert ownership["event_id"] == 1
    else:
        with pytest.raises(GitHubReadError, match="conflicts"):
            app_publisher.close_primary_ticket(**authority, close_intent=intent)
    assert writes == [("issue", "comment"), ("issue", "close")]


@pytest.mark.parametrize("rest_author", ["delivery[bot]", "delivery"])
def test_app_parent_readback_uses_rest_author_not_ambiguous_graphql_slug(app_publisher, monkeypatch, rest_author):
    comments = []
    writes = []
    closed = False

    def api(*arguments, **kwargs):
        if arguments[:2] == ("issue", "view"):
            # A GraphQL bot and a personal account can both have this login.
            return {"state": "CLOSED" if closed else "OPEN",
                    "comments": [{"body": "ambiguous", "author": {"login": "delivery"}}]}
        if arguments[1].endswith("comments"):
            return [comments]
        if arguments[1].endswith("events"):
            return [[{"id": 1, "event": "closed", "actor": {"login": "delivery[bot]"},
                      "created_at": "2026-09-28T10:00:01Z"}]] if closed else [[]]
        raise AssertionError(arguments)

    def write(*arguments):
        nonlocal closed
        writes.append(arguments[1])
        if arguments[1] == "comment":
            comments.append({"id": 1, "body": arguments[-1], "user": {"login": rest_author},
                             "created_at": "2026-09-28T10:00:00Z"})
        else:
            assert arguments[1] == "close"
            closed = True

    monkeypatch.setattr(app_publisher, "_json", api)
    monkeypatch.setattr(app_publisher, "_require", write)
    authority = dict(parent_number=1, run_id="run-1", pr_number=3,
                     integrated_sha="abc", delivery_type="Final Run")
    if rest_author == "delivery[bot]":
        app_publisher.close_parent_issue(**authority)
        assert writes == ["comment", "close"]
    else:
        with pytest.raises(GitHubReadError, match="intent comment is not visible"):
            app_publisher.close_parent_issue(**authority)
        assert writes == ["comment"]


def test_public_app_config_drives_native_publisher_unknown_write(app_publisher, tmp_path, monkeypatch):
    """Keep real config, Publisher, renewal and subprocess; fake only external I/O."""
    import os
    import sys
    import time

    from agent_run.credential_lifecycle import ReadCredential
    from agent_run.github_identity import resolve_runner_identity
    from agent_run.runner_credentials import runner_credential_scope
    from agent_run.user_defaults import UserDefaultsStore
    from support.user_defaults import settings

    profile = app_publisher.git.runner_identity["app_profile"]
    code, result = settings("configure", "--runner-github-source", "app", "--worker-github-source", "app",
                            "--github-app-id", profile["app_id"], "--github-installation-id", profile["installation_id"],
                            "--github-private-key-path", profile["private_key_path"], "--github-app-read-only")
    assert code == 0, result
    app_publisher.git.runner_identity = resolve_runner_identity(UserDefaultsStore().load()["github"])
    cancelled = []

    class Provider:
        def __init__(self, selected_profile, **scope):
            assert selected_profile.app_id == profile["app_id"]
            assert scope == {}  # The configured Worker restriction does not reach Runner.
        def __call__(self):
            return ReadCredential("runner-full-token", time.time() + 3600)
        def cancel(self):
            cancelled.append(True)

    monkeypatch.setattr("agent_run.runner_credentials._GitHubAppCredentialProvider", Provider)
    tools = tmp_path / "tools"
    tools.mkdir()
    capture = tmp_path / "native-calls.jsonl"
    remote = tmp_path / "comments.json"
    gh = tools / "gh"
    gh.write_text(f"#!{sys.executable}\n" + f'''import json, os, sys
from pathlib import Path
args = sys.argv[1:]
assert args[0] == "api" and "comments" in args[1], args
assert os.environ["GH_TOKEN"] == "runner-full-token"
assert "GITHUB_TOKEN" not in os.environ
with Path({str(capture)!r}).open("a") as log:
    log.write(json.dumps(args) + "\\n")
remote = Path({str(remote)!r})
comments = json.loads(remote.read_text()) if remote.exists() else []
if "-f" in args:
    body = next(arg[5:] for arg in args if arg.startswith("body="))
    comments.append({{"id": 1, "body": body, "user": {{"login": "delivery[bot]"}}}})
    remote.write_text(json.dumps(comments))
    print("response lost", file=sys.stderr)
    sys.exit(1)
print(json.dumps([comments]))
''')
    gh.chmod(0o700)
    monkeypatch.setenv("PATH", str(tools) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("GH_TOKEN", "host-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "host-secret")
    with runner_credential_scope():
        with pytest.raises(GitHubReadError, match="response lost"):
            app_publisher.record_run_publication(10, {"run_id": "run-1", "candidate_sha": "abc"})
        app_publisher.record_run_publication(10, {"run_id": "run-1", "candidate_sha": "abc"})
    assert cancelled
    calls = [json.loads(line) for line in capture.read_text().splitlines()]
    assert sum("-f" in call for call in calls) == 1
    assert os.environ["GH_TOKEN"] == "host-secret"
