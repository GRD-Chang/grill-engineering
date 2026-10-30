from __future__ import annotations

import json
import random
import time
import subprocess
from pathlib import Path
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from time import monotonic
from typing import Any
from urllib.parse import quote, urlparse

from agent_run.adapters.auth.runner_transport import runner_options
from agent_run.adapters.github_commands import run_read_command
from agent_run.adapters import github_commands
from agent_run.contracts.repository import normalize_repository, same_repository
from agent_run.runtime.git_command import git_environment
from agent_run.model import Requirements, Repository, wire_json
from agent_run.errors import GitHubReadError
from agent_run.model import Blocker, Issue, ParentIssue


_ISSUE_FIELDS = """
  id number title body state repository { id nameWithOwner }
  parent { id number repository { id nameWithOwner } }
  labels(first:100,after:$labelsCursor) {
    totalCount nodes { id name } pageInfo { hasNextPage endCursor }
  }
  blockedBy(first:100,after:$cursor) {
    totalCount nodes { id number state } pageInfo { hasNextPage endCursor }
  }
"""
_SOURCE_LIMIT = 4 * 1024 * 1024


class GhGitHubReader:
    def __init__(
        self,
        repository_override: str | None = None,
        *,
        working_directory: Path | None = None,
        runner_identity: Mapping[str, Any] | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> None:
        self.cancelled = cancelled
        self.runner_identity = runner_identity
        self.repository_override = repository_override
        self.working_directory = working_directory

    def repository_hint(self) -> str | None:
        identity = self.repository_override or _repository_hint_from_origin(
            self.working_directory
        )
        return identity.lower() if identity else None

    def _repository_metadata(self, fields: str) -> dict[str, Any]:
        arguments = ["repo", "view"]
        if self.repository_override:
            arguments.append(self.repository_override)
        arguments.extend(["--json", fields])
        data = self._gh_json(*arguments)
        name_with_owner = _string(data, "nameWithOwner")
        if not same_repository(name_with_owner, self.repository_override or name_with_owner):
            raise GitHubReadError("github_invalid_response", "Source repository does not match the selected repository")
        return data

    def repository(self) -> Repository:
        data = self._repository_metadata("nameWithOwner,defaultBranchRef")
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
        return Repository(
            name_with_owner=name_with_owner,
            default_branch=default_branch,
            default_head_sha=_string(_mapping(commit, "object"), "sha"),
        )

    def requirements(self, parent_number: int) -> Requirements:
        return _SourceObservation(self).read(parent_number)

    def _read_parent(self, owner: str, name: str, number: int, observation: _SourceObservation
                     ) -> tuple[_ParentFacts, dict[int, dict[str, Any]]]:
        fields = ("id number repository { id nameWithOwner } "
                  "parent { id number repository { id nameWithOwner } }" if observation.compact else _ISSUE_FIELDS.replace(",after:$labelsCursor", "").replace(",after:$cursor", ""))
        width = 100 if observation.compact else 16
        query = """
        query SourceSnapshot($owner:String!,$name:String!,$number:Int!,$cursor:String) {
          repository(owner:$owner,name:$name) {
            id nameWithOwner
            issue(number:$number) {
              id number title body repository { id nameWithOwner }
              subIssues(first:WIDTH,after:$cursor) {
                totalCount nodes { FIELDS } pageInfo { hasNextPage endCursor }
              }
            }
          }
        }
        """.replace("WIDTH", str(width)).replace("FIELDS", fields)
        selected: str | None = None
        first_pages: dict[int, dict[str, Any]] = {}
        children = _SourceConnection("subIssues")
        snapshot = None
        tickets: list[tuple[int, str]] = []
        minimum_size = 0
        while not children.done:
            data = observation.graphql(query, owner, name, number, children.cursor)
            repository = _mapping(_mapping(data, "data"), "repository")
            canonical = _string(repository, "nameWithOwner")
            if (not same_repository(canonical, self.repository_override or canonical)
                    or selected is not None and canonical != selected):
                raise GitHubReadError("github_invalid_response", "Source repository identity changed")
            selected = canonical
            owner, name = canonical.split("/", 1)
            issue = _issue_from_graphql(data, number, selected)
            node_id, _, repository_id = _issue_identity(issue, selected)
            current = (node_id, repository_id, _string(issue, "title"), _string(issue, "body"))
            if snapshot is not None and current != snapshot:
                raise GitHubReadError("github_observation_pending", "Parent changed during pagination")
            if snapshot is None:
                minimum_size = len(wire_json({"parent": asdict(ParentIssue(number, current[2], current[3], (), True)),
                                              "issues": []}))
            snapshot = current
            for child in children.read(_mapping(issue, "subIssues")):
                child_id, child_number, _ = _issue_identity(child, selected, repository_id)
                _parent_identity(child, node_id, number, selected, repository_id)
                if child_number == number or any(n == child_number for n, _ in tickets):
                    raise GitHubReadError("github_invalid_response", "Duplicate or recursive sub-Issue")
                tickets.append((child_number, child_id))
                # Account only facts that every valid completed Source must
                # retain. Repeated wire envelopes/Parent bodies are not charged.
                minimum_size += (len(wire_json(child_number)) + 2 * int(len(tickets) > 1)
                                 + _minimum_issue_size({} if observation.compact else child, child_number))
                if minimum_size > _SOURCE_LIMIT:
                    raise GitHubReadError("github_invalid_response", "Source observation exceeds its complete material limit")
                if not observation.compact:
                    first_pages[child_number] = {"data": {"repository": {
                        "id": repository_id, "nameWithOwner": selected, "issue": child}}}
        assert snapshot is not None
        return _ParentFacts(ParentIssue(number, snapshot[2], snapshot[3], tuple(n for n, _ in tickets), True),
                            snapshot[0], snapshot[1], tuple(tickets), selected or ""), first_pages

    def _read_issue(self, owner: str, name: str, number: int, node_id: str, parent: _ParentFacts,
                    observation: _SourceObservation, first_page: dict[str, Any] | None = None) -> Issue:
        query = """
        query SourceTicket($owner:String!,$name:String!,$number:Int!,$cursor:String,$labelsCursor:String) {
          repository(owner:$owner,name:$name) {
            id nameWithOwner
            issue(number:$number) { FIELDS }
          }
        }
        """.replace("FIELDS", _ISSUE_FIELDS)
        selected = f"{owner}/{name}"
        labels, blockers = _SourceConnection("labels"), _SourceConnection("blockedBy")
        label_names: set[str] = set()
        blocked_by: list[Blocker] = []
        ticket_ids = dict(parent.tickets)
        snapshot = None
        while not labels.done or not blockers.done:
            data = first_page if first_page is not None else observation.graphql(
                query, owner, name, number, None if blockers.done else blockers.cursor,
                labels_cursor=None if labels.done else labels.cursor)
            first_page = None
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

    def _gh_json(self, *arguments: str) -> dict[str, Any]:
        result = run_read_command(
            ["gh", *arguments], complete_stdout=True, **runner_options(self.runner_identity),
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


def _minimum_issue_size(value: dict[str, Any], number: int) -> int:
    """Resource lower bound only; _read_issue remains the Issue validator.

    Complete valid output must retain these body/name/state facts and every
    first-page label/blocker. Invalid shapes supply no extra estimated facts;
    they still fail the original validator before any Source can be returned.
    """
    labels = value.get("labels")
    label_nodes = labels.get("nodes") if isinstance(labels, dict) else None
    names = {row["name"] for row in label_nodes if isinstance(row, dict) and isinstance(row.get("name"), str)} if isinstance(label_nodes, list) else set()
    blocked = value.get("blockedBy")
    blocked_nodes = blocked.get("nodes") if isinstance(blocked, dict) else None
    blockers = [{"number": row["number"], "state": row["state"]} for row in blocked_nodes
                if isinstance(row, dict) and type(row.get("number")) is int
                and isinstance(row.get("state"), str) and row.get("state") in {"OPEN", "CLOSED"}] if isinstance(blocked_nodes, list) else []
    return len(wire_json({"number": number,
        "title": value.get("title") if isinstance(value.get("title"), str) else "",
        "body": value.get("body") if isinstance(value.get("body"), str) else "",
        "state": "CLOSED" if value.get("state") == "CLOSED" else "OPEN",
        "labels": sorted(names), "blocked_by": blockers}))


class _SourceObservation:
    """One fresh Source, with no facts or permission reused across calls.

    Three complete Source attempts share one absolute 90-second budget. An
    optional Delivery projection joins only the first physical request; that
    request also consumes one of Delivery's own three basis attempts. Failed
    Source attempts discard all attached Delivery facts. Explicit errors on
    the separate Delivery root may accompany independently complete Source.
    Failed or unscoped partial packages supply no facts. Only explicit output overflow switches
    once to compact Parent + individual Tickets; that consumes an attempt.
    GraphQL errors (including mixed/null data) are incomplete and retryable.
    Complete malformed data is contradictory. Core retains its bounded wait
    across calls; a later complete contradiction is never treated as success.

    Successful pages do not consume retry attempts. With T Tickets, and Li/Bi
    label/blocker totals, a complete attempt needs at most max(1,T) Parent
    requests + sum(max(1,Li+Bi)) Ticket requests (the nested first pages only
    reduce this bound). Each unfinished connection must add a unique node;
    changing totals, duplicate nodes and repeated cursors fail. Failed attempts
    add at most their failed request and never renew the absolute deadline.
    """
    def __init__(self, reader: GhGitHubReader, *,
                 delivery_projection: tuple[list[str], list[str], list[str], list[str]] | None = None) -> None:
        self.reader = reader
        self.delivery_projection = delivery_projection
        self.delivery_packet: dict[str, Any] | None = None
        self.attempts = 0
        self.combined_attempted = False
        self._first_packet = True
        self.deadline = monotonic() + github_commands.MAX_READ_ATTEMPTS * github_commands.READ_TIMEOUT_SECONDS
        self.compact = False

    def command(self, arguments: list[str], *, cwd: Path,
                runner_identity: Mapping[str, Any], complete_stdout: bool) -> subprocess.CompletedProcess[str]:
        """One exact-ref command under this observation's original budget."""
        if (cwd != self.reader.working_directory or runner_identity != self.reader.runner_identity
                or not complete_stdout):
            raise ValueError("Ref command belongs to another observation")
        try:
            result = github_commands._run_bounded_command(arguments, cwd=self.reader.working_directory,
                timeout=min(github_commands.READ_TIMEOUT_SECONDS, self._remaining()), complete_stdout=True,
                **runner_options(self.reader.runner_identity), before_start=self._before_start, cancelled=self._cancelled)
        except subprocess.TimeoutExpired as error:
            raise GitHubReadError('github_read_failed', 'Observation command deadline expired') from error
        self._remaining()
        return result

    def _remaining(self) -> float:
        if self.reader.cancelled is not None and self.reader.cancelled():
            raise InterruptedError("Source observation cancelled")
        remaining = self.deadline - monotonic()
        if remaining <= 0:
            raise GitHubReadError("github_read_failed", "Source observation deadline expired")
        return remaining

    def _before_start(self) -> None:
        self._remaining()

    def _cancelled(self) -> bool:
        self._remaining()
        return False

    def read(self, parent_number: int) -> Requirements:
        selected = self.reader.repository_override
        if selected is None:
            # Let this bounded gh command resolve its own repository context.
            # A local origin URL is never treated as a frozen authority.
            owner, name = "{owner}", "{repo}"
        else:
            try:
                owner, name = normalize_repository(selected).split("/", 1)
            except ValueError as error:
                raise GitHubReadError("github_invalid_response", "Source repository identity is invalid") from error
        for attempt in range(github_commands.MAX_READ_ATTEMPTS):
            self.attempts = attempt + 1
            self.delivery_packet = None
            self._first_packet = True
            self._remaining()
            try:
                parent, pages = self.reader._read_parent(owner, name, parent_number, self)
                canonical_owner, canonical_name = parent.repository.split("/", 1)
                issues: list[Issue] = []
                size = len(wire_json({"parent": asdict(parent.value), "issues": []}))
                for number, node_id in parent.tickets:
                    issues.append(self.reader._read_issue(canonical_owner, canonical_name, number, node_id,
                                                         parent, self, pages.pop(number, None)))
                    issue = issues[-1]
                    size += len(wire_json({**asdict(issue), "labels": sorted(issue.labels)})) + int(len(issues) > 1)
                    if size > _SOURCE_LIMIT:
                        raise GitHubReadError("github_invalid_response", "Source observation exceeds its complete material limit")
                self._remaining()
                return Requirements(parent.value, tuple(issues))
            except github_commands.ReadOutputOverflow as error:
                if self.compact:
                    raise
                if attempt == github_commands.MAX_READ_ATTEMPTS - 1:
                    raise GitHubReadError("github_read_failed",
                        "Source observation budget ended before its compact projection could be read") from error
                # A joint overflow says nothing about either individual material.
                if not (attempt == 0 and self.combined_attempted):
                    self.compact = True
                continue
            except (UnicodeError, RecursionError) as error:
                raise GitHubReadError("github_invalid_response", "Source material is not valid complete text") from error
            except GitHubReadError as error:
                if error.code != "github_read_failed" or attempt == github_commands.MAX_READ_ATTEMPTS - 1:
                    raise
                time.sleep(min(2 ** attempt + random.uniform(0, 0.25),
                                               self._remaining()))
        raise AssertionError("Source observation exhausted without a result")

    def graphql(self, query: str, owner: str, name: str, number: int, cursor: str | None,
                *, labels_cursor: str | None = None) -> dict[str, Any]:
        combined = (self.delivery_projection is not None and self.attempts == 1 and self._first_packet
                    and not self.compact and "query SourceSnapshot(" in query)
        self._first_packet = False
        extra_arguments: list[str] = []
        if combined:
            self.combined_attempted = True
            assert self.delivery_projection is not None
            declarations, fields, roots, extra_arguments = self.delivery_projection
            extra = [value for value in declarations if not value.startswith(("$owner:", "$name:"))]
            query = query.replace("query SourceSnapshot(", "query SourceSnapshotDeliveryBasis(", 1)
            query = query.replace(") {", ("," + ",".join(extra) if extra else "") + ") {", 1)
            end = query.rfind("}")
            query = (query[:end] + " ".join(roots)
                     + " delivery:repository(owner:$owner,name:$name) { " + " ".join(fields) + " } " + query[end:])
        arguments = ["gh", "api", "graphql", "-f", "query=" + query,
                     "-F" if owner == "{owner}" else "-f", "owner=" + owner,
                     "-F" if name == "{repo}" else "-f", "name=" + name, "-F", f"number={number}"]
        arguments += [value for index in range(0, len(extra_arguments), 2)
                      if not extra_arguments[index + 1].startswith(("owner=", "name="))
                      for value in extra_arguments[index:index + 2]]
        if cursor is not None:
            arguments += ["-f", "cursor=" + cursor]
        if labels_cursor is not None:
            arguments += ["-f", "labelsCursor=" + labels_cursor]
        try:
            response = github_commands._run_bounded_command(arguments, cwd=self.reader.working_directory,
                timeout=min(github_commands.READ_TIMEOUT_SECONDS, self._remaining()), complete_stdout=True,
                **runner_options(self.reader.runner_identity),
                **({"complete_nonzero_stdout": True} if combined else {}),
                before_start=self._before_start, cancelled=self._cancelled)
        except subprocess.TimeoutExpired as error:
            raise GitHubReadError("github_read_failed", "Source command deadline expired") from error
        except github_commands.ReadOutputOverflow:
            raise
        except GitHubReadError as error:
            if combined and error.code == "github_invalid_response":
                raise GitHubReadError("github_read_failed", "Combined response cannot establish Source completeness") from error
            raise
        self._remaining()
        if response.returncode and not combined:
            raise github_commands.read_response_error(response)
        try:
            value = json.loads(response.stdout)
        except (ValueError, RecursionError) as error:
            raise GitHubReadError("github_read_failed" if combined else "github_invalid_response", "Source response is not complete JSON") from error
        if not isinstance(value, dict):
            raise GitHubReadError("github_read_failed" if combined else "github_invalid_response", "Source response root must be an object")
        errors = value.get("errors", [])
        if not isinstance(errors, list) or any(not isinstance(error, dict) or not isinstance(error.get("message"), str)
                                               for error in errors):
            raise GitHubReadError("github_read_failed" if combined else "github_invalid_response", "Source error envelope is malformed")
        delivery_roots = {"delivery"}
        if combined and "viewer { login }" in roots:
            delivery_roots.add("viewer")
        delivery_errors = (combined and bool(errors) and response.returncode in (0, 1) and all(
            isinstance(error.get("path"), list) and error["path"]
            and all(isinstance(part, str) or type(part) is int for part in error["path"])
            and error["path"][0] in delivery_roots for error in errors))
        if response.returncode and not delivery_errors:
            raise github_commands.read_response_error(response)
        if errors and not delivery_errors:
            raise GitHubReadError("github_read_failed", "Source response contains incomplete or erroneous data")
        data = value.get("data")
        if combined and (not isinstance(data, dict) or not isinstance(data.get("repository"), dict)):
            raise GitHubReadError("github_read_failed", "Combined response cannot establish Source completeness")
        if ("data" in value and data is None or isinstance(data, dict)
                and "repository" in data and data["repository"] is None):
            raise GitHubReadError("github_read_failed", "Source repository facts are unavailable")
        if combined:
            if delivery_errors:
                self.delivery_packet = {"errors": errors}
            else:
                self.delivery_packet = {"data": {
                    "repository": data.get("delivery") if isinstance(data, dict) else None,
                    "viewer": data.get("viewer") if isinstance(data, dict) else None}}
        return value


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
    repository: str


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
