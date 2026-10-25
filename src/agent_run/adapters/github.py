from __future__ import annotations

import json
import subprocess
from pathlib import Path
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlparse

from agent_run.adapters.auth.runner_transport import runner_options
from agent_run.adapters.github_commands import run_read_command
from agent_run.runtime.git_command import git_environment
from agent_run.model import Requirements, Repository
from agent_run.errors import GitHubReadError
from agent_run.model import Blocker, Issue, ParentIssue


class GhGitHubReader:
    def __init__(
        self,
        repository_override: str | None = None,
        *,
        working_directory: Path | None = None,
        runner_identity: Mapping[str, Any] | None = None,
    ) -> None:
        self.runner_identity = runner_identity
        self.repository_override = repository_override
        self.working_directory = working_directory
        self._repository: Repository | None = None

    def repository_hint(self) -> str | None:
        identity = self.repository_override or _repository_hint_from_origin(
            self.working_directory
        )
        return identity.lower() if identity else None

    def repository(self) -> Repository:
        arguments = ["repo", "view"]
        if self.repository_override:
            arguments.append(self.repository_override)
        arguments.extend(["--json", "nameWithOwner,defaultBranchRef"])
        data = self._gh_json(*arguments)
        name_with_owner = _string(data, "nameWithOwner")
        default_ref = _mapping(data, "defaultBranchRef")
        default_branch = _string(default_ref, "name")
        commit = self._gh_json(
            "api",
            f"repos/{name_with_owner}/git/ref/heads/{quote(default_branch, safe='')}",
        )
        if (_string(commit, "ref") != f"refs/heads/{default_branch}"
                or _string(_mapping(commit, "object"), "type") != "commit"):
            raise GitHubReadError("github_invalid_response", "Default branch ref has another identity or type")
        self._repository = Repository(
            name_with_owner=name_with_owner,
            default_branch=default_branch,
            default_head_sha=_string(_mapping(commit, "object"), "sha"),
        )
        return self._repository

    def requirements(self, parent_number: int) -> Requirements:
        repository = self.repository()
        owner, name = repository.name_with_owner.split("/", 1)
        parent = self._read_parent(owner, name, parent_number)
        issues = tuple(self._read_issue(owner, name, number, node_id, parent)
                       for number, node_id in parent.tickets)
        return Requirements(parent.value, issues)

    def _read_parent(self, owner: str, name: str, number: int) -> _ParentFacts:
        query = """
        query($owner:String!,$name:String!,$number:Int!,$cursor:String) {
          repository(owner:$owner,name:$name) {
            id nameWithOwner
            issue(number:$number) {
              id number title body repository { id nameWithOwner }
              subIssues(first:100,after:$cursor) {
                totalCount
                nodes { id number repository { id nameWithOwner }
                        parent { id number repository { id nameWithOwner } } }
                pageInfo { hasNextPage endCursor }
              }
            }
          }
        }
        """
        selected = f"{owner}/{name}"
        children = _SourceConnection("subIssues")
        snapshot = None
        tickets: list[tuple[int, str]] = []
        while not children.done:
            issue = _issue_from_graphql(self._graphql(query, owner, name, number, children.cursor), number, selected)
            node_id, _, repository_id = _issue_identity(issue, selected)
            current = (node_id, repository_id, _string(issue, "title"), _string(issue, "body"))
            if snapshot is not None and current != snapshot:
                raise GitHubReadError("github_observation_pending", "Parent changed during pagination")
            snapshot = current
            for child in children.read(_mapping(issue, "subIssues")):
                child_id, child_number, _ = _issue_identity(child, selected, repository_id)
                _parent_identity(child, node_id, number, selected, repository_id)
                if child_number == number or any(n == child_number for n, _ in tickets):
                    raise GitHubReadError("github_invalid_response", "Duplicate or recursive sub-Issue")
                tickets.append((child_number, child_id))
        assert snapshot is not None
        return _ParentFacts(ParentIssue(number, snapshot[2], snapshot[3], tuple(n for n, _ in tickets), True),
                            snapshot[0], snapshot[1], tuple(tickets))

    def _read_issue(self, owner: str, name: str, number: int, node_id: str, parent: _ParentFacts) -> Issue:
        query = """
        query($owner:String!,$name:String!,$number:Int!,$cursor:String,$labelsCursor:String) {
          repository(owner:$owner,name:$name) {
            id nameWithOwner
            issue(number:$number) {
              id number title body state repository { id nameWithOwner }
              parent { id number repository { id nameWithOwner } }
              labels(first:100,after:$labelsCursor) {
                totalCount nodes { id name } pageInfo { hasNextPage endCursor }
              }
              blockedBy(first:100,after:$cursor) {
                totalCount nodes { id number state } pageInfo { hasNextPage endCursor }
              }
            }
          }
        }
        """
        selected = f"{owner}/{name}"
        labels, blockers = _SourceConnection("labels"), _SourceConnection("blockedBy")
        label_names: set[str] = set()
        blocked_by: list[Blocker] = []
        ticket_ids = dict(parent.tickets)
        snapshot = None
        while not labels.done or not blockers.done:
            data = self._graphql(query, owner, name, number, None if blockers.done else blockers.cursor,
                                 labels_cursor=None if labels.done else labels.cursor)
            issue = _issue_from_graphql(data, number, selected)
            identity, _, _ = _issue_identity(issue, selected, parent.repository_id)
            _parent_identity(issue, parent.node_id, parent.value.number, selected, parent.repository_id)
            if identity != node_id:
                raise GitHubReadError("github_invalid_response", "Ticket no longer matches its original sub-Issue")
            current = (_string(issue, "title"), _string(issue, "body"), _issue_state(issue))
            if snapshot is not None and current != snapshot:
                raise GitHubReadError("github_observation_pending", "Ticket changed during pagination")
            snapshot = current
            if labels.done:
                labels.recheck(_mapping(issue, "labels"))
            else:
                for label in labels.read(_mapping(issue, "labels")):
                    label_name = _string(label, "name")
                    if label_name in label_names:
                        raise GitHubReadError("github_observation_pending", "Duplicate source label")
                    label_names.add(label_name)
            if blockers.done:
                blockers.recheck(_mapping(issue, "blockedBy"))
            else:
                for blocker in blockers.read(_mapping(issue, "blockedBy")):
                    blocker_number = _integer(blocker, "number")
                    if blocker_number < 1 or any(value.number == blocker_number for value in blocked_by):
                        raise GitHubReadError("github_invalid_response", "Duplicate or invalid blocker number")
                    if blocker_number in ticket_ids and _nonempty(blocker, "id") != ticket_ids[blocker_number]:
                        raise GitHubReadError("github_invalid_response", "Blocker number belongs to a different global Issue")
                    blocked_by.append(Blocker(blocker_number, _issue_state(blocker)))
        assert snapshot is not None
        return Issue(number, snapshot[0], snapshot[1], snapshot[2], frozenset(label_names), tuple(blocked_by))

    def _graphql(
        self,
        query: str,
        owner: str,
        name: str,
        number: int,
        cursor: str | None,
        *, labels_cursor: str | None = None,
    ) -> dict[str, Any]:
        arguments = [
            "api",
            "graphql",
            "-f",
            f"query={query}",
            "-F",
            f"owner={owner}",
            "-F",
            f"name={name}",
            "-F",
            f"number={number}",
        ]
        if cursor is not None:
            arguments.extend(["-F", f"cursor={cursor}"])
        if labels_cursor is not None:
            arguments.extend(["-F", f"labelsCursor={labels_cursor}"])
        return self._gh_json(*arguments)

    def _gh_json(self, *arguments: str) -> dict[str, Any]:
        result = run_read_command(
            ["gh", *arguments], **runner_options(self.runner_identity),
        )
        if result.returncode != 0:
            raise GitHubReadError(
                "github_read_failed",
                result.stderr.strip() or "gh command failed",
            )
        try:
            loaded: object = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise GitHubReadError(
                "github_invalid_response", f"gh returned invalid JSON: {error}"
            ) from error
        if not isinstance(loaded, dict):
            raise GitHubReadError(
                "github_invalid_response", "gh response root must be an object"
            )
        if arguments[:2] == ("api", "graphql") and "errors" in loaded and loaded["errors"] != []:
            raise GitHubReadError("github_read_failed", "GraphQL returned incomplete or erroneous data")
        return loaded


def _repository_hint_from_origin(working_directory: Path | None) -> str | None:
    """Read a local GitHub origin without turning a network failure into one."""

    result = subprocess.run(
        ["git", "config", "--get", "remote.origin.url"],
        cwd=working_directory,
        text=True,
        capture_output=True,
        check=False,
        env=git_environment(),
    )
    if result.returncode != 0:
        return None
    remote = result.stdout.strip()
    if remote.startswith("git@github.com:"):
        path = remote.removeprefix("git@github.com:")
    else:
        parsed = urlparse(remote)
        if parsed.hostname != "github.com":
            return None
        path = parsed.path.lstrip("/")
    if path.endswith(".git"):
        path = path[:-4]
    owner, separator, repository = path.partition("/")
    if not separator or not owner or not repository or "/" in repository:
        return None
    return f"{owner}/{repository}"


@dataclass(frozen=True)
class _ParentFacts:
    value: ParentIssue
    node_id: str
    repository_id: str
    tickets: tuple[tuple[int, str], ...]


class _SourceConnection:
    """One source connection's completeness, never state shared across reads."""
    def __init__(self, field: str) -> None:
        self.field = field
        self.cursor: str | None = None
        self.done = False
        self.count: int | None = None
        self.nodes: set[str] = set()
        self.cursors: set[str] = set()
        self.first: tuple[list[dict[str, Any]], int, bool] | None = None

    def recheck(self, connection: dict[str, Any]) -> None:
        values, count, more, _ = self._page(connection)
        if (values, count, more) != self.first:
            raise GitHubReadError("github_observation_pending", f"{self.field} changed during source pagination")

    def _page(self, connection: dict[str, Any]) -> tuple[list[dict[str, Any]], int, bool, str | None]:
        count = _integer(connection, "totalCount")
        raw = connection.get("nodes")
        page = _mapping(connection, "pageInfo")
        more, cursor = page.get("hasNextPage"), page.get("endCursor")
        if (count < 0 or not isinstance(raw, list) or type(more) is not bool or "endCursor" not in page
                or cursor is not None and not isinstance(cursor, str)):
            raise GitHubReadError("github_invalid_response", f"Invalid {self.field} pagination")
        if more and not cursor:
            raise GitHubReadError("github_invalid_response", f"Missing {self.field} pagination cursor")
        return [_as_mapping(node) for node in raw], count, more, cursor

    def read(self, connection: dict[str, Any]) -> list[dict[str, Any]]:
        values, count, more, cursor = self._page(connection)
        if self.first is None:
            self.first = values, count, more
        if self.count is not None and self.count != count:
            raise GitHubReadError("github_observation_pending", f"{self.field} count changed during pagination")
        self.count = count
        for node in values:
            identity = _nonempty(node, "id")
            if identity in self.nodes:
                raise GitHubReadError("github_observation_pending", f"Duplicate {self.field} node")
            self.nodes.add(identity)
        if len(self.nodes) > count or (not more and len(self.nodes) != count) or (more and (not values or len(self.nodes) >= count)):
            raise GitHubReadError("github_invalid_response", f"Incomplete {self.field} collection")
        if more:
            if not cursor or cursor in self.cursors:
                raise GitHubReadError("github_invalid_response", f"Invalid {self.field} pagination cursor")
            self.cursors.add(cursor)
            self.cursor = cursor
        self.done = not more
        return values


def _issue_from_graphql(data: dict[str, Any], number: int, selected: str) -> dict[str, Any]:
    repository = _mapping(_mapping(data, "data"), "repository")
    if _string(repository, "nameWithOwner") != selected:
        raise GitHubReadError("github_invalid_response", "Source repository identity changed")
    issue = repository.get("issue")
    if not isinstance(issue, dict):
        raise GitHubReadError("missing_ticket", f"GitHub did not return issue #{number}")
    if _issue_identity(issue, selected, _nonempty(repository, "id"))[1] != number:
        raise GitHubReadError("github_invalid_response", "Source Issue number changed")
    return issue


def _issue_identity(issue: dict[str, Any], selected: str, repository_id: str | None = None) -> tuple[str, int, str]:
    repository = _mapping(issue, "repository")
    number, actual_id = _integer(issue, "number"), _nonempty(repository, "id")
    if number < 1 or _string(repository, "nameWithOwner") != selected or repository_id is not None and actual_id != repository_id:
        raise GitHubReadError("github_invalid_response", "Foreign source Issue identity")
    return _nonempty(issue, "id"), number, actual_id


def _parent_identity(issue: dict[str, Any], parent_id: str, number: int, selected: str, repository_id: str) -> None:
    actual_id, actual_number, _ = _issue_identity(_mapping(issue, "parent"), selected, repository_id)
    if (actual_id, actual_number) != (parent_id, number):
        raise GitHubReadError("github_observation_pending", "Source Issue parent changed")


def _issue_state(issue: dict[str, Any]) -> str:
    value = _string(issue, "state")
    if value not in {"OPEN", "CLOSED"}:
        raise GitHubReadError("github_invalid_response", "Invalid source Issue state")
    return value


def _nonempty(data: dict[str, Any], key: str) -> str:
    value = _string(data, key)
    if not value:
        raise GitHubReadError("github_invalid_response", f"{key} must not be empty")
    return value


def _mapping(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise GitHubReadError("github_invalid_response", f"{key} must be an object")
    return value


def _as_mapping(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise GitHubReadError("github_invalid_response", "node must be an object")
    return value


def _string(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        raise GitHubReadError("github_invalid_response", f"{key} must be a string")
    return value


def _integer(data: dict[str, Any], key: str) -> int:
    value = data.get(key)
    if type(value) is not int:
        raise GitHubReadError("github_invalid_response", f"{key} must be an integer")
    return value
