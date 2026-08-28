from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from agent_run.github_retry import run_read_command
from agent_run.models import Blocker, DeliveryGraph, Issue, ParentIssue, Repository


class GitHubReadError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class MergeOutcomeUnknownError(RuntimeError):
    """A merge write may have succeeded, but GitHub has not converged yet."""


def _merge_identity_matches(
    live: dict[str, Any],
    *,
    expected_head_sha: str,
    expected_head_branch: str | None,
    expected_head_repository: str | None,
    expected_base_branch: str | None,
    expected_base_sha: str | None,
    expected_base_repository: str | None,
) -> bool:
    """Check the complete PR identity supplied to a merge authority."""

    expected = {
        "head_sha": expected_head_sha,
        "head_branch": expected_head_branch,
        "head_repository": expected_head_repository,
        "base_branch": expected_base_branch,
        "base_sha": expected_base_sha,
        "base_repository": expected_base_repository,
    }
    return all(
        value is None or live.get(key) == value
        for key, value in expected.items()
    )


class GhGitHubReader:
    def __init__(
        self,
        repository_override: str | None = None,
        *,
        working_directory: Path | None = None,
    ) -> None:
        self.repository_override = repository_override
        self.working_directory = working_directory
        self._repository: Repository | None = None

    def repository_hint(self) -> str | None:
        return self.repository_override or _repository_hint_from_origin(
            self.working_directory
        )

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
            f"repos/{name_with_owner}/commits/{default_branch}",
        )
        self._repository = Repository(
            name_with_owner=name_with_owner,
            default_branch=default_branch,
            default_head_sha=_string(commit, "sha"),
        )
        return self._repository

    def delivery_graph(self, parent_number: int) -> DeliveryGraph:
        repository = self.repository()
        owner, name = repository.name_with_owner.split("/", 1)
        parent, ordered_numbers = self._read_parent(owner, name, parent_number)
        issues = {
            number: self._read_issue(owner, name, number) for number in ordered_numbers
        }
        return DeliveryGraph(parent=parent, issues=issues)

    def live_pull_request(self, pr_number: int) -> dict[str, Any]:
        repository = self.repository()
        data = self._gh_json(
            "pr", "view", str(pr_number), "--repo", repository.name_with_owner,
            "--json", "state,headRefOid,baseRefName,baseRefOid,mergeCommit",
        )
        merge_commit = data.get("mergeCommit")
        integrated_sha = (
            merge_commit.get("oid")
            if isinstance(merge_commit, dict)
            else None
        )
        result: dict[str, Any] = {
            "state": _string(data, "state"),
            "head_sha": _string(data, "headRefOid"),
            "base_branch": _string(data, "baseRefName"),
            "base_sha": _string(data, "baseRefOid"),
            "integrated_sha": integrated_sha,
        }
        head_sha = result["head_sha"]
        if isinstance(integrated_sha, str) and isinstance(head_sha, str):
            integrated = self._commit_metadata(repository.name_with_owner, integrated_sha)
            head = self._commit_metadata(repository.name_with_owner, head_sha)
            result.update(
                {
                    "head_tree": head["tree"],
                    "integrated_tree": integrated["tree"],
                    "integrated_parents": integrated["parents"],
                }
            )
        return result

    def _commit_metadata(self, repository: str, sha: str) -> dict[str, Any]:
        data = self._gh_json("api", f"repos/{repository}/git/commits/{sha}")
        tree = _mapping(data, "tree")
        parents = data.get("parents")
        if not isinstance(parents, list):
            raise GitHubReadError("github_invalid_response", "commit parents must be an array")
        return {
            "tree": _string(tree, "sha"),
            "parents": [_string(_as_mapping(parent), "sha") for parent in parents],
        }

    def _read_parent(
        self, owner: str, name: str, parent_number: int
    ) -> tuple[ParentIssue, tuple[int, ...]]:
        query = """
        query($owner:String!,$name:String!,$number:Int!,$cursor:String) {
          repository(owner:$owner,name:$name) {
            issue(number:$number) {
              number title body
              subIssues(first:100,after:$cursor) {
                nodes { number }
                pageInfo { hasNextPage endCursor }
              }
            }
          }
        }
        """
        cursor: str | None = None
        numbers: list[int] = []
        title = ""
        body = ""
        while True:
            data = self._graphql(query, owner, name, parent_number, cursor)
            issue_data = _issue_from_graphql(data, parent_number)
            title = _string(issue_data, "title")
            body = _string(issue_data, "body")
            connection = _mapping(issue_data, "subIssues")
            nodes = connection.get("nodes")
            if not isinstance(nodes, list):
                raise GitHubReadError(
                    "github_invalid_response", "subIssues.nodes is missing"
                )
            numbers.extend(_integer(_as_mapping(node), "number") for node in nodes)
            page = _mapping(connection, "pageInfo")
            if not bool(page.get("hasNextPage")):
                break
            cursor_value = page.get("endCursor")
            if not isinstance(cursor_value, str):
                raise GitHubReadError(
                    "github_invalid_response", "subIssues pagination cursor is missing"
                )
            cursor = cursor_value
        parent = ParentIssue(
            number=parent_number,
            title=title,
            body=body,
            sub_issue_numbers=tuple(numbers),
            sub_issue_order_reliable=True,
        )
        return parent, tuple(numbers)

    def _read_issue(self, owner: str, name: str, number: int) -> Issue:
        query = """
        query($owner:String!,$name:String!,$number:Int!,$cursor:String) {
          repository(owner:$owner,name:$name) {
            issue(number:$number) {
              number title body state
              labels(first:100) { nodes { name } }
              blockedBy(first:100,after:$cursor) {
                nodes { number state }
                pageInfo { hasNextPage endCursor }
              }
            }
          }
        }
        """
        cursor: str | None = None
        blockers: list[Blocker] = []
        issue_data: dict[str, Any] | None = None
        while True:
            data = self._graphql(query, owner, name, number, cursor)
            issue_data = _issue_from_graphql(data, number)
            blocked_by = _mapping(issue_data, "blockedBy")
            nodes = blocked_by.get("nodes")
            if not isinstance(nodes, list):
                raise GitHubReadError(
                    "github_invalid_response", "blockedBy.nodes is missing"
                )
            blockers.extend(
                Blocker(
                    number=_integer(_as_mapping(node), "number"),
                    state=_string(_as_mapping(node), "state"),
                )
                for node in nodes
            )
            page = _mapping(blocked_by, "pageInfo")
            if not bool(page.get("hasNextPage")):
                break
            cursor_value = page.get("endCursor")
            if not isinstance(cursor_value, str):
                raise GitHubReadError(
                    "github_invalid_response", "blockedBy pagination cursor is missing"
                )
            cursor = cursor_value
        if issue_data is None:
            raise GitHubReadError("missing_ticket", f"GitHub did not return issue #{number}")
        labels_connection = _mapping(issue_data, "labels")
        label_nodes = labels_connection.get("nodes")
        if not isinstance(label_nodes, list):
            raise GitHubReadError(
                "github_invalid_response", "labels.nodes is missing"
            )
        return Issue(
            number=_integer(issue_data, "number"),
            title=_string(issue_data, "title"),
            body=_string(issue_data, "body"),
            state=_string(issue_data, "state"),
            labels=frozenset(
                _string(_as_mapping(label), "name") for label in label_nodes
            ),
            blocked_by=tuple(blockers),
        )

    def _graphql(
        self,
        query: str,
        owner: str,
        name: str,
        number: int,
        cursor: str | None,
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
        return self._gh_json(*arguments)

    @staticmethod
    def _gh_json(*arguments: str) -> dict[str, Any]:
        result = run_read_command(["gh", *arguments])
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
        return loaded


def _repository_hint_from_origin(working_directory: Path | None) -> str | None:
    """Read a local GitHub origin without turning a network failure into one."""

    result = subprocess.run(
        ["git", "config", "--get", "remote.origin.url"],
        cwd=working_directory,
        text=True,
        capture_output=True,
        check=False,
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


def _issue_from_graphql(data: dict[str, Any], number: int) -> dict[str, Any]:
    repository = _mapping(_mapping(data, "data"), "repository")
    issue = repository.get("issue")
    if not isinstance(issue, dict):
        raise GitHubReadError("missing_ticket", f"GitHub did not return issue #{number}")
    return issue


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
    if not isinstance(value, int):
        raise GitHubReadError("github_invalid_response", f"{key} must be an integer")
    return value
