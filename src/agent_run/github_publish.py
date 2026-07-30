from __future__ import annotations

import json
import subprocess
from typing import Any

from agent_run.git import GitError, GitRepository
from agent_run.github import GhGitHubReader, GitHubReadError
from agent_run.revisions import effective_revision_from_graph


class MergeOutcomeUnknownError(RuntimeError):
    pass


_ACCEPTANCE_MARKER = "<!-- agent-run:acceptance-record -->"


class GhGitHubPublisher:
    """GitHub/Git mutation adapter used only by the trusted Publisher process."""

    def __init__(self, repository: str, git: GitRepository) -> None:
        self.repository = repository
        self.git = git

    def ensure_ticket_branch(
        self,
        *,
        ticket_number: int,
        branch: str,
        base_branch: str,
    ) -> None:
        self._ensure_remote_run_branch(base_branch)
        listed = self._run(
            "issue",
            "develop",
            "--list",
            str(ticket_number),
            "--repo",
            self.repository,
        )
        if listed.returncode == 0 and branch in listed.stdout:
            return
        created = self._run(
            "issue",
            "develop",
            str(ticket_number),
            "--repo",
            self.repository,
            "--name",
            branch,
            "--base",
            base_branch,
        )
        if created.returncode != 0:
            raise GitHubReadError(
                "github_write_failed",
                created.stderr.strip() or "could not create linked ticket branch",
            )

    def _ensure_remote_run_branch(self, branch: str) -> None:
        remote = subprocess.run(
            ["git", "ls-remote", "--heads", "origin", f"refs/heads/{branch}"],
            cwd=self.git.root,
            text=True,
            capture_output=True,
            check=False,
        )
        if remote.returncode != 0:
            raise GitError(remote.stderr.strip() or "could not read remote Run Branch")
        if remote.stdout.strip():
            return
        head = self.git.resolve(branch)
        pushed = subprocess.run(
            ["git", "push", "origin", f"{head}:refs/heads/{branch}"],
            cwd=self.git.root,
            text=True,
            capture_output=True,
            check=False,
        )
        if pushed.returncode != 0:
            raise GitError(pushed.stderr.strip() or "could not publish Run Branch")

    def publish_branch(
        self,
        branch: str,
        head_sha: str,
        *,
        expected_remote_sha: str,
    ) -> None:
        remote = subprocess.run(
            ["git", "ls-remote", "--heads", "origin", f"refs/heads/{branch}"],
            cwd=self.git.root,
            text=True,
            capture_output=True,
            check=False,
        )
        if remote.returncode != 0:
            raise GitError(remote.stderr.strip() or "could not read ticket branch")
        current_remote_sha = (
            remote.stdout.split()[0] if remote.stdout.strip() else None
        )
        if current_remote_sha == head_sha:
            return
        if current_remote_sha != expected_remote_sha:
            raise GitError("remote ticket branch drifted")
        arguments = [
            "git",
            "push",
            "origin",
            f"{head_sha}:refs/heads/{branch}",
            f"--force-with-lease=refs/heads/{branch}:{expected_remote_sha}",
        ]
        pushed = subprocess.run(
            arguments,
            cwd=self.git.root,
            text=True,
            capture_output=True,
            check=False,
        )
        if pushed.returncode != 0:
            raise GitError(pushed.stderr.strip() or "could not publish ticket branch")

    def ensure_ticket_pr(
        self,
        *,
        branch: str,
        base_branch: str,
        title: str,
        body: str,
        primary_ticket: int,
    ) -> int:
        pulls = self._json(
            "pr",
            "list",
            "--repo",
            self.repository,
            "--state",
            "open",
            "--head",
            branch,
            "--base",
            base_branch,
            "--json",
            "number",
        )
        if not isinstance(pulls, list):
            raise GitHubReadError("github_invalid_response", "PR list must be an array")
        if len(pulls) > 1:
            raise GitHubReadError(
                "ambiguous_ticket_pr", "more than one open Ticket PR exists"
            )
        if pulls:
            number = _integer(_mapping(pulls[0]), "number")
            self._require(
                "pr",
                "edit",
                str(number),
                "--repo",
                self.repository,
                "--title",
                title,
                "--body",
                body,
            )
            return number
        self._require(
            "pr",
            "create",
            "--repo",
            self.repository,
            "--head",
            branch,
            "--base",
            base_branch,
            "--title",
            title,
            "--body",
            body,
        )
        created = self._json(
            "pr",
            "view",
            branch,
            "--repo",
            self.repository,
            "--json",
            "number",
        )
        return _integer(_mapping(created), "number")

    def required_checks(self, pr_number: int) -> str:
        checks = self._json(
            "pr",
            "checks",
            str(pr_number),
            "--repo",
            self.repository,
            "--required",
            "--json",
            "bucket",
            allowed_exit_codes={0, 1, 8},
        )
        if not isinstance(checks, list):
            raise GitHubReadError("github_invalid_response", "checks must be an array")
        buckets = {
            str(_mapping(check).get("bucket", "")).lower() for check in checks
        }
        if not buckets:
            return "none"
        if buckets & {"fail", "cancel"}:
            return "fail"
        if "pending" in buckets:
            return "pending"
        return "pass"

    def required_check_evidence(self, pr_number: int) -> dict[str, Any]:
        checks = self._json(
            "pr",
            "checks",
            str(pr_number),
            "--repo",
            self.repository,
            "--required",
            "--json",
            "bucket,name,link,workflow,description",
            allowed_exit_codes={0, 1, 8},
        )
        if not isinstance(checks, list):
            raise GitHubReadError(
                "github_invalid_response", "checks must be an array"
            )
        failed = [
            dict(_mapping(check))
            for check in checks
            if str(_mapping(check).get("bucket", "")).lower()
            in {"fail", "cancel"}
        ]
        return {"pr_number": pr_number, "checks": failed}

    def live_pull_request(self, pr_number: int) -> dict[str, Any]:
        value = self._json(
            "pr",
            "view",
            str(pr_number),
            "--repo",
            self.repository,
            "--json",
            "headRefOid,baseRefName,baseRefOid,mergeable,state,mergeCommit",
        )
        data = _mapping(value)
        merge_commit = data.get("mergeCommit")
        integrated_sha = (
            merge_commit.get("oid")
            if isinstance(merge_commit, dict)
            else None
        )
        result = {
            "head_sha": data.get("headRefOid"),
            "base_branch": data.get("baseRefName"),
            "base_sha": data.get("baseRefOid"),
            "mergeable": (
                data.get("mergeable") == "MERGEABLE" and data.get("state") == "OPEN"
            ),
            "state": data.get("state"),
            "integrated_sha": integrated_sha,
        }
        head_sha = data.get("headRefOid")
        if isinstance(integrated_sha, str) and isinstance(head_sha, str):
            integrated = self._commit_metadata(integrated_sha)
            head = self._commit_metadata(head_sha)
            result.update(
                {
                    "head_tree": head["tree"],
                    "integrated_tree": integrated["tree"],
                    "integrated_message": integrated["message"],
                    "integrated_parents": integrated["parents"],
                }
            )
        return result

    def record_acceptance(
        self, pr_number: int, record: dict[str, Any]
    ) -> None:
        body = (
            f"{_ACCEPTANCE_MARKER}\n"
            "## Fresh Acceptance Record\n\n"
            "```json\n"
            f"{json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True)}\n"
            "```"
        )
        comments = self._json(
            "api",
            f"repos/{self.repository}/issues/{pr_number}/comments",
            "--paginate",
        )
        if not isinstance(comments, list):
            raise GitHubReadError("github_invalid_response", "comments must be an array")
        existing = next(
            (
                _mapping(comment)
                for comment in comments
                if _ACCEPTANCE_MARKER in str(_mapping(comment).get("body", ""))
            ),
            None,
        )
        if existing is None:
            self._json(
                "api",
                f"repos/{self.repository}/issues/{pr_number}/comments",
                "-f",
                f"body={body}",
            )
        else:
            comment_id = _integer(existing, "id")
            self._json(
                "api",
                "--method",
                "PATCH",
                f"repos/{self.repository}/issues/comments/{comment_id}",
                "-f",
                f"body={body}",
            )

    def squash_merge(
        self,
        *,
        pr_number: int,
        expected_head_sha: str,
        run_branch: str,
        commit_message: str,
    ) -> str:
        del run_branch
        merged = self._run(
            "pr",
            "merge",
            str(pr_number),
            "--repo",
            self.repository,
            "--squash",
            "--match-head-commit",
            expected_head_sha,
            "--subject",
            commit_message,
        )
        try:
            live = self.live_pull_request(pr_number)
        except GitHubReadError as error:
            raise MergeOutcomeUnknownError(
                "could not determine squash merge outcome"
            ) from error
        integrated = live.get("integrated_sha")
        if live.get("state") == "MERGED" and isinstance(integrated, str):
            return integrated
        raise MergeOutcomeUnknownError(
            merged.stderr.strip() or "could not determine squash merge outcome"
        )

    def sync_run_branch(
        self, *, run_branch: str, integrated_sha: str
    ) -> None:
        fetched = subprocess.run(
            ["git", "fetch", "--no-tags", "origin", run_branch],
            cwd=self.git.root,
            text=True,
            capture_output=True,
            check=False,
        )
        if fetched.returncode != 0:
            raise GitError(fetched.stderr.strip() or "could not fetch merged Run Branch")
        fetched_sha = self.git.resolve("FETCH_HEAD")
        if fetched_sha != integrated_sha:
            raise GitError("remote Run Branch does not match integrated commit")
        subprocess.run(
            ["git", "update-ref", f"refs/heads/{run_branch}", integrated_sha],
            cwd=self.git.root,
            check=True,
        )

    def close_primary_ticket(
        self,
        *,
        ticket_number: int,
        run_id: str,
        pr_number: int,
        integrated_sha: str,
    ) -> None:
        issue = _mapping(
            self._json(
                "issue",
                "view",
                str(ticket_number),
                "--repo",
                self.repository,
                "--json",
                "state,comments",
            )
        )
        marker = f"<!-- agent-run:{run_id}:ticket-{ticket_number}:completed -->"
        comments = issue.get("comments")
        already_recorded = isinstance(comments, list) and any(
            marker in str(_mapping(comment).get("body", "")) for comment in comments
        )
        if not already_recorded:
            body = (
                f"{marker}\nDelivery Run `{run_id}` completed this ticket in "
                f"PR #{pr_number}; Run Branch commit `{integrated_sha}`. "
                "This change has not yet entered the default branch."
            )
            self._require(
                "issue",
                "comment",
                str(ticket_number),
                "--repo",
                self.repository,
                "--body",
                body,
            )
        if issue.get("state") != "CLOSED":
            self._require(
                "issue",
                "close",
                str(ticket_number),
                "--repo",
                self.repository,
            )

    def mark_ready_for_human(self, ticket_number: int) -> None:
        self._require(
            "issue",
            "edit",
            str(ticket_number),
            "--repo",
            self.repository,
            "--add-label",
            "ready-for-human",
            "--remove-label",
            "ready-for-agent",
        )

    def current_effective_revision(
        self,
        *,
        parent_number: int,
        ticket_number: int,
        expected_revision: str,
    ) -> str:
        graph = GhGitHubReader(self.repository).delivery_graph(parent_number)
        return effective_revision_from_graph(graph, ticket_number)

    def _commit_metadata(self, sha: str) -> dict[str, Any]:
        data = _mapping(
            self._json(
                "api",
                f"repos/{self.repository}/git/commits/{sha}",
            )
        )
        tree = _mapping(data.get("tree"))
        tree_sha = tree.get("sha")
        message = data.get("message")
        raw_parents = data.get("parents")
        parents = (
            [
                _mapping(parent).get("sha")
                for parent in raw_parents
                if isinstance(parent, dict)
            ]
            if isinstance(raw_parents, list)
            else None
        )
        if (
            not isinstance(tree_sha, str)
            or not isinstance(message, str)
            or parents is None
            or not all(isinstance(parent, str) for parent in parents)
        ):
            raise GitHubReadError(
                "github_invalid_response", "Git commit metadata is invalid"
            )
        return {
            "tree": tree_sha,
            "message": message.splitlines()[0],
            "parents": parents,
        }

    def _require(self, *arguments: str) -> None:
        result = self._run(*arguments)
        if result.returncode != 0:
            raise GitHubReadError(
                "github_write_failed",
                result.stderr.strip() or "gh mutation failed",
            )

    def _json(
        self,
        *arguments: str,
        allowed_exit_codes: set[int] | None = None,
    ) -> object:
        result = self._run(*arguments)
        allowed = allowed_exit_codes or {0}
        if result.returncode not in allowed:
            raise GitHubReadError(
                "github_write_failed",
                result.stderr.strip() or "gh command failed",
            )
        try:
            return json.loads(result.stdout or "null")
        except json.JSONDecodeError as error:
            raise GitHubReadError(
                "github_invalid_response", f"gh returned invalid JSON: {error}"
            ) from error

    def _run(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["gh", *arguments],
            cwd=self.git.root,
            text=True,
            capture_output=True,
            check=False,
        )


def _mapping(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise GitHubReadError("github_invalid_response", "expected an object")
    return value


def _integer(data: dict[str, Any], key: str) -> int:
    value = data.get(key)
    if not isinstance(value, int):
        raise GitHubReadError("github_invalid_response", f"{key} must be an integer")
    return value
