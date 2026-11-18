"""Exact Status comment observation and its single authorized POST/PATCH.

The delivery journal owns intents and receipts. This adapter has only GitHub
reads and one comment write; business Effects and their reconciliation stay out.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
import json
from pathlib import Path
import re
import subprocess
from tempfile import NamedTemporaryFile
from typing import Any, Protocol
from urllib.parse import urlsplit

from agent_run.adapters.github_transport import GitHubReadTransport, ReadAPI
from agent_run.adapters.github_commands import run_write_command
from agent_run.adapters.operation_errors import EffectReadError
from agent_run.contracts.repository import same_repository
from agent_run.contracts.status_comment import StatusTarget
from agent_run.contracts.write_authority import WriteAuthority, admit_write
from agent_run.errors import GitHubReadError
from agent_run.storage.sqlite import MATERIAL_LIMIT


class CommentAPI(ReadAPI, Protocol):
    def write(self, endpoint: str, method: str, body: Mapping[str, Any]) -> subprocess.CompletedProcess[str]: ...


_STATUS_COMMENT_QUERY = """query StatusCommentObservation(
  $owner:String!, $name:String!, $number:Int!, $endCursor:String) {
  VIEWER
  repository(owner:$owner,name:$name) { nameWithOwner
    pullRequest(number:$number) { number author { __typename login }
      headRefName headRefOid baseRefName headRepository { nameWithOwner } baseRepository { nameWithOwner }
      comments(first:100,after:$endCursor) { totalCount pageInfo { endCursor hasNextPage }
        nodes { fullDatabaseId body author { __typename login } repository { nameWithOwner }
          pullRequest { number repository { nameWithOwner } } }
      }
    }
  }
}"""


class StatusCommentTransport:
    """Compose a read-only transport with the sole permitted comment mutation."""
    def __init__(self, root: Path, repository: str, *, runner_identity: Mapping[str, Any],
                 api: CommentAPI | None = None, cancelled: Callable[[], bool] | None = None) -> None:
        self.reads = GitHubReadTransport(root, repository, runner_identity=runner_identity,
                                        api=api, cancelled=cancelled)
        self.api = api

    def read_status_comment(self, target: StatusTarget, marker: str, intent: Mapping[str, Any],
                            cancelled: Callable[[], bool], *, expected_head: str | None = None) -> dict[str, Any]:
        """Observe one exact display intent. Foreign/absent is never write authority."""
        def check_cancel() -> None:
            if cancelled():
                raise ValueError("Status delivery cancelled")
        check_cancel()
        if not same_repository(target.repository, self.reads.repository):
            return {"disposition": "third"}
        actor, pr, rows = self._status_comment_facts(target, check_cancel)
        check_cancel()
        if actor != target.actor:
            return {"disposition": "third"}
        head, base = pr.get("head", {}), pr.get("base", {})
        if (not isinstance(head, dict) or not isinstance(base, dict)
                or not isinstance(head.get("repo"), dict) or not isinstance(base.get("repo"), dict)
                or pr.get("number") != target.pr_number or _login(pr.get("user")) != target.actor
                or not isinstance(head["repo"].get("full_name"), str)
                or not same_repository(head["repo"]["full_name"], self.reads.repository)
                or not isinstance(base["repo"].get("full_name"), str)
                or not same_repository(base["repo"]["full_name"], self.reads.repository)
                or "refs/heads/" + str(head.get("ref")) != target.source_ref
                or "refs/heads/" + str(base.get("ref")) != target.target_ref):
            return {"disposition": "third"}
        if expected_head is not None and head.get("sha") != expected_head:
            return {"disposition": "third"}
        check_cancel()
        prefix = f"<!-- agent-run-status:{target.key}:"
        found = [row for row in rows if row.get("id") == intent["comment_id"]
                 or isinstance(row.get("body"), str) and prefix in row["body"]]
        if not found:
            return {"disposition": "prior" if intent["comment_id"] is None else "third"}
        if len(found) != 1:
            return {"disposition": "third"}
        row = found[0]
        issue_path = urlsplit(str(row.get("issue_url", ""))).path.split("/")
        if (type(row.get("id")) is not int or row["id"] <= 0
                or intent["comment_id"] is not None and row["id"] != intent["comment_id"]
                or _login(row.get("user")) != target.actor
                or len(issue_path) != 6 or issue_path[:2] != ["", "repos"]
                or issue_path[4:] != ["issues", str(target.pr_number)]
                or not same_repository("/".join(issue_path[2:4]), self.reads.repository)
                or not isinstance(row.get("body"), str) or not row["body"].startswith(marker + "\n")):
            return {"disposition": "third"}
        disposition = ("target" if row["body"] == intent["body"] else "prior"
                       if intent["comment_id"] is not None and row["body"] == intent["before"] else "third")
        return {"disposition": disposition, "comment_id": row["id"]}

    def _status_comment_facts(self, target: StatusTarget, check_cancel: Callable[[], None]
                              ) -> tuple[str, dict[str, Any], list[dict[str, Any]]]:
        """One complete round; initial, pre-write and readback calls stay independent."""
        if self.api is not None:
            return (self.reads.actor(check_cancel),
                    self.reads.object(f"repos/{self.reads.repository}/pulls/{target.pr_number}", check_cancel),
                    self.reads.list(f"repos/{self.reads.repository}/issues/{target.pr_number}/comments?per_page=100", check_cancel))
        app_actor = self.reads.actor(check_cancel) if self.reads.identity["source"] == "app" else None
        owner, name = self.reads.repository.split("/", 1)
        query = _STATUS_COMMENT_QUERY.replace("VIEWER", "" if app_actor is not None else "viewer { login }")
        command = ["gh", "api", "graphql", "--paginate", "--slurp", "-f", "query=" + query,
                   "-f", "owner=" + owner, "-f", "name=" + name, "-F", "number=" + str(target.pr_number)]
        pages = json.loads(self.reads.read(command, check_cancel).stdout)
        check_cancel()
        return _status_comment_pages(pages, self.reads.repository, app_actor)

    def write_status_comment(self, target: StatusTarget, marker: str, intent: Mapping[str, Any],
                             authorize: WriteAuthority, cancelled: Callable[[], bool], *,
                             expected_head: str | None = None) -> dict[str, Any]:
        """Only POST/PATCH this comment. No retry, deletion, merge or Issue mutation."""
        def boundary() -> None:
            if self.read_status_comment(target, marker, intent, cancelled, expected_head=expected_head)["disposition"] != "prior":
                raise ValueError("Status comment changed before write")
        identifier = intent["comment_id"]
        endpoint, method = (f"issues/{target.pr_number}/comments", "POST") if identifier is None else (f"issues/comments/{identifier}", "PATCH")
        response = self._write(f"repos/{self.reads.repository}/{endpoint}", method, {"body": intent["body"]}, authorize, boundary)
        try:
            result = self.read_status_comment(target, marker, intent, cancelled)
        except (GitHubReadError, OSError, ValueError) as error:
            raise EffectReadError(GitHubReadError("status_readback_unavailable", "Status readback unavailable"), response) from error
        return {**result, "write_returncode": response.returncode}

    def _write(self, endpoint: str, method: str, body: Mapping[str, Any],
               authorize: WriteAuthority, boundary: Callable[[], None]) -> subprocess.CompletedProcess[str]:
        data = json.dumps(dict(body), ensure_ascii=False).encode()
        if len(data) > MATERIAL_LIMIT:
            raise ValueError("Status write material exceeds limit")
        if self.api is not None:
            admit_write(authorize, boundary)
            return self.api.write(endpoint, method, body)
        with NamedTemporaryFile(prefix="agent-run-status-", suffix=".json") as stream:
            stream.write(data)
            stream.flush()
            return run_write_command(["gh", "api", endpoint, "--method", method, "--input", stream.name],
                authorize=lambda: admit_write(authorize, boundary), cwd=self.reads.root,
                runner_identity=self.reads.identity,
                **({"cancelled": self.reads.cancelled} if self.reads.cancelled is not None else {}))


def _status_author(value: Any) -> dict[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("Missing Status author")
    kind, login = _text(value.get("__typename")), _text(value.get("login"))
    # GraphQL Bot.login is the App slug; REST and the App resolver use [bot].
    # Other Actor kinds (e.g. Mannequin) cannot adopt a User or Bot identity.
    if kind == "Mannequin":
        return None
    if kind not in {"User", "Bot"}:
        raise ValueError("Unknown Status author type")
    if login.endswith("[bot]"):
        raise ValueError("Invalid GraphQL author login")
    return {"login": login + "[bot]" if kind == "Bot" else login}


def _status_comment_pages(pages: Any, repository: str, app_actor: str | None
                          ) -> tuple[str, dict[str, Any], list[dict[str, Any]]]:
    """Validate one complete GH pagination result before the existing selector."""
    if not isinstance(pages, list) or not pages:
        raise ValueError("Status observation requires complete pages")
    rows: list[dict[str, Any]] = []
    identifiers: set[int] = set()
    cursors: set[str] = set()
    binding = None
    for index, page in enumerate(pages):
        if not isinstance(page, dict) or page.get("errors", []) != []:
            raise ValueError("Incomplete Status observation")
        data = page.get("data")
        repo = data.get("repository") if isinstance(data, dict) else None
        pr = repo.get("pullRequest") if isinstance(repo, dict) else None
        if not isinstance(data, dict) or not isinstance(repo, dict) or not isinstance(pr, dict):
            raise ValueError("Missing Status publication")
        canonical = _text(repo.get("nameWithOwner"))
        if not same_repository(canonical, repository):
            raise ValueError("Status repository changed")
        actor = app_actor if app_actor is not None else _login(data.get("viewer"))
        actor = _text(actor)
        head, base = pr.get("headRepository"), pr.get("baseRepository")
        if (not isinstance(head, dict) or not isinstance(base, dict) or "author" not in pr
                or pr["author"] is not None and _login(pr["author"]) is None):
            raise ValueError("Missing Status publication repository")
        observed = {"number": _number(pr.get("number")), "user": _status_author(pr.get("author")),
                    "head": {"ref": _text(pr.get("headRefName")), "sha": pr.get("headRefOid"), "repo": {"full_name": _text(head.get("nameWithOwner"))}},
                    "base": {"ref": _text(pr.get("baseRefName")), "repo": {"full_name": _text(base.get("nameWithOwner"))}}}
        comments = pr.get("comments")
        nodes = comments.get("nodes") if isinstance(comments, dict) else None
        info = comments.get("pageInfo") if isinstance(comments, dict) else None
        total = comments.get("totalCount") if isinstance(comments, dict) else None
        if (not isinstance(nodes, list) or len(nodes) > 100 or not isinstance(info, dict)
                or type(total) is not int or total < 0 or type(info.get("hasNextPage")) is not bool
                or info["hasNextPage"] != (index < len(pages) - 1)):
            raise ValueError("Incomplete Status comment pagination")
        current = (actor, observed, total)
        if binding is not None and current != binding:
            raise ValueError("Status publication changed during pagination")
        binding = current
        cursor = info.get("endCursor")
        if nodes:
            cursor = _text(cursor)
            if cursor in cursors:
                raise ValueError("Status comment cursor did not advance")
            cursors.add(cursor)
        elif total != 0 or len(pages) != 1 or cursor is not None:
            raise ValueError("Incomplete Status comment page")
        for node in nodes:
            if (not isinstance(node, dict) or "author" not in node
                    or node["author"] is not None and _login(node["author"]) is None):
                raise ValueError("Missing Status comment")
            identifier = node.get("fullDatabaseId")
            if isinstance(identifier, str) and re.fullmatch(r"[1-9][0-9]*", identifier):
                identifier = int(identifier)
            identifier = _number(identifier)
            if identifier in identifiers:
                raise ValueError("Duplicate Status comment identity")
            identifiers.add(identifier)
            comment_repo, pull = node.get("repository"), node.get("pullRequest")
            pull_repo = pull.get("repository") if isinstance(pull, dict) else None
            if not isinstance(comment_repo, dict) or not isinstance(pull, dict) or not isinstance(pull_repo, dict):
                raise ValueError("Missing Status comment owner")
            owner = _text(comment_repo.get("nameWithOwner"))
            if not same_repository(owner, _text(pull_repo.get("nameWithOwner"))):
                raise ValueError("Inconsistent Status comment owner")
            rows.append({"id": identifier, "body": _string(node.get("body")), "user": _status_author(node.get("author")),
                         "issue_url": f"https://api.github.com/repos/{owner}/issues/{_number(pull.get('number'))}"})
    assert binding is not None
    if len(rows) != binding[2] or len(json.dumps(rows).encode()) > MATERIAL_LIMIT:
        raise ValueError("Incomplete or oversized Status comment collection")
    return binding[0], binding[1], rows


def _login(value: Any) -> str | None:
    login = value.get("login") if isinstance(value, Mapping) else None
    return login if isinstance(login, str) and login.strip() else None


def _string(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("expected remote string")
    return value


def _text(value: Any) -> str:
    result = _string(value)
    if not result.strip():
        raise ValueError("expected nonempty remote string")
    return result


def _number(value: Any) -> int:
    if type(value) is not int or value < 1:
        raise ValueError("expected positive remote integer")
    return value
