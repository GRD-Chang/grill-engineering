"""Status batches one physical read, retaining each independent proof round."""
from copy import deepcopy
from dataclasses import replace
import json
import subprocess

import pytest

from agent_run.adapters import github_commands
from agent_run.adapters.remote import EffectReadError, RemoteEffects
from agent_run.contracts.status_comment import StatusTarget
from agent_run.contracts.write_authority import WriteAuthority, admit_write
from agent_run.errors import GitHubReadError


@pytest.fixture
def observation(tmp_path, monkeypatch):
    target = StatusTarget("example/repository", "run", 1, "ticket:2", 1, 2, 7,
                          "publisher", "effect", "receipt", "refs/heads/source", "refs/heads/main")
    marker = f"<!-- agent-run-status:{target.key}:create -->"
    intent = {"comment_id": None, "before": None, "body": marker + "\nCurrent status"}
    port = RemoteEffects(tmp_path, target.repository, runner_identity={"source": "host"})
    calls, responses = [], []

    def read(command, before_attempt=None):
        if before_attempt is not None:
            before_attempt()
        calls.append(command)
        assert command[:3] == ["gh", "api", "graphql"], "Status must use one combined physical read"
        assert "--paginate" in command and "--slurp" in command
        value = responses.pop(0)
        if isinstance(value, BaseException):
            raise value
        return subprocess.CompletedProcess(command, 0, json.dumps(value), "")

    monkeypatch.setattr(port, "_read", read)
    return port, target, marker, intent, calls, responses


def comment(target, identifier, body):
    repository = {"nameWithOwner": target.repository}
    return {"fullDatabaseId": identifier, "body": body, "author": {"__typename": "User", "login": target.actor},
            "repository": repository,
            "pullRequest": {"number": target.pr_number, "repository": dict(repository)}}


def pages(target, rows):
    result = []
    for offset in range(0, max(1, len(rows)), 100):
        nodes = rows[offset:offset + 100]
        result.append({"data": {"viewer": {"login": target.actor}, "repository": {
            "nameWithOwner": target.repository,
            "pullRequest": {"number": target.pr_number, "author": {"__typename": "User", "login": target.actor},
                "headRefName": target.source_ref.removeprefix("refs/heads/"),
                "baseRefName": target.target_ref.removeprefix("refs/heads/"),
                "headRepository": {"nameWithOwner": target.repository},
                "baseRepository": {"nameWithOwner": target.repository},
                "comments": {"totalCount": len(rows), "nodes": nodes,
                    "pageInfo": {"endCursor": str(offset + len(nodes)) if nodes else None,
                                 "hasNextPage": offset + len(nodes) < len(rows)}}}}}})
    return result


@pytest.mark.parametrize("source", ["host", "pat"])
def test_status_round_uses_one_complete_request_and_reloads_next_round(observation, source):
    port, target, marker, intent, calls, responses = observation
    if source == "pat":
        port.identity = {"source": "pat", "pat": {"env": "STATUS_TEST_TOKEN"}}
    responses.extend([pages(target, []), pages(target, [comment(target, 71, intent["body"])])])
    assert port.read_status_comment(target, marker, intent, lambda: False) == {"disposition": "prior"}
    assert len(calls) == 1
    assert port.read_status_comment(target, marker, intent, lambda: False) == {
        "disposition": "target", "comment_id": 71}
    assert len(calls) == 2  # Later rounds never reuse the earlier empty collection.


def test_status_batch_keeps_initial_prewrite_and_readback_observations(observation, monkeypatch):
    port, target, marker, intent, calls, responses = observation
    responses.extend([pages(target, []), pages(target, []),
                      pages(target, [comment(target, 71, intent["body"])])])
    events = []

    def write(endpoint, method, body, authorize, boundary):
        admit_write(authorize, boundary)
        events.append((method, endpoint, len(calls)))
        return subprocess.CompletedProcess([], 0, "{}", "")

    monkeypatch.setattr(port, "_write", write)
    authority = WriteAuthority(lambda: lambda: events.append(("admit", len(calls))))
    assert port.read_status_comment(target, marker, intent, lambda: False)["disposition"] == "prior"
    assert port.write_status_comment(target, marker, intent, authority, lambda: False) == {
        "disposition": "target", "comment_id": 71, "write_returncode": 0}
    assert events == [("admit", 2), ("POST", "repos/example/repository/issues/7/comments", 2)]
    assert len(calls) == 3


@pytest.mark.parametrize("returncode", [0, 1])
def test_status_batch_unavailable_readback_keeps_original_write_result(observation, monkeypatch, returncode):
    port, target, marker, intent, calls, responses = observation
    responses.extend([pages(target, []), GitHubReadError("read_failed", "Unavailable")])
    writes = []

    def write(endpoint, method, body, authorize, boundary):
        admit_write(authorize, boundary)
        writes.append(method)
        return subprocess.CompletedProcess([], returncode, "", "lost response")

    monkeypatch.setattr(port, "_write", write)
    with pytest.raises(EffectReadError) as caught:
        port.write_status_comment(target, marker, intent, WriteAuthority(lambda: lambda: None), lambda: False)
    assert caught.value.write_returncode == returncode
    assert writes == ["POST"] and len(calls) == 2


@pytest.mark.parametrize("owner,name", [("true", "false"), ("null", "123")])
def test_status_repository_variables_remain_literal_strings(observation, owner, name):
    port, target, marker, intent, calls, responses = observation
    target = replace(target, repository=f"{owner}/{name}")
    port.repository = target.repository
    responses.append(pages(target, []))
    assert port.read_status_comment(target, marker, intent, lambda: False)["disposition"] == "prior"
    command, = calls
    for value in (f"owner={owner}", f"name={name}"):
        assert command[command.index(value) - 1] == "-f"


def test_status_app_keeps_actual_resolver_and_does_not_require_viewer(observation, monkeypatch):
    port, target, marker, intent, calls, responses = observation
    target = replace(target, actor="publisher[bot]")
    port.identity = {"source": "app", "app_profile": {"app_id": "1", "installation_id": "2",
        "private_key_path": "/nonexistent/status-test-key"}, "app_read_only": False,
        "app_current_repository_only": False}
    resolved = []
    monkeypatch.setattr("agent_run.adapters.auth.app.resolve_app_login",
                        lambda profile: resolved.append(profile) or target.actor)
    payload = pages(target, [comment(target, 71, intent["body"])])
    payload[0]["data"].pop("viewer")
    pr = payload[0]["data"]["repository"]["pullRequest"]
    pr["author"] = {"__typename": "Bot", "login": "publisher"}
    pr["comments"]["nodes"][0]["author"] = {"__typename": "Bot", "login": "publisher"}
    responses.append(payload)
    assert port.read_status_comment(target, marker, intent, lambda: False) == {
        "disposition": "target", "comment_id": 71}
    assert len(resolved) == len(calls) == 1
    query = next(arg for arg in calls[0] if arg.startswith("query="))
    assert "viewer" not in query
    assert query.count("__typename") == 2  # Both PR and comment authors carry their Actor type.


@pytest.mark.parametrize("location", ["pr", "comment"])
def test_status_same_login_user_and_bot_are_distinct(observation, location):
    port, target, marker, intent, calls, responses = observation
    payload = pages(target, [comment(target, 71, intent["body"])])
    pr = payload[0]["data"]["repository"]["pullRequest"]
    row = pr if location == "pr" else pr["comments"]["nodes"][0]
    row["author"] = {"__typename": "Bot", "login": target.actor}
    responses.append(payload)
    assert port.read_status_comment(target, marker, intent, lambda: False)["disposition"] == "third"
    assert len(calls) == 1


@pytest.mark.parametrize("location", ["pr", "comment"])
@pytest.mark.parametrize("typename", [None, "", 1, "UnknownActor"])
def test_status_author_type_must_be_observed(observation, location, typename):
    port, target, marker, intent, calls, responses = observation
    payload = pages(target, [comment(target, 71, intent["body"])])
    pr = payload[0]["data"]["repository"]["pullRequest"]
    author = (pr if location == "pr" else pr["comments"]["nodes"][0])["author"]
    if typename is None:
        author.pop("__typename")
    else:
        author["__typename"] = typename
    responses.append(payload)
    with pytest.raises((ValueError, GitHubReadError)):
        port.read_status_comment(target, marker, intent, lambda: False)
    assert len(calls) == 1


@pytest.mark.parametrize("identifier", [9007199254740993, "9007199254740993"])
def test_status_preserves_exact_comment_identity_above_json_float_range(observation, identifier):
    port, target, marker, intent, calls, responses = observation
    responses.append(pages(target, [comment(target, identifier, intent["body"])]))
    assert port.read_status_comment(target, marker, intent, lambda: False) == {
        "disposition": "target", "comment_id": 9007199254740993}
    assert len(calls) == 1


@pytest.mark.parametrize("identifier", [True, False, 1.5, 0, -1, "1.0", " 1", "invalid", None])
def test_status_rejects_unusable_numeric_comment_identity(observation, identifier):
    port, target, marker, intent, calls, responses = observation
    responses.append(pages(target, [comment(target, identifier, intent["body"])]))
    with pytest.raises((ValueError, GitHubReadError)):
        port.read_status_comment(target, marker, intent, lambda: False)
    assert len(calls) == 1


def test_status_owned_comment_after_first_page_is_still_observed(observation):
    port, target, marker, intent, calls, responses = observation
    rows = [comment(target, number, "Unrelated comment") for number in range(1, 102)]
    rows.append(comment(target, 200, intent["body"]))
    responses.append(pages(target, rows))
    assert port.read_status_comment(target, marker, intent, lambda: False) == {
        "disposition": "target", "comment_id": 200}
    assert len(calls) == 1


@pytest.mark.parametrize("author", [None, {"__typename": "Mannequin", "login": "publisher"}])
def test_status_unrelated_unadoptable_author_does_not_hide_owned_comment(observation, author):
    port, target, marker, intent, calls, responses = observation
    unrelated = comment(target, 70, "Unrelated comment")
    unrelated["author"] = author
    responses.append(pages(target, [unrelated, comment(target, 71, intent["body"])]))
    assert port.read_status_comment(target, marker, intent, lambda: False) == {
        "disposition": "target", "comment_id": 71}
    assert len(calls) == 1


@pytest.mark.parametrize("fault", ["partial", "null-data", "null-repository", "null-pr", "null-viewer",
                                   "null-head-repository", "null-comments", "null-node", "inconsistent-comment-owner"])
def test_status_incomplete_packet_never_becomes_absence_or_foreign(observation, fault):
    port, target, marker, intent, calls, responses = observation
    payload = pages(target, [comment(target, 71, intent["body"])])
    data = payload[0]["data"]
    pr = data["repository"]["pullRequest"]
    if fault == "partial":
        payload[0]["errors"] = [{"message": "unavailable", "path": ["repository", "pullRequest", "comments"]}]
    elif fault == "null-data":
        payload[0]["data"] = None
    elif fault == "null-repository":
        data["repository"] = None
    elif fault == "null-pr":
        data["repository"]["pullRequest"] = None
    elif fault == "null-viewer":
        data["viewer"] = None
    elif fault == "null-head-repository":
        pr["headRepository"] = None
    elif fault == "null-comments":
        pr["comments"] = None
    elif fault == "null-node":
        pr["comments"]["nodes"][0] = None
    else:
        pr["comments"]["nodes"][0]["repository"]["nameWithOwner"] = "foreign/repository"
    responses.append(payload)
    with pytest.raises((ValueError, GitHubReadError)):
        port.read_status_comment(target, marker, intent, lambda: False)
    assert len(calls) == 1


@pytest.mark.parametrize("fault", ["missing-page", "extra-page", "duplicate-id", "repeated-cursor",
                                   "count-drift", "missing-member", "pr-drift", "actor-drift"])
def test_status_requires_complete_stable_paginated_collection(observation, fault):
    port, target, marker, intent, calls, responses = observation
    payload = pages(target, [comment(target, n, "Unrelated comment") for n in range(1, 102)])
    first = payload[0]["data"]["repository"]["pullRequest"]
    last = payload[1]["data"]["repository"]["pullRequest"]
    if fault == "missing-page":
        payload.pop()
    elif fault == "extra-page":
        payload.append(deepcopy(payload[-1]))
    elif fault == "duplicate-id":
        last["comments"]["nodes"][0]["fullDatabaseId"] = 1
    elif fault == "repeated-cursor":
        last["comments"]["pageInfo"]["endCursor"] = first["comments"]["pageInfo"]["endCursor"]
    elif fault == "count-drift":
        last["comments"]["totalCount"] += 1
    elif fault == "missing-member":
        last["comments"]["nodes"] = []
    elif fault == "pr-drift":
        last["baseRefName"] = "other"
    else:
        payload[1]["data"]["viewer"]["login"] = "other"
    responses.append(payload)
    with pytest.raises((ValueError, GitHubReadError)):
        port.read_status_comment(target, marker, intent, lambda: False)
    assert len(calls) == 1


@pytest.mark.parametrize("fault", ["actor", "pr-author", "null-pr-author", "source-repository", "target-ref", "comment-owner", "null-comment-author", "mannequin-comment-author",
                                   "comment-pr", "comment-repository", "edited", "duplicate-marker"])
def test_status_complete_foreign_evidence_still_refuses_original_intent(observation, fault):
    port, target, marker, intent, calls, responses = observation
    payload = pages(target, [comment(target, 71, intent["body"])])
    data = payload[0]["data"]
    pr = data["repository"]["pullRequest"]
    row = pr["comments"]["nodes"][0]
    if fault == "actor":
        data["viewer"]["login"] = "someone-else"
    elif fault == "pr-author":
        pr["author"]["login"] = "someone-else"
    elif fault == "null-pr-author":
        pr["author"] = None
    elif fault == "source-repository":
        pr["headRepository"]["nameWithOwner"] = "foreign/repository"
    elif fault == "target-ref":
        pr["baseRefName"] = "other"
    elif fault == "comment-owner":
        row["author"]["login"] = "someone-else"
    elif fault == "null-comment-author":
        row["author"] = None
    elif fault == "mannequin-comment-author":
        row["author"] = {"__typename": "Mannequin", "login": target.actor}
    elif fault == "comment-pr":
        row["pullRequest"]["number"] += 1
    elif fault == "comment-repository":
        row["repository"]["nameWithOwner"] = "foreign/repository"
        row["pullRequest"]["repository"]["nameWithOwner"] = "foreign/repository"
    elif fault == "edited":
        row["body"] += "\nHuman edit"
    else:
        pr["comments"]["nodes"].append({**row, "fullDatabaseId": 72})
        pr["comments"]["totalCount"] = 2
    responses.append(payload)
    assert port.read_status_comment(target, marker, intent, lambda: False)["disposition"] == "third"
    assert len(calls) == 1


@pytest.mark.parametrize("error", [TimeoutError("watchdog"), InterruptedError("cancelled"), OSError("close")])
def test_status_capture_failure_keeps_original_exception_and_does_not_retry(observation, error):
    port, target, marker, intent, calls, responses = observation
    responses.append(error)
    with pytest.raises(type(error)) as caught:
        port.read_status_comment(target, marker, intent, lambda: False)
    assert caught.value is error
    assert len(calls) == 1


@pytest.mark.parametrize("error", [
    github_commands.ReadOutputOverflow("github_invalid_response", "Complete output too large"),
    GitHubReadError("github_read_failed", "Incomplete observation"),
])
def test_status_failed_complete_read_is_not_split_or_retried(observation, error):
    port, target, marker, intent, calls, responses = observation
    responses.append(error)
    with pytest.raises(type(error)) as caught:
        port.read_status_comment(target, marker, intent, lambda: False)
    assert caught.value is error
    assert len(calls) == 1
