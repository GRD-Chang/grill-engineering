"""Read-only GitHub transport and exact immutable-object conversion.

No Effect intent, reconciliation or remote write capability is accepted here.
"""
from __future__ import annotations
from collections.abc import Callable, Iterable, Mapping
import json
from pathlib import Path
import re
import subprocess
from typing import Any, Protocol
from urllib.parse import urlencode
from agent_run.adapters.github_commands import read_response_error, run_read_command
from agent_run.adapters.git_objects import LocalGit
from agent_run.contracts.identity import validate_identity_snapshot
from agent_run.errors import GitHubReadError
from agent_run.model import Commit, PullRequest
from agent_run.storage.sqlite import MATERIAL_LIMIT

class ReadAPI(Protocol):
    def object(self, endpoint: str) -> dict[str, Any]: ...
    def list(self, endpoint: str) -> list[dict[str, Any]]: ...

class GitReadAPI(Protocol):
    def refs(self) -> dict[str, str]: ...
    def fetch(self, sha: str) -> None: ...

class GitHubReadTransport:
    def __init__(self, root: Path, repository: str, *, runner_identity: Mapping[str, Any],
                 remote: str = "origin", api: ReadAPI | None = None,
                 git_reads: GitReadAPI | None = None, cancelled: Callable[[], bool] | None = None) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise ValueError("Expected an exact GitHub owner/repository")
        if not remote or remote.startswith("-") or "\0" in remote:
            raise ValueError("Expected a remote name or URL")
        self.root, self.repository, self.remote = root, repository, remote
        self.identity = validate_identity_snapshot(runner_identity)
        self.git = LocalGit(root)
        self.api, self.git_reads, self.cancelled = api, git_reads, cancelled

    def actor(self, before_attempt: Callable[[], None] | None = None) -> str:
        """Resolve the selected Runner's actual login, not a fixture identity."""
        if self.identity["source"] == "app":
            from agent_run.adapters.auth.app import resolve_app_login
            from agent_run.config.github_app import GitHubAppProfile
            profile = self.identity["app_profile"]
            return resolve_app_login(GitHubAppProfile(profile["app_id"], profile["installation_id"],
                                                     Path(profile["private_key_path"])))
        return _text((self.object("user") if before_attempt is None else self.object("user", before_attempt))["login"])


    def pull_request(self, number: int) -> PullRequest:
        """Original PR identity and actual commit trees, for reads and receipts."""
        return self._pr(self.object(f"repos/{self.repository}/pulls/{number}"))


    def find_pull_request(self, source_ref: str, target_ref: str) -> PullRequest | None:
        data = self._find_pr(source_ref, target_ref, None)
        return None if data is None else self._pr(data)


    def _prepare_ancestor_objects(self, shas: Iterable[str]) -> None:
        # Transport only absent immutable objects; refs and FETCH_HEAD stay intact.
        missing = self.git.missing_commits(shas)
        for sha in missing:
            if self.git_reads is None:
                self.read(["git", "fetch", "--no-tags", "--no-write-fetch-head", "--refmap=", "--", self.remote, sha])
            else:
                self.git_reads.fetch(sha)
        if missing and self.git.missing_commits(missing):
            raise GitHubReadError("git_ancestry_unknown", "Fetched commit object is unavailable")


    def _ancestor(self, before: str, after: str) -> bool:
        if before == after:
            return True
        self._prepare_ancestor_objects((before, after))
        return self.git.is_ancestor(before, after)


    def commit(self, sha: str) -> tuple[Commit, tuple[str, ...], str | None]:
        data = self.object(f"repos/{self.repository}/git/commits/{_sha(sha)}")
        if data["sha"] != sha:
            raise ValueError("foreign commit response")
        message = data.get("message")
        return (Commit(sha, _sha(data["tree"]["sha"])), tuple(_sha(p["sha"]) for p in data["parents"]),
                message if isinstance(message, str) else None)


    def _pr(self, data: Mapping[str, Any], *,
            read_commit: Callable[[str], Commit] | None = None,
            merged_parents: Mapping[str, Commit] | None = None) -> PullRequest:
        commit = (lambda sha: self.commit(sha)[0]) if read_commit is None else read_commit
        head, base = data["head"], data["base"]
        repository = _text(base["repo"]["full_name"])
        head_repository = None if head.get("repo") is None else _text(head["repo"]["full_name"])
        merged = None
        base_sha = _sha(base["sha"])
        if data.get("merged"):
            # Parent order is separate receipt evidence; keep its original fresh read.
            merged, parents, _ = self.commit(_sha(data["merge_commit_sha"]))
            if not parents:
                raise ValueError("merged commit has no original base parent")
            # A merged receipt describes the original Git event, not a later
            # live base-branch pointer. Its actual first parent is that base.
            base_commit = merged_parents.get(parents[0]) if merged_parents is not None else None
            if base_commit is None:
                base_commit = commit(parents[0])
        else:
            base_commit = commit(base_sha)
        return PullRequest(_number(data["number"]), repository, "refs/heads/" + _text(head["ref"]),
            "refs/heads/" + _text(base["ref"]), commit(_sha(head["sha"])), base_commit,
            _text(data["title"]), _string(data["body"]), "MERGED" if merged else _text(data["state"]).upper(),
            _login(data.get("user")), merged, head_repository)


    def _find_pr(self, source_ref: str, target_ref: str, number: int | None) -> dict[str, Any] | None:
        if number is not None:
            return self.object(f"repos/{self.repository}/pulls/{number}")
        prs = self.list(self._pr_discovery_endpoint(source_ref, target_ref))
        if len(prs) > 1:
            raise ValueError("ambiguous original PR")
        return prs[0] if prs else None


    def _pr_discovery_endpoint(self, source_ref: str, target_ref: str) -> str:
        query = urlencode({"state": "all", "head": self.repository.split('/')[0] + ':' +
                           source_ref.removeprefix("refs/heads/"), "base": target_ref.removeprefix("refs/heads/"),
                           "per_page": 100})
        return f"repos/{self.repository}/pulls?{query}"


    def read(self, command: list[str], before_attempt: Callable[[], None] | None = None) -> subprocess.CompletedProcess[str]:
        # API envelopes are structured; fetch output remains diagnostic.
        complete_stdout = command[:2] == ["gh", "api"]
        response = (run_read_command(command, cwd=self.root, runner_identity=self.identity, complete_stdout=complete_stdout) if before_attempt is None and self.cancelled is None else
                    run_read_command(command, cwd=self.root, runner_identity=self.identity, complete_stdout=complete_stdout, before_attempt=before_attempt,
                                     **({"cancelled": self.cancelled} if self.cancelled is not None else {})))
        if response.returncode:
            raise GitHubReadError("remote_read_failed", read_response_error(response).message)
        return response


    def object(self, endpoint: str, before_attempt: Callable[[], None] | None = None) -> dict[str, Any]:
        if before_attempt is not None:
            before_attempt()
        data = self.api.object(endpoint) if self.api is not None else json.loads((
            self.read(["gh", "api", endpoint]) if before_attempt is None else self.read(["gh", "api", endpoint], before_attempt)).stdout)
        if not isinstance(data, dict):
            raise ValueError("expected remote object")
        return data


    def list(self, endpoint: str, before_attempt: Callable[[], None] | None = None) -> list[dict[str, Any]]:
        if before_attempt is not None:
            before_attempt()
        if self.api is not None:
            rows = self.api.list(endpoint)
            if not all(isinstance(row, dict) for row in rows) or len(json.dumps(rows).encode()) > MATERIAL_LIMIT:
                raise ValueError("invalid or oversized remote collection")
            return rows
        command = ["gh", "api", endpoint, "--paginate", "--slurp"]
        pages = json.loads((self.read(command) if before_attempt is None else self.read(command, before_attempt)).stdout)
        if not isinstance(pages, list) or any(not isinstance(page, list) for page in pages):
            raise ValueError("expected paginated remote list")
        rows = [row for page in pages for row in page]
        if not all(isinstance(row, dict) for row in rows) or len(json.dumps(rows).encode()) > MATERIAL_LIMIT:
            raise ValueError("invalid or oversized remote collection")
        return rows


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


def _sha(value: Any) -> str:
    result = _text(value)
    if not re.fullmatch(r"(?:[a-f0-9]{40}|[a-f0-9]{64})", result):
        raise ValueError("invalid commit identity")
    return result
