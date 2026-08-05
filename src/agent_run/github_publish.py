from __future__ import annotations

import json
import subprocess
from typing import Any

from agent_run.git import GitError, GitRepository, is_managed_delivery_branch
from agent_run.github import GhGitHubReader, GitHubReadError
from agent_run.revisions import effective_revision_from_graph


class MergeOutcomeUnknownError(RuntimeError):
    pass


_ACCEPTANCE_MARKER = "<!-- agent-run:acceptance-record -->"
_RUN_PUBLICATION_MARKER = "<!-- agent-run:run-publication-record -->"
_AGENT_RUN_STATUS_MARKER = "<!-- agent-run:agent-run-status -->"


class GhGitHubPublisher:
    """GitHub/Git mutation adapter used only by the trusted Publisher process."""

    def __init__(self, repository: str, git: GitRepository) -> None:
        self.repository = repository
        self.git = git

    def ensure_parent_branch(
        self, *, parent_number: int, branch: str, base_branch: str
    ) -> None:
        listed = self._run(
            "issue", "develop", "--list", str(parent_number), "--repo", self.repository
        )
        if listed.returncode == 0 and branch in listed.stdout:
            return
        created = self._run(
            "issue",
            "develop",
            str(parent_number),
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
                created.stderr.strip() or "could not create linked Parent branch",
            )

    def delete_managed_branch(self, branch: str) -> None:
        if not is_managed_delivery_branch(branch):
            raise GitError(f"refusing to delete unmanaged branch {branch!r}")
        remote = subprocess.run(
            ["git", "ls-remote", "--heads", "origin", f"refs/heads/{branch}"],
            cwd=self.git.root,
            text=True,
            capture_output=True,
            check=False,
        )
        if remote.returncode != 0:
            raise GitError(remote.stderr.strip() or "could not inspect remote branch")
        if not remote.stdout.strip():
            return
        deleted = subprocess.run(
            ["git", "push", "origin", "--delete", branch],
            cwd=self.git.root,
            text=True,
            capture_output=True,
            check=False,
        )
        if deleted.returncode != 0:
            raise GitError(deleted.stderr.strip() or "could not delete remote branch")

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

    def ensure_run_repair_branch(self, *, branch: str, base_branch: str) -> None:
        self._ensure_remote_run_branch(base_branch)
        remote = subprocess.run(
            ["git", "ls-remote", "--heads", "origin", f"refs/heads/{branch}"],
            cwd=self.git.root,
            text=True,
            capture_output=True,
            check=False,
        )
        if remote.returncode != 0:
            raise GitError(remote.stderr.strip() or "could not read Run Repair branch")
        if remote.stdout.strip():
            return
        base_sha = self.git.resolve(base_branch)
        pushed = subprocess.run(
            ["git", "push", "origin", f"{base_sha}:refs/heads/{branch}"],
            cwd=self.git.root,
            text=True,
            capture_output=True,
            check=False,
        )
        if pushed.returncode != 0:
            raise GitError(pushed.stderr.strip() or "could not create Run Repair branch")

    def ensure_run_repair_pr(
        self, *, branch: str, base_branch: str, title: str, body: str
    ) -> int:
        pulls = self._json(
            "pr", "list", "--repo", self.repository, "--state", "open",
            "--head", branch, "--base", base_branch, "--json", "number"
        )
        if not isinstance(pulls, list):
            raise GitHubReadError("github_invalid_response", "PR list must be an array")
        if len(pulls) > 1:
            raise GitHubReadError("ambiguous_run_repair_pr", "more than one open Run Repair PR exists")
        if pulls:
            number = _integer(_mapping(pulls[0]), "number")
            self._require("pr", "edit", str(number), "--repo", self.repository, "--title", title, "--body", body)
            return number
        self._require("pr", "create", "--repo", self.repository, "--head", branch, "--base", base_branch, "--title", title, "--body", body)
        created = self._json("pr", "view", branch, "--repo", self.repository, "--json", "number")
        return _integer(_mapping(created), "number")

    def ensure_run_pr(
        self, *, branch: str, base_branch: str, title: str, body: str
    ) -> int:
        self._ensure_remote_run_branch(branch)
        pulls = self._json(
            "pr", "list", "--repo", self.repository, "--state", "all",
            "--head", branch, "--base", base_branch, "--json", "number,state"
        )
        if not isinstance(pulls, list):
            raise GitHubReadError("github_invalid_response", "PR list must be an array")
        if len(pulls) > 1:
            raise GitHubReadError("ambiguous_run_pr", "more than one open final Run PR exists")
        if pulls:
            existing = _mapping(pulls[0])
            number = _integer(existing, "number")
            if existing.get("state") != "OPEN":
                raise GitHubReadError(
                    "final_run_pr_not_open",
                    "the existing final Run PR is not open",
                )
            self._require("pr", "edit", str(number), "--repo", self.repository, "--title", title, "--body", body)
            return number
        self._require("pr", "create", "--repo", self.repository, "--head", branch, "--base", base_branch, "--title", title, "--body", body)
        created = self._json("pr", "view", branch, "--repo", self.repository, "--json", "number")
        return _integer(_mapping(created), "number")

    def ensure_parent_pr(
        self, *, branch: str, base_branch: str, title: str, body: str
    ) -> int:
        self._ensure_remote_run_branch(branch)
        pulls = self._json(
            "pr", "list", "--repo", self.repository, "--state", "all",
            "--head", branch, "--base", base_branch, "--json", "number,state"
        )
        if not isinstance(pulls, list):
            raise GitHubReadError("github_invalid_response", "PR list must be an array")
        if len(pulls) > 1:
            raise GitHubReadError("ambiguous_parent_pr", "more than one Parent PR exists")
        if pulls:
            existing = _mapping(pulls[0])
            number = _integer(existing, "number")
            if existing.get("state") != "OPEN":
                raise GitHubReadError("parent_pr_not_open", "the existing Parent PR is not open")
            self._require("pr", "edit", str(number), "--repo", self.repository, "--title", title, "--body", body)
            return number
        self._require("pr", "create", "--repo", self.repository, "--head", branch, "--base", base_branch, "--title", title, "--body", body)
        created = self._json("pr", "view", branch, "--repo", self.repository, "--json", "number")
        return _integer(_mapping(created), "number")

    def abandon_parent_pr(self, pr_number: int) -> None:
        live = self.live_pull_request(pr_number)
        if live.get("state") == "OPEN":
            self._require("pr", "close", str(pr_number), "--repo", self.repository)

    def record_run_publication(
        self, pr_number: int, record: dict[str, Any]
    ) -> None:
        body = (
            f"{_RUN_PUBLICATION_MARKER}\n"
            "## Run Publication Record\n\n```json\n"
            f"{json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True)}\n```"
        )
        comments = self._json(
            "api", f"repos/{self.repository}/issues/{pr_number}/comments", "--paginate"
        )
        if not isinstance(comments, list):
            raise GitHubReadError("github_invalid_response", "comments must be an array")
        existing = next(
            (
                _mapping(comment)
                for comment in comments
                if _RUN_PUBLICATION_MARKER in str(_mapping(comment).get("body", ""))
            ),
            None,
        )
        if existing is None:
            self._json("api", f"repos/{self.repository}/issues/{pr_number}/comments", "-f", f"body={body}")
        else:
            self._json(
                "api", "--method", "PATCH",
                f"repos/{self.repository}/issues/comments/{_integer(existing, 'id')}",
                "-f", f"body={body}",
            )

    def normal_merge(self, *, pr_number: int, expected_head_sha: str) -> str:
        merged = self._run(
            "pr", "merge", str(pr_number), "--repo", self.repository,
            "--merge", "--match-head-commit", expected_head_sha,
        )
        try:
            live = self.live_pull_request(pr_number)
        except GitHubReadError as error:
            raise MergeOutcomeUnknownError("could not determine final merge outcome") from error
        integrated = live.get("integrated_sha")
        if live.get("state") == "MERGED" and isinstance(integrated, str):
            return integrated
        raise MergeOutcomeUnknownError(
            merged.stderr.strip() or "could not determine final merge outcome"
        )

    def close_parent_issue(
        self,
        *,
        parent_number: int,
        run_id: str,
        pr_number: int,
        integrated_sha: str,
    ) -> None:
        issue = _mapping(
            self._json(
                "issue",
                "view",
                str(parent_number),
                "--repo",
                self.repository,
                "--json",
                "state,comments",
            )
        )
        marker = f"<!-- agent-run:{run_id}:parent-completed -->"
        comments = issue.get("comments")
        already_recorded = isinstance(comments, list) and any(
            marker in str(_mapping(comment).get("body", "")) for comment in comments
        )
        if not already_recorded:
            body = (
                f"{marker}\nDelivery Run `{run_id}` completed this Parent Issue "
                f"in Final Run PR #{pr_number}; merge commit `{integrated_sha}` "
                "is verified on the default branch."
            )
            self._require(
                "issue",
                "comment",
                str(parent_number),
                "--repo",
                self.repository,
                "--body",
                body,
            )
        if issue.get("state") != "CLOSED":
            self._require(
                "issue",
                "close",
                str(parent_number),
                "--repo",
            )

    def abandon_run_pr(self, pr_number: int) -> None:
        live = self.live_pull_request(pr_number)
        if live.get("state") == "OPEN":
            self._require("pr", "close", str(pr_number), "--repo", self.repository)

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

    def publication_context(self, pr_number: int) -> dict[str, object]:
        value = self._json(
            "pr",
            "view",
            str(pr_number),
            "--repo",
            self.repository,
            "--json",
            "number,url,title",
        )
        data = _mapping(value)
        return {
            "number": _integer(data, "number"),
            "url": _string(data, "url"),
            "title": _string(data, "title"),
        }

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

    def record_agent_run_status(
        self, pr_number: int, status: dict[str, Any]
    ) -> None:
        body = _render_agent_run_status(status)
        comments = self._json(
            "api", f"repos/{self.repository}/issues/{pr_number}/comments", "--paginate"
        )
        if not isinstance(comments, list):
            raise GitHubReadError("github_invalid_response", "comments must be an array")
        existing = next(
            (
                _mapping(comment)
                for comment in comments
                if _AGENT_RUN_STATUS_MARKER in str(_mapping(comment).get("body", ""))
            ),
            None,
        )
        if existing is None:
            self._json(
                "api", f"repos/{self.repository}/issues/{pr_number}/comments", "-f", f"body={body}"
            )
        else:
            self._json(
                "api", "--method", "PATCH",
                f"repos/{self.repository}/issues/comments/{_integer(existing, 'id')}",
                "-f", f"body={body}",
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


def _render_agent_run_status(status: dict[str, Any]) -> str:
    lanes = _mapping(status.get("lane_statuses"))
    lane_summary = ", ".join(
        f"{lane}={lanes.get(lane)}" for lane in ("e2e", "standards", "spec")
    )
    required = _string(status, "required_checks")
    return (
        f"{_AGENT_RUN_STATUS_MARKER}\n"
        "## Agent Run Status\n\n"
        f"- Scope: `{_string(status, 'scope')}`\n"
        f"- Candidate/Base: `{_string(status, 'candidate_sha')}` / "
        f"`{_string(status, 'base_sha')}`\n"
        f"- Fresh Validation: `{_string(status, 'validation_verdict')}` "
        f"({lane_summary})\n"
        f"- Required Checks: `{required}`\n"
        f"- Next action: {_string(status, 'next_action')}"
    )


def _string(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise GitHubReadError("github_invalid_response", f"{key} must be a string")
    return value
