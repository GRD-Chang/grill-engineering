from __future__ import annotations

import json
import re
import subprocess
from typing import Any

from agent_run.git import GitError, GitRepository, is_managed_delivery_branch
from agent_run.github import GhGitHubReader, GitHubReadError
from agent_run.github_retry import run_read_command, run_write_command
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
            "issue", "develop", "--list", str(parent_number), "--repo", self.repository,
        )
        if listed.returncode != 0:
            raise GitHubReadError(
                "github_read_failed",
                listed.stderr.strip() or "could not inspect linked Parent branch",
            )
        if branch in listed.stdout:
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
        remote = run_read_command(
            ["git", "ls-remote", "--heads", "origin", f"refs/heads/{branch}"],
            cwd=self.git.root,
        )
        if remote.returncode != 0:
            raise GitError(remote.stderr.strip() or "could not inspect remote branch")
        if not remote.stdout.strip():
            return
        deleted = run_write_command(
            ["git", "push", "origin", "--delete", branch],
            cwd=self.git.root,
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
        if listed.returncode != 0:
            raise GitHubReadError(
                "github_read_failed",
                listed.stderr.strip() or "could not inspect linked ticket branch",
            )
        if branch in listed.stdout:
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
        remote = run_read_command(
            ["git", "ls-remote", "--heads", "origin", f"refs/heads/{branch}"],
            cwd=self.git.root,
        )
        if remote.returncode != 0:
            raise GitError(remote.stderr.strip() or "could not read Run Repair branch")
        if remote.stdout.strip():
            return
        base_sha = self.git.resolve(base_branch)
        pushed = run_write_command(
            ["git", "push", "origin", f"{base_sha}:refs/heads/{branch}"],
            cwd=self.git.root,
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
        delivery_type: str,
    ) -> None:
        issue = _mapping(
            self._json(
                "issue",
                "view",
                str(parent_number),
                "--repo",
                self.repository,
                "--json",
                "state,comments,updatedAt",
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
                f"in {delivery_type} PR #{pr_number}; merge commit `{integrated_sha}` "
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
                self.repository,
            )

    def abandon_run_pr(self, pr_number: int) -> None:
        live = self.live_pull_request(pr_number)
        if live.get("state") == "OPEN":
            self._require("pr", "close", str(pr_number), "--repo", self.repository)

    def abandon_change_pr(self, pr_number: int) -> None:
        live = self.live_pull_request(pr_number)
        if live.get("state") == "OPEN":
            self._require("pr", "close", str(pr_number), "--repo", self.repository)

    def _ensure_remote_run_branch(self, branch: str) -> None:
        remote = run_read_command(
            ["git", "ls-remote", "--heads", "origin", f"refs/heads/{branch}"],
            cwd=self.git.root,
        )
        if remote.returncode != 0:
            raise GitError(remote.stderr.strip() or "could not read remote Run Branch")
        if remote.stdout.strip():
            return
        head = self.git.resolve(branch)
        pushed = run_write_command(
            ["git", "push", "origin", f"{head}:refs/heads/{branch}"],
            cwd=self.git.root,
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
        remote = run_read_command(
            ["git", "ls-remote", "--heads", "origin", f"refs/heads/{branch}"],
            cwd=self.git.root,
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
        pushed = run_write_command(arguments, cwd=self.git.root)
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
        checks = self._checks(pr_number, "bucket")
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
        checks = self._checks(pr_number, "bucket,name,link,workflow,description")
        failed = [
            dict(_mapping(check))
            for check in checks
            if str(_mapping(check).get("bucket", "")).lower()
            in {"fail", "cancel"}
        ]
        return {"pr_number": pr_number, "checks": failed}

    def _checks(self, pr_number: int, fields: str) -> list[object]:
        arguments = (
            "pr",
            "checks",
            str(pr_number),
            "--repo",
            self.repository,
            "--required",
            "--json",
            fields,
        )
        result = self._run(*arguments)
        if (
            result.returncode == 1
            and "no required checks reported" in result.stderr.lower()
        ):
            return self._ruleset_checks(pr_number, fields)
        if result.returncode not in {0, 1, 8}:
            raise GitHubReadError(
                "github_write_failed", result.stderr.strip() or "gh command failed"
            )
        try:
            checks = json.loads(result.stdout or "null")
        except json.JSONDecodeError as error:
            raise GitHubReadError(
                "github_invalid_response", f"gh returned invalid JSON: {error}"
            ) from error
        if not isinstance(checks, list):
            raise GitHubReadError("github_invalid_response", "checks must be an array")
        return checks

    def _ruleset_checks(self, pr_number: int, fields: str) -> list[object]:
        live = self.live_pull_request(pr_number)
        base_branch = _string(live, "base_branch")
        required_contexts = self._ruleset_required_contexts(base_branch)
        if not required_contexts:
            return []
        integration_bound = [
            context
            for context, integration_id in required_contexts.items()
            if integration_id is not None
        ]
        if integration_bound:
            raise GitHubReadError(
                "github_unsupported_ruleset",
                "Ruleset required checks with integration_id are not safely observable",
            )
        requested_fields = tuple(field for field in fields.split(",") if field)
        check_fields = ",".join(dict.fromkeys((*requested_fields, "name")))
        checks = self._json(
            "pr",
            "checks",
            str(pr_number),
            "--repo",
            self.repository,
            "--json",
            check_fields,
            allowed_exit_codes={0, 1, 8},
        )
        if not isinstance(checks, list):
            raise GitHubReadError("github_invalid_response", "checks must be an array")
        matching = [
            check
            for check in checks
            if _string(_mapping(check), "name") in required_contexts
        ]
        observed = {_string(_mapping(check), "name") for check in matching}
        return matching + [
            {"name": context, "bucket": "pending"}
            for context in required_contexts
            if context not in observed
        ]

    def _ruleset_required_contexts(self, branch: str) -> dict[str, int | None]:
        pages = self._json(
            "api", f"repos/{self.repository}/rulesets", "--paginate", "--slurp"
        )
        if not isinstance(pages, list) or not all(
            isinstance(page, list) for page in pages
        ):
            raise GitHubReadError("github_invalid_response", "ruleset pages must be arrays")
        contexts: dict[str, int | None] = {}
        rulesets = [summary for page in pages for summary in page]
        for summary in rulesets:
            summary_data = _mapping(summary)
            if (
                summary_data.get("target") != "branch"
                or summary_data.get("enforcement") != "active"
            ):
                continue
            ruleset_id = _integer(summary_data, "id")
            ruleset = _mapping(
                self._json("api", f"repos/{self.repository}/rulesets/{ruleset_id}")
            )
            if not self._ruleset_applies_to_branch(ruleset, branch):
                continue
            rules = ruleset.get("rules")
            if not isinstance(rules, list):
                raise GitHubReadError("github_invalid_response", "ruleset rules must be an array")
            for rule in rules:
                rule_data = _mapping(rule)
                if rule_data.get("type") != "required_status_checks":
                    continue
                parameters = _mapping(rule_data.get("parameters"))
                checks = parameters.get("required_status_checks")
                if not isinstance(checks, list):
                    raise GitHubReadError(
                        "github_invalid_response",
                        "required_status_checks must be an array",
                    )
                for check in checks:
                    check_data = _mapping(check)
                    context = _string(check_data, "context")
                    integration_id = check_data.get("integration_id")
                    if integration_id is not None and not isinstance(integration_id, int):
                        raise GitHubReadError(
                            "github_invalid_response",
                            "Ruleset integration_id must be an integer",
                        )
                    existing = contexts.get(context)
                    if existing is not None and integration_id not in {None, existing}:
                        raise GitHubReadError(
                            "github_invalid_response",
                            "Ruleset context has conflicting integration_id values",
                        )
                    contexts[context] = (
                        integration_id if integration_id is not None else existing
                    )
        return contexts

    def _ruleset_applies_to_branch(self, ruleset: dict[str, Any], branch: str) -> bool:
        conditions = ruleset.get("conditions")
        if conditions is None:
            return True
        ref_name_value = _mapping(conditions).get("ref_name")
        if ref_name_value is None:
            return True
        ref_name = _mapping(ref_name_value)
        reference = f"refs/heads/{branch}"
        includes = ref_name.get("include", [])
        excludes = ref_name.get("exclude", [])
        if not isinstance(includes, list) or not isinstance(excludes, list):
            raise GitHubReadError(
                "github_invalid_response", "ruleset ref conditions must be arrays"
            )
        return (
            (not includes or any(_matches_ref(reference, pattern) for pattern in includes))
            and not any(_matches_ref(reference, pattern) for pattern in excludes)
        )

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
        fetched = run_read_command(
            ["git", "fetch", "--no-tags", "origin", run_branch],
            cwd=self.git.root,
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

    def prepare_primary_ticket_close(
        self,
        *,
        ticket_number: int,
        run_id: str,
        pr_number: int,
        integrated_sha: str,
    ) -> dict[str, Any] | None:
        issue = _mapping(
            self._json(
                "issue",
                "view",
                str(ticket_number),
                "--repo",
                self.repository,
                "--json",
                "state,comments,updatedAt",
            )
        )
        marker = f"<!-- agent-run:{run_id}:ticket-{ticket_number}:completed -->"
        intent_binding = f"pr-{pr_number}:sha-{integrated_sha}"
        close_intent = (
            f"<!-- agent-run:{run_id}:ticket-{ticket_number}:"
            f"publisher-close-intent:{intent_binding} -->"
        )
        comments = issue.get("comments")
        publisher_login = self._publisher_login()
        trusted_comments = (
            [
                _mapping(comment)
                for comment in comments
                if isinstance(_mapping(comment).get("author"), dict)
                and _mapping(comment)["author"].get("login") == publisher_login
            ]
            if isinstance(comments, list)
            else []
        )
        already_recorded = any(
            marker in str(_mapping(comment).get("body", ""))
            for comment in trusted_comments
        )
        has_close_intent = any(
            close_intent in str(_mapping(comment).get("body", ""))
            for comment in trusted_comments
        )
        baseline_event_id: int | None = None
        if issue.get("state") != "CLOSED" and not has_close_intent:
            baseline_event_id = self._ticket_transition_watermark(ticket_number)
        if not already_recorded or (
            issue.get("state") != "CLOSED" and not has_close_intent
        ):
            baseline_marker = (
                f"<!-- agent-run:{run_id}:ticket-{ticket_number}:"
                f"publisher-close-baseline:{intent_binding}:"
                f"{baseline_event_id} -->"
                if baseline_event_id is not None
                else ""
            )
            body = (
                f"{marker}\n"
                f"{close_intent if issue.get('state') != 'CLOSED' else ''}\n"
                f"{baseline_marker}\n"
                f"Delivery Run `{run_id}` completed this ticket in "
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
            intent_issue = _mapping(
                self._json(
                    "issue",
                    "view",
                    str(ticket_number),
                    "--repo",
                    self.repository,
                    "--json",
                    "state,comments,updatedAt",
                )
            )
            intent = self._publisher_close_intent(
                intent_issue,
                close_intent,
                publisher_login,
                run_id=run_id,
                ticket_number=ticket_number,
                intent_binding=intent_binding,
            )
            if intent is None:
                raise GitHubReadError(
                    "ticket_close_intent_pending",
                    "GitHub has not exposed the Publisher close intent yet",
                )
            if intent_issue.get("state") != "OPEN":
                raise GitHubReadError(
                    "ticket_close_reconciliation_pending",
                    "Ticket state changed after the Publisher close intent",
                )
            return {
                **intent,
                "event_id": None,
            }
        intent = self._publisher_close_intent(
            issue,
            close_intent,
            publisher_login,
            run_id=run_id,
            ticket_number=ticket_number,
            intent_binding=intent_binding,
        )
        if intent is None:
            return None
        ownership = self._ticket_close_ownership(
            ticket_number=ticket_number,
            run_id=run_id,
            recorded_ownership=intent,
        )
        return ownership

    def close_primary_ticket(
        self,
        *,
        ticket_number: int,
        run_id: str,
        pr_number: int,
        integrated_sha: str,
        close_intent: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        prepared = close_intent or self.prepare_primary_ticket_close(
            ticket_number=ticket_number,
            run_id=run_id,
            pr_number=pr_number,
            integrated_sha=integrated_sha,
        )
        if prepared is None:
            return prepared
        expected_binding = f"pr-{pr_number}:sha-{integrated_sha}"
        if prepared.get("intent_binding") != expected_binding:
            raise GitHubReadError(
                "ticket_close_reconciliation_pending",
                "prepared Ticket close belongs to a different PR generation",
            )
        if prepared.get("event_id") is not None:
            ownership = self._ticket_close_ownership(
                ticket_number=ticket_number,
                run_id=run_id,
                recorded_ownership=prepared,
            )
            if ownership is None:
                raise GitHubReadError(
                    "ticket_close_reconciliation_pending",
                    "recorded Ticket close is no longer current",
                )
            return ownership
        issue = _mapping(
            self._json(
                "issue",
                "view",
                str(ticket_number),
                "--repo",
                self.repository,
                "--json",
                "state,comments,updatedAt",
            )
        )
        if issue.get("state") == "CLOSED":
            ownership = self._ticket_close_ownership(
                ticket_number=ticket_number,
                run_id=run_id,
                recorded_ownership=prepared,
            )
            if ownership is None:
                raise GitHubReadError(
                    "ticket_close_reconciliation_pending",
                    "Ticket close ownership conflicts with the prepared dispatch",
                )
            return ownership
        if issue.get("state") != "OPEN":
            raise GitHubReadError(
                "ticket_close_reconciliation_pending", "Ticket state is unavailable"
            )
        baseline = prepared.get("baseline_event_id")
        if not isinstance(baseline, int) or isinstance(baseline, bool):
            raise ValueError("prepared Ticket close is missing its event watermark")
        transitions = self._ticket_transitions(ticket_number)
        if any(int(event["id"]) > baseline for event in transitions):
            self._ticket_close_ownership(
                ticket_number=ticket_number,
                run_id=run_id,
                recorded_ownership=prepared,
            )
            raise GitHubReadError(
                "ticket_close_reconciliation_pending",
                "Ticket changed after the Publisher close intent",
            )
        if issue.get("updatedAt") != prepared.get("intent_created_at"):
            raise GitHubReadError(
                "ticket_close_reconciliation_pending",
                "Ticket currentness changed after the Publisher close intent",
            )
        self._require(
            "issue", "close", str(ticket_number), "--repo", self.repository
        )
        ownership = self._ticket_close_ownership(
            ticket_number=ticket_number,
            run_id=run_id,
            recorded_ownership=prepared,
        )
        if ownership is None:
            raise GitHubReadError(
                "ticket_close_reconciliation_pending",
                "Ticket close ownership conflicts with the dispatched close",
            )
        return ownership

    def ticket_closed_by_run(
        self,
        *,
        ticket_number: int,
        run_id: str,
        recorded_ownership: dict[str, Any] | None,
    ) -> bool:
        return self.ticket_close_ownership(
            ticket_number=ticket_number,
            run_id=run_id,
            recorded_ownership=recorded_ownership,
        ) is not None

    def ticket_close_ownership(
        self,
        *,
        ticket_number: int,
        run_id: str,
        recorded_ownership: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        return self._ticket_close_ownership(
            ticket_number=ticket_number,
            run_id=run_id,
            recorded_ownership=recorded_ownership,
        )

    def _ticket_close_ownership(
        self,
        *,
        ticket_number: int,
        run_id: str,
        recorded_ownership: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        issue = _mapping(
            self._json(
                "issue",
                "view",
                str(ticket_number),
                "--repo",
                self.repository,
                "--json",
                "state,comments,updatedAt",
            )
        )
        transitions = self._ticket_transitions(ticket_number)
        if not transitions:
            if (
                isinstance(recorded_ownership, dict)
                and recorded_ownership.get("event_id") is None
                and issue.get("state") == "OPEN"
            ):
                return None
            raise GitHubReadError(
                "ticket_close_ownership_pending",
                "GitHub has not exposed the Ticket close event yet",
            )
        latest = transitions[-1]
        if isinstance(recorded_ownership, dict):
            recorded_event_id = recorded_ownership.get("event_id")
            if recorded_event_id is not None:
                recorded_events = [
                    event
                    for event in transitions
                    if str(event["id"]) == str(recorded_event_id)
                ]
                if not recorded_events:
                    raise GitHubReadError(
                        "ticket_close_ownership_pending",
                        "GitHub has not exposed the recorded Ticket close event yet",
                    )
                if str(latest["id"]) != str(recorded_event_id):
                    return None
                if issue.get("state") != "CLOSED":
                    raise GitHubReadError(
                        "ticket_close_ownership_pending",
                        "Ticket state and close timeline have not converged",
                    )
                if not self._ticket_current_at_transition(
                    issue,
                    latest,
                    publisher_login=str(recorded_ownership.get("actor", "")),
                    run_id=run_id,
                    ticket_number=ticket_number,
                ):
                    raise GitHubReadError(
                        "ticket_close_ownership_pending",
                        "Ticket updates are newer than the recorded close event",
                    )
                return dict(recorded_ownership)
            publisher_login = recorded_ownership.get("actor")
            intent_time = recorded_ownership.get("intent_created_at")
            baseline_event_id = recorded_ownership.get("baseline_event_id")
            intent_binding = recorded_ownership.get("intent_binding")
            if (
                not isinstance(publisher_login, str)
                or not isinstance(intent_time, str)
                or not isinstance(baseline_event_id, int)
                or isinstance(baseline_event_id, bool)
                or not isinstance(intent_binding, str)
            ):
                raise ValueError("provisional Ticket close ownership is incomplete")
        else:
            return None
        after_baseline = [
            event for event in transitions if int(event["id"]) > baseline_event_id
        ]
        if not after_baseline:
            if issue.get("state") == "OPEN":
                return None
            raise GitHubReadError(
                "ticket_close_ownership_pending",
                "GitHub has not exposed a transition after the close intent baseline yet",
            )
        owned = next(
            (
                event
                for event in after_baseline
                if event.get("event") == "closed"
                and isinstance(event.get("actor"), dict)
                and event["actor"].get("login") == publisher_login
            ),
            None,
        )
        latest_after_baseline = after_baseline[-1]
        if (
            issue.get("state") == "CLOSED"
            and latest_after_baseline.get("event") != "closed"
        ):
            raise GitHubReadError(
                "ticket_close_ownership_pending",
                "Ticket state and close timeline have not converged",
            )
        if issue.get("state") != "CLOSED":
            if latest_after_baseline.get("event") == "closed":
                raise GitHubReadError(
                    "ticket_close_ownership_pending",
                    "Ticket state and close timeline have not converged",
                )
            return None
        if (
            len(after_baseline) != 1
            or owned is None
            or latest_after_baseline["id"] != owned["id"]
        ):
            return None
        if not self._ticket_current_at_transition(
            issue,
            latest_after_baseline,
            publisher_login=publisher_login,
            run_id=run_id,
            ticket_number=ticket_number,
        ):
            raise GitHubReadError(
                "ticket_close_ownership_pending",
                "Ticket updates are newer than the Publisher close event",
            )
        return {
            "event_id": owned["id"],
            "actor": publisher_login,
            "created_at": owned["created_at"],
            "intent_created_at": intent_time,
            "baseline_event_id": baseline_event_id,
            "intent_binding": intent_binding,
        }

    @staticmethod
    def _ticket_current_at_transition(
        issue: dict[str, Any],
        transition: dict[str, Any],
        *,
        publisher_login: str,
        run_id: str,
        ticket_number: int,
        allow_recovery_marker: bool = False,
    ) -> bool:
        updated_at = issue.get("updatedAt")
        transition_time = transition.get("created_at")
        if not isinstance(updated_at, str) or not isinstance(transition_time, str):
            return False
        if updated_at == transition_time:
            return True
        if not allow_recovery_marker:
            return False
        marker = f"<!-- agent-run:{run_id}:ticket-{ticket_number}:abandoned -->"
        comments = issue.get("comments")
        if not isinstance(comments, list):
            return False
        return any(
            isinstance(_mapping(comment).get("createdAt"), str)
            and _mapping(comment).get("createdAt") == updated_at
            and isinstance(_mapping(comment).get("author"), dict)
            and _mapping(comment)["author"].get("login") == publisher_login
            and marker in str(_mapping(comment).get("body", ""))
            for comment in comments
        )

    def _ticket_transitions(self, ticket_number: int) -> list[dict[str, Any]]:
        raw_events = self._json(
            "api",
            f"repos/{self.repository}/issues/{ticket_number}/events",
            "--paginate",
            "--slurp",
        )
        if not isinstance(raw_events, list):
            raise GitHubReadError(
                "github_invalid_response", "Issue events must be an array"
            )
        events = (
            [event for page in raw_events for event in page]
            if all(isinstance(page, list) for page in raw_events)
            else raw_events
        )
        transitions = [
            event
            for event in events
            if isinstance(event, dict)
            and event.get("event") in {"closed", "reopened"}
            and isinstance(event.get("id"), int)
            and not isinstance(event.get("id"), bool)
            and isinstance(event.get("created_at"), str)
        ]
        return sorted(
            transitions,
            key=lambda event: (str(event["created_at"]), int(event["id"])),
        )

    def _ticket_transition_watermark(self, ticket_number: int) -> int:
        transitions = self._ticket_transitions(ticket_number)
        return max((int(event["id"]) for event in transitions), default=0)

    @staticmethod
    def _publisher_close_intent(
        issue: dict[str, Any],
        marker: str,
        publisher_login: str,
        *,
        run_id: str,
        ticket_number: int,
        intent_binding: str,
    ) -> dict[str, Any] | None:
        comments = issue.get("comments")
        if not isinstance(comments, list):
            return None
        intents = [
            _mapping(comment)
            for comment in comments
            if marker in str(_mapping(comment).get("body", ""))
        ]
        trusted = [
            intent
            for intent in intents
            if isinstance(intent.get("createdAt"), str)
            and isinstance(intent.get("author"), dict)
            and intent["author"].get("login") == publisher_login
        ]
        if not trusted:
            return None
        intent = max(trusted, key=lambda item: str(item["createdAt"]))
        baseline_marker = (
            f"<!-- agent-run:{run_id}:ticket-{ticket_number}:"
            f"publisher-close-baseline:{intent_binding}:"
        )
        body = str(intent.get("body", ""))
        start = body.find(baseline_marker)
        if start < 0:
            return None
        value_start = start + len(baseline_marker)
        value_end = body.find(" -->", value_start)
        if value_end < 0:
            return None
        try:
            baseline_event_id = int(body[value_start:value_end])
        except ValueError:
            return None
        return {
            "actor": publisher_login,
            "intent_created_at": str(intent["createdAt"]),
            "baseline_event_id": baseline_event_id,
            "intent_binding": intent_binding,
        }

    def _publisher_login(self) -> str:
        identity = _mapping(self._json("api", "user"))
        login = identity.get("login")
        if not isinstance(login, str) or not login:
            raise GitHubReadError(
                "github_invalid_response", "authenticated GitHub login is missing"
            )
        return login

    def recover_abandoned_ticket(
        self,
        *,
        ticket_number: int,
        run_id: str,
        pr_number: int,
        integrated_sha: str,
        expected_ownership: dict[str, Any],
    ) -> bool:
        expected_binding = f"pr-{pr_number}:sha-{integrated_sha}"
        if expected_ownership.get("intent_binding") != expected_binding:
            raise GitHubReadError(
                "ticket_close_reconciliation_pending",
                "recorded Ticket close belongs to a different PR generation",
            )
        issue = _mapping(
            self._json(
                "issue",
                "view",
                str(ticket_number),
                "--repo",
                self.repository,
                "--json",
                "state,comments,updatedAt",
            )
        )
        marker = f"<!-- agent-run:{run_id}:ticket-{ticket_number}:abandoned -->"
        comments = issue.get("comments")
        publisher_login = expected_ownership.get("actor")
        if not isinstance(publisher_login, str) or not publisher_login:
            raise ValueError("expected Ticket ownership is missing its Publisher")
        already_recorded = isinstance(comments, list) and any(
            marker in str(_mapping(comment).get("body", ""))
            and isinstance(_mapping(comment).get("author"), dict)
            and _mapping(comment)["author"].get("login") == publisher_login
            for comment in comments
        )
        body = (
            f"{marker}\nDelivery Run `{run_id}` was abandoned before entering "
            f"the default branch. Reopened Ticket #{ticket_number}; its prior "
            f"completion was recorded by PR #{pr_number} at Run Branch commit "
            f"`{integrated_sha}`."
        )
        if issue.get("state") != "CLOSED":
            if issue.get("state") != "OPEN":
                return False
            completed = self._publisher_reopen_completed(
                issue,
                ticket_number=ticket_number,
                run_id=run_id,
                expected_ownership=expected_ownership,
                publisher_login=publisher_login,
            )
            if completed and not already_recorded:
                self._require(
                    "issue",
                    "comment",
                    str(ticket_number),
                    "--repo",
                    self.repository,
                    "--body",
                    body,
                )
            return completed
        ownership = self._ticket_close_ownership(
            ticket_number=ticket_number,
            run_id=run_id,
            recorded_ownership=expected_ownership,
        )
        if ownership is None:
            return False
        self._require(
            "issue",
            "reopen",
            str(ticket_number),
            "--repo",
            self.repository,
        )
        if not already_recorded:
            self._require(
                "issue",
                "comment",
                str(ticket_number),
                "--repo",
                self.repository,
                "--body",
                body,
            )
        return True

    def _publisher_reopen_completed(
        self,
        issue: dict[str, Any],
        *,
        ticket_number: int,
        run_id: str,
        expected_ownership: dict[str, Any],
        publisher_login: str,
    ) -> bool:
        expected_event_id = expected_ownership.get("event_id")
        if expected_event_id is None:
            raise ValueError("expected Ticket ownership requires an exact close event")
        transitions = self._ticket_transitions(ticket_number)
        expected_indexes = [
            index
            for index, event in enumerate(transitions)
            if str(event["id"]) == str(expected_event_id)
        ]
        if not expected_indexes:
            raise GitHubReadError(
                "ticket_reopen_reconciliation_pending",
                "GitHub has not exposed the expected close event yet",
            )
        later = transitions[expected_indexes[-1] + 1 :]
        if not later:
            raise GitHubReadError(
                "ticket_reopen_reconciliation_pending",
                "GitHub has not exposed the Ticket reopen event yet",
            )
        latest = later[-1]
        actor = latest.get("actor")
        if (
            latest.get("event") != "reopened"
            or not isinstance(actor, dict)
            or actor.get("login") != publisher_login
        ):
            return False
        if not self._ticket_current_at_transition(
            issue,
            latest,
            publisher_login=publisher_login,
            run_id=run_id,
            ticket_number=ticket_number,
            allow_recovery_marker=True,
        ):
            raise GitHubReadError(
                "ticket_reopen_reconciliation_pending",
                "Ticket updates are newer than the Publisher reopen event",
            )
        return True

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

    def _run(
        self, *arguments: str, retry: bool | None = None
    ) -> subprocess.CompletedProcess[str]:
        command = ["gh", *arguments]
        if retry if retry is not None else _is_read_command(arguments):
            return run_read_command(command, cwd=self.git.root)
        return run_write_command(command, cwd=self.git.root)


def _mapping(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise GitHubReadError("github_invalid_response", "expected an object")
    return value


def _integer(data: dict[str, Any], key: str) -> int:
    value = data.get(key)
    if not isinstance(value, int):
        raise GitHubReadError("github_invalid_response", f"{key} must be an integer")
    return value


def _is_read_command(arguments: tuple[str, ...]) -> bool:
    if not arguments:
        return False
    command = arguments[0]
    if command == "api":
        return (
            "--method" not in arguments
            and not any(argument.startswith("body=") for argument in arguments)
        )
    if command == "repo":
        return arguments[1:2] == ("view",)
    if command == "pr":
        return arguments[1:2] in {("list",), ("view",), ("checks",)}
    if command == "issue":
        return arguments[1:2] == ("view",) or (
            arguments[1:2] == ("develop",) and "--list" in arguments
        )
    return False


def _matches_ref(reference: str, pattern: object) -> bool:
    if not isinstance(pattern, str):
        raise GitHubReadError("github_invalid_response", "ruleset ref pattern must be a string")
    if pattern == "~ALL":
        return True
    if pattern.startswith("~"):
        raise GitHubReadError(
            "github_unsupported_ruleset", f"unsupported ruleset ref pattern: {pattern}"
        )
    expression = ""
    position = 0
    while position < len(pattern):
        character = pattern[position]
        if character == "*":
            if position + 1 < len(pattern) and pattern[position + 1] == "*":
                expression += ".*"
                position += 2
                continue
            expression += "[^/]*"
        elif character == "?":
            expression += "[^/]"
        elif character == "[":
            raise GitHubReadError(
                "github_unsupported_ruleset", "unsupported ruleset ref character class"
            )
        else:
            expression += re.escape(character)
        position += 1
    return re.fullmatch(expression, reference) is not None


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
