from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from typing import Any
from urllib.parse import quote

from agent_run.git import GitError, GitRepository, is_managed_delivery_branch
from agent_run.github import (
    GhGitHubReader,
    GitHubReadError,
    MergeOutcomeUnknownError,
    _merge_identity_matches,
)
from agent_run.github_retry import run_read_command, run_write_command
from agent_run.revisions import effective_revision_from_graph
from agent_run.required_checks import annotate_configured_code_failures

_ACCEPTANCE_MARKER = "<!-- agent-run:acceptance-record -->"
_RUN_PUBLICATION_MARKER = "<!-- agent-run:run-publication-record -->"
_AGENT_RUN_STATUS_MARKER = "<!-- agent-run:agent-run-status -->"


class GhGitHubPublisher:
    """GitHub/Git mutation adapter used only by the trusted Publisher process."""

    def __init__(self, repository: str, git: GitRepository) -> None:
        self.repository = repository
        self.git = git
        self._write_guard: Callable[[], None] | None = None

    def _set_write_guard(self, guard: Callable[[], None]) -> None:
        """Bind the owning Executor check to each actual mutation dispatch."""

        self._write_guard = guard

    def ensure_change_branch(
        self,
        *,
        branch: str,
        base_branch: str,
        expected_base_sha: str,
        expected_remote_sha: str,
        recovery_remote_sha: str,
    ) -> None:
        if is_managed_delivery_branch(base_branch):
            self._ensure_remote_run_branch(base_branch, expected_base_sha)
        remote_sha = self._remote_branch_sha(branch)
        if remote_sha in {expected_remote_sha, recovery_remote_sha}:
            return
        if remote_sha is not None:
            raise GitError("Change ref has a foreign identity")
        if expected_remote_sha != expected_base_sha:
            raise GitError("Change ref is missing after publication")
        created = self._write_command(
            [
                "git",
                "push",
                "origin",
                f"{expected_base_sha}:refs/heads/{branch}",
                f"--force-with-lease=refs/heads/{branch}:",
            ],
        )
        if created.returncode != 0:
            observed = self._remote_branch_sha(branch)
            if observed == expected_base_sha:
                return
            if observed is not None:
                raise GitError("Change ref has a foreign identity")
            raise GitHubReadError(
                "github_write_failed", created.stderr.strip() or "could not create Change ref"
            )
        if self._remote_branch_sha(branch) != expected_base_sha:
            raise GitError("Change ref creation readback did not match intent")

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
        deleted = self._write_command(
            ["git", "push", "origin", "--delete", branch],
        )
        if deleted.returncode != 0:
            raise GitError(deleted.stderr.strip() or "could not delete remote branch")

    def ensure_ticket_branch(
        self,
        *,
        ticket_number: int,
        branch: str,
        base_branch: str,
        expected_base_sha: str,
        expected_remote_sha: str,
        recovery_remote_sha: str,
    ) -> None:
        del ticket_number
        self.ensure_change_branch(
            branch=branch,
            base_branch=base_branch,
            expected_base_sha=expected_base_sha,
            expected_remote_sha=expected_remote_sha,
            recovery_remote_sha=recovery_remote_sha,
        )

    def link_issue_branch_display(
        self, *, issue_number: int, branch: str, head_sha: str
    ) -> str:
        """Create and exactly read back the optional Issue Linked Branch.

        This uses GitHub's GraphQL API directly instead of ``gh issue develop``;
        it neither reads nor writes local Git configuration.  The caller owns
        the once-only intent and treats every API or readback failure as a
        display-only unavailable result.
        """
        try:
            issue = _mapping(
                self._json(
                    "issue", "view", str(issue_number), "--repo", self.repository, "--json", "id"
                )
            )
            issue_id = _string(issue, "id")
            mutation = (
                "mutation($issueId: ID!, $name: String!, $oid: GitObjectID!) "
                "{ createLinkedBranch(input: {issueId: $issueId, name: $name, oid: $oid}) "
                "{ linkedBranch { ref { name target { oid } } } } }"
            )
            result = self._run(
                "api", "graphql", "-f", f"query={mutation}",
                "-F", f"issueId={issue_id}", "-F", f"name={branch}", "-F", f"oid={head_sha}",
                retry=False,
            )
            if result.returncode != 0:
                return "unavailable"
            payload = _mapping(json.loads(result.stdout or "null"))
            data = _mapping(payload.get("data"))
            created = _mapping(data.get("createLinkedBranch"))
            linked = _mapping(created.get("linkedBranch"))
            ref = _mapping(linked.get("ref"))
            target = _mapping(ref.get("target"))
            if ref.get("name") == branch and target.get("oid") == head_sha:
                return "linked"
        except (GitHubReadError, OSError, json.JSONDecodeError):
            pass
        return "unavailable"

    def ensure_parent_branch(self, **authority: str) -> None:
        self.ensure_change_branch(**authority)

    def ensure_run_repair_branch(self, **authority: str) -> None:
        self.ensure_change_branch(**authority)

    def ensure_run_repair_pr(self, **authority: str) -> int:
        return self.ensure_change_pr(**authority)

    def ensure_final_run_ref(self, *, branch: str, expected_head_sha: str) -> None:
        self._ensure_remote_run_branch(branch, expected_head_sha)

    def final_run_ref_matches(self, *, branch: str, expected_head_sha: str) -> bool:
        return self._remote_branch_sha(branch) == expected_head_sha

    def ensure_run_pr(
        self,
        *,
        branch: str,
        base_branch: str,
        expected_head_sha: str,
        expected_base_sha: str,
        title: str,
        body: str,
    ) -> int:
        existing = self.find_run_pr(
            branch=branch,
            expected_head_sha=expected_head_sha,
            expected_base_branch=base_branch,
            expected_base_sha=expected_base_sha,
        )
        if existing is not None:
            return existing
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
            "pr", "view", branch, "--repo", self.repository, "--json", "number"
        )
        pr_number = _integer(_mapping(created), "number")
        self._require_final_run_pr_identity(
            self.live_pull_request(pr_number),
            branch=branch,
            expected_head_sha=expected_head_sha,
            expected_base_branch=base_branch,
            expected_base_sha=expected_base_sha,
        )
        return pr_number

    def refresh_run_pr_narrative(
        self,
        *,
        pr_number: int,
        expected_head_branch: str,
        expected_head_sha: str,
        expected_base_branch: str,
        expected_base_sha: str,
        title: str,
        body: str,
    ) -> None:
        live = self.live_pull_request(pr_number)
        self._require_final_run_pr_identity(
            live,
            branch=expected_head_branch,
            expected_head_sha=expected_head_sha,
            expected_base_branch=expected_base_branch,
            expected_base_sha=expected_base_sha,
        )
        self._require(
            "pr",
            "edit",
            str(pr_number),
            "--repo",
            self.repository,
            "--title",
            title,
            "--body",
            body,
        )

    def find_run_pr(
        self,
        *,
        branch: str,
        expected_head_sha: str,
        expected_base_branch: str,
        expected_base_sha: str,
    ) -> int | None:
        pulls = self._json(
            "pr",
            "list",
            "--repo",
            self.repository,
            "--state",
            "open",
            "--head",
            branch,
            "--json",
            "number",
        )
        if not isinstance(pulls, list):
            raise GitHubReadError("github_invalid_response", "PR list must be an array")
        if len(pulls) > 1:
            raise GitHubReadError(
                "ambiguous_run_pr", "more than one open final Run PR exists"
            )
        if not pulls:
            return None
        pr_number = _integer(_mapping(pulls[0]), "number")
        self._require_final_run_pr_identity(
            self.live_pull_request(pr_number),
            branch=branch,
            expected_head_sha=expected_head_sha,
            expected_base_branch=expected_base_branch,
            expected_base_sha=expected_base_sha,
        )
        return pr_number

    def ensure_parent_pr(self, **authority: str) -> int:
        return self.ensure_change_pr(**authority)

    def abandon_parent_pr(self, pr_number: int) -> bool:
        live = self.live_pull_request(pr_number)
        if live.get("state") != "OPEN":
            return False
        self._require("pr", "close", str(pr_number), "--repo", self.repository)
        return True

    def record_run_publication(self, pr_number: int, record: dict[str, Any]) -> None:
        body = (
            f"{_RUN_PUBLICATION_MARKER}\n"
            "## Run Publication Record\n\n```json\n"
            f"{json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True)}\n```"
        )
        self._record_marker_comment(
            pr_number=pr_number,
            marker=_RUN_PUBLICATION_MARKER,
            body=body,
            label="Run publication record",
        )

    def normal_merge(
        self,
        *,
        pr_number: int,
        expected_head_sha: str,
        expected_head_branch: str | None = None,
        expected_head_repository: str | None = None,
        expected_base_branch: str | None = None,
        expected_base_sha: str | None = None,
        expected_base_repository: str | None = None,
    ) -> str:
        if any(
            value is not None
            for value in (
                expected_head_branch,
                expected_head_repository,
                expected_base_branch,
                expected_base_sha,
                expected_base_repository,
            )
        ):
            live = self.live_pull_request(pr_number)
            if live.get("state") != "OPEN" or not _merge_identity_matches(
                live,
                expected_head_sha=expected_head_sha,
                expected_head_branch=expected_head_branch,
                expected_head_repository=expected_head_repository,
                expected_base_branch=expected_base_branch,
                expected_base_sha=expected_base_sha,
                expected_base_repository=expected_base_repository,
            ):
                raise GitHubReadError(
                    "foreign_run_pr", "Final Run PR does not match merge identity"
                )
        merged = self._run(
            "pr",
            "merge",
            str(pr_number),
            "--repo",
            self.repository,
            "--merge",
            "--match-head-commit",
            expected_head_sha,
        )
        try:
            live = self.live_pull_request(pr_number)
        except GitHubReadError as error:
            raise MergeOutcomeUnknownError(
                "could not determine final merge outcome"
            ) from error
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
        body = (
            f"{marker}\nDelivery Run `{run_id}` completed this Parent Issue "
            f"in {delivery_type} PR #{pr_number}; merge commit `{integrated_sha}` "
            "is verified on the default branch."
        )
        comments = issue.get("comments")
        marker_comments = (
            [
                _mapping(comment)
                for comment in comments
                if marker in str(_mapping(comment).get("body", ""))
            ]
            if isinstance(comments, list)
            else []
        )
        if marker_comments and not any(
            comment.get("body") == body for comment in marker_comments
        ):
            raise GitHubReadError(
                "github_write_outcome_unknown",
                "Parent Issue completion marker body does not match the intent",
            )
        already_recorded = bool(marker_comments)
        if not already_recorded:
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
        observed = _mapping(
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
        observed_comments = observed.get("comments")
        if (
            observed.get("state") != "CLOSED"
            or not isinstance(observed_comments, list)
            or not any(
                _mapping(comment).get("body") == body
                for comment in observed_comments
            )
        ):
            raise GitHubReadError(
                "github_write_outcome_unknown",
                "Parent Issue close remote readback did not match the intent",
            )

    def abandon_run_pr(self, pr_number: int) -> None:
        live = self.live_pull_request(pr_number)
        if live.get("state") == "OPEN":
            self._require("pr", "close", str(pr_number), "--repo", self.repository)

    def abandon_change_pr(self, pr_number: int) -> bool:
        live = self.live_pull_request(pr_number)
        if live.get("state") != "OPEN":
            return False
        self._require("pr", "close", str(pr_number), "--repo", self.repository)
        return True

    def _ensure_remote_run_branch(self, branch: str, expected_sha: str) -> None:
        """Create or recover one Run/Parent ref by its durable exact SHA.

        A same-named ref is not ownership evidence: it is usable only when its
        readback is the exact SHA recorded by the Controller before publication.
        """
        remote_sha = self._remote_branch_sha(branch)
        if remote_sha == expected_sha:
            return
        if remote_sha is not None:
            raise GitError("Run ref has a foreign identity")
        pushed = self._write_command(
            [
                "git",
                "push",
                "origin",
                f"{expected_sha}:refs/heads/{branch}",
                f"--force-with-lease=refs/heads/{branch}:",
            ],
        )
        if pushed.returncode != 0:
            observed = self._remote_branch_sha(branch)
            if observed == expected_sha:
                return
            if observed is not None:
                raise GitError("Run ref has a foreign identity")
            raise GitHubReadError(
                "github_write_failed", pushed.stderr.strip() or "could not create Run ref"
            )
        if self._remote_branch_sha(branch) != expected_sha:
            raise GitError("Run ref creation readback did not match intent")

    def publish_branch(
        self,
        branch: str,
        head_sha: str,
        *,
        expected_remote_sha: str,
    ) -> None:
        current_remote_sha = self._remote_branch_sha(branch)
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
        pushed = self._write_command(arguments)
        if pushed.returncode != 0:
            observed = self._remote_branch_sha(branch)
            if observed == head_sha:
                return
            if observed != expected_remote_sha:
                raise GitError("remote ticket branch drifted")
            raise GitHubReadError(
                "github_write_failed",
                pushed.stderr.strip() or "could not publish ticket branch",
            )
        if self._remote_branch_sha(branch) != head_sha:
            raise GitError("Ticket ref publication readback did not match intent")

    def verify_change_pr_before_publish(
        self,
        *,
        branch: str,
        base_branch: str,
        expected_head_sha: str,
        expected_base_sha: str,
    ) -> None:
        pulls = self._change_prs(branch)
        if len(pulls) > 1:
            raise GitHubReadError(
                "ambiguous_change_pr", "more than one open Change PR exists"
            )
        if pulls:
            self._require_change_pr_identity(
                _mapping(pulls[0]),
                branch=branch,
                base_branch=base_branch,
                expected_head_sha=expected_head_sha,
                expected_base_sha=expected_base_sha,
            )

    def verify_ticket_pr_before_publish(
        self,
        *,
        branch: str,
        base_branch: str,
        expected_head_sha: str,
        expected_base_sha: str,
    ) -> None:
        self.verify_change_pr_before_publish(
            branch=branch,
            base_branch=base_branch,
            expected_head_sha=expected_head_sha,
            expected_base_sha=expected_base_sha,
        )

    def ensure_change_pr(
        self,
        *,
        branch: str,
        base_branch: str,
        title: str,
        body: str,
        expected_head_sha: str,
        expected_base_sha: str,
    ) -> int:
        pulls = self._change_prs(branch)
        if len(pulls) > 1:
            raise GitHubReadError(
                "ambiguous_change_pr", "more than one open Change PR exists"
            )
        if pulls:
            existing = _mapping(pulls[0])
            number = _integer(existing, "number")
            self._require_change_pr_identity(
                existing,
                branch=branch,
                base_branch=base_branch,
                expected_head_sha=expected_head_sha,
                expected_base_sha=expected_base_sha,
            )
            if existing.get("title") != title or existing.get("body") != body:
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
            refreshed = self._change_prs(branch)
            if len(refreshed) != 1:
                raise GitHubReadError(
                    "change_pr_readback_missing",
                    "Change PR refresh did not produce one exact PR",
                )
            current = _mapping(refreshed[0])
            self._require_change_pr_identity(
                current,
                branch=branch,
                base_branch=base_branch,
                expected_head_sha=expected_head_sha,
                expected_base_sha=expected_base_sha,
            )
            if current.get("title") != title or current.get("body") != body:
                raise GitHubReadError(
                    "change_pr_narrative_readback_mismatch",
                    "Change PR narrative readback did not match publication artifact",
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
        recovered = self._change_prs(branch)
        if len(recovered) != 1:
            raise GitHubReadError(
                "change_pr_readback_missing",
                "Change PR creation did not produce one exact PR",
            )
        created = _mapping(recovered[0])
        self._require_change_pr_identity(
            created,
            branch=branch,
            base_branch=base_branch,
            expected_head_sha=expected_head_sha,
            expected_base_sha=expected_base_sha,
        )
        return _integer(created, "number")

    def ensure_ticket_pr(
        self,
        *,
        primary_ticket: int,
        **authority: str,
    ) -> int:
        del primary_ticket
        return self.ensure_change_pr(**authority)

    def _remote_branch_sha(self, branch: str) -> str | None:
        remote = run_read_command(
            ["git", "ls-remote", "--heads", "origin", f"refs/heads/{branch}"],
            cwd=self.git.root,
        )
        if remote.returncode != 0:
            raise GitHubReadError(
                "github_read_failed", remote.stderr.strip() or "could not read Ticket ref"
            )
        return remote.stdout.split()[0] if remote.stdout.strip() else None

    def _change_prs(self, branch: str) -> list[object]:
        owner = self.repository.split("/", 1)[0]
        pulls = self._json(
            "api",
            f"repos/{self.repository}/pulls",
            "--method",
            "GET",
            "-f",
            "state=open",
            "-f",
            f"head={owner}:{branch}",
            "-f",
            "per_page=2",
        )
        if not isinstance(pulls, list):
            raise GitHubReadError("github_invalid_response", "PR list must be an array")
        return pulls

    def _require_change_pr_identity(
        self,
        pull: dict[str, Any],
        *,
        branch: str,
        base_branch: str,
        expected_head_sha: str,
        expected_base_sha: str,
    ) -> None:
        head = _mapping(pull.get("head"))
        base = _mapping(pull.get("base"))
        head_repo = _mapping(head.get("repo"))
        base_repo = _mapping(base.get("repo"))
        if (
            head.get("ref") != branch
            or head.get("sha") != expected_head_sha
            or head_repo.get("full_name") != self.repository
            or base.get("ref") != base_branch
            or base.get("sha") != expected_base_sha
            or base_repo.get("full_name") != self.repository
        ):
            raise GitHubReadError(
                "change_pr_identity_mismatch",
                "Change PR does not match the durable ref and base identity",
            )

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
        buckets = {str(_mapping(check).get("bucket", "")).lower() for check in checks}
        if not buckets:
            return "none"
        if buckets - {"fail", "cancel", "pending", "pass", "skipping", "neutral"}:
            return "unknown"
        if buckets & {"fail", "cancel"}:
            return "fail"
        if "pending" in buckets:
            return "pending"
        return "pass"

    def required_checks_snapshot(
        self, pr_number: int, *, expected_head_sha: str
    ) -> dict[str, Any]:
        live = self.live_pull_request(pr_number)
        if live.get("head_sha") != expected_head_sha:
            raise GitHubReadError(
                "change_pr_head_drift",
                "Required Checks snapshot does not match the expected PR head",
            )
        checks = [
            dict(_mapping(check))
            for check in self._checks(
                pr_number, "bucket,state,name,link,workflow,description"
            )
        ]
        live = self.live_pull_request(pr_number)
        if live.get("head_sha") != expected_head_sha:
            raise GitHubReadError(
                "change_pr_head_drift",
                "Required Checks snapshot does not match the expected PR head",
            )
        buckets = {str(check.get("bucket", "")).lower() for check in checks}
        result = (
            "none"
            if not buckets
            else "fail"
            if buckets & {"fail", "cancel"}
            else "pending"
            if "pending" in buckets
            else "unknown"
            if buckets - {"fail", "cancel", "pending", "pass", "skipping", "neutral"}
            else "pass"
        )
        return {
            "pr_number": pr_number,
            "head_sha": expected_head_sha,
            "result": result,
            "checks": checks,
        }

    def required_check_evidence(
        self, pr_number: int, *, expected_head_sha: str | None = None
    ) -> dict[str, Any]:
        if expected_head_sha is None:
            checks = self._checks(pr_number, "bucket,name,link,workflow,description")
            legacy_failed = [
                dict(_mapping(check))
                for check in checks
                if str(_mapping(check).get("bucket", "")).lower() in {"fail", "cancel"}
            ]
            return {"pr_number": pr_number, "checks": legacy_failed}
        checks = self._checks(pr_number, "bucket,state,name,link,workflow,description")
        failed: list[dict[str, Any]] = []
        for raw in checks:
            check = dict(_mapping(raw))
            if str(check.get("bucket", "")).lower() not in {"fail", "cancel"}:
                continue
            job_id = _actions_job_id(check.get("link"), self.repository)
            if job_id is not None:
                job = _mapping(
                    self._json(
                        "api",
                        f"repos/{self.repository}/actions/jobs/{job_id}",
                    )
                )
                if _integer(job, "id") != job_id:
                    raise GitHubReadError(
                        "github_invalid_response",
                        "Actions job response does not match the requested job",
                    )
                check["job"] = dict(job)
            failed.append(check)
        failed = annotate_configured_code_failures(
            failed,
            self.git.root,
            expected_head_sha=expected_head_sha,
        )
        return {"pr_number": pr_number, "checks": failed}

    def _checks(self, pr_number: int, fields: str) -> list[object]:
        requested_fields = tuple(field for field in fields.split(",") if field)
        check_fields = ",".join(dict.fromkeys((*requested_fields, "name")))
        arguments = (
            "pr",
            "checks",
            str(pr_number),
            "--repo",
            self.repository,
            "--required",
            "--json",
            check_fields,
        )
        result = self._run(*arguments)
        if (
            result.returncode == 1
            and "no required checks reported" in result.stderr.lower()
        ):
            return self._ruleset_checks(pr_number, check_fields)
        if result.returncode not in {0, 1, 8}:
            raise GitHubReadError(
                "github_read_failed", result.stderr.strip() or "gh command failed"
            )
        try:
            checks = json.loads(result.stdout or "null")
        except json.JSONDecodeError as error:
            raise GitHubReadError(
                "github_invalid_response", f"gh returned invalid JSON: {error}"
            ) from error
        if not isinstance(checks, list):
            raise GitHubReadError("github_invalid_response", "checks must be an array")
        for check in checks:
            _string(_mapping(check), "name")
        if not checks:
            return self._ruleset_checks(pr_number, check_fields)
        live = self.live_pull_request(pr_number)
        required_contexts = self._required_check_contexts(
            _string(live, "base_branch")
        )
        if not required_contexts:
            return checks
        return self._project_required_checks(required_contexts, checks)

    def _ruleset_checks(self, pr_number: int, fields: str) -> list[object]:
        live = self.live_pull_request(pr_number)
        required_contexts = self._required_check_contexts(
            _string(live, "base_branch")
        )
        if not required_contexts:
            return []
        checks = self._json(
            "pr",
            "checks",
            str(pr_number),
            "--repo",
            self.repository,
            "--json",
            fields,
            allowed_exit_codes={0, 1, 8},
        )
        if not isinstance(checks, list):
            raise GitHubReadError("github_invalid_response", "checks must be an array")
        return self._project_required_checks(required_contexts, checks)

    def _required_check_contexts(self, branch: str) -> dict[str, int | None]:
        required_contexts = self._ruleset_required_contexts(branch)
        for context in self._branch_protection_required_contexts(branch):
            required_contexts.setdefault(context, None)
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
        return required_contexts

    def _project_required_checks(
        self, required_contexts: dict[str, int | None], checks: list[object]
    ) -> list[object]:
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

    def _branch_protection_required_contexts(self, branch: str) -> list[str]:
        """Read classic branch-protection contexts, including unreported ones.

        ``gh pr checks --required`` omits a protected context until its first
        check run exists.  The protection API is the authority for that
        missing-run case; a 404 means this branch has no classic protection.
        Any other response is a read failure so the caller cannot mistake an
        unavailable protection read for an unconfigured repository.
        """

        endpoint = (
            f"repos/{self.repository}/branches/{quote(branch, safe='')}/"
            "protection/required_status_checks/contexts"
        )
        result = self._run("api", endpoint)
        if result.returncode == 404 or (
            result.returncode != 0 and "http 404" in result.stderr.lower()
        ):
            return []
        if result.returncode != 0:
            raise GitHubReadError(
                "github_read_failed",
                result.stderr.strip() or "could not read branch protection",
            )
        try:
            payload = json.loads(result.stdout or "null")
        except json.JSONDecodeError as error:
            raise GitHubReadError(
                "github_invalid_response",
                f"branch protection contexts are invalid: {error}",
            ) from error
        if not isinstance(payload, list) or not all(
            isinstance(context, str) and context.strip() for context in payload
        ):
            raise GitHubReadError(
                "github_invalid_response",
                "branch protection contexts must be an array of strings",
            )
        return list(dict.fromkeys(payload))

    def _ruleset_required_contexts(self, branch: str) -> dict[str, int | None]:
        pages = self._json(
            "api", f"repos/{self.repository}/rulesets", "--paginate", "--slurp"
        )
        if not isinstance(pages, list) or not all(
            isinstance(page, list) for page in pages
        ):
            raise GitHubReadError(
                "github_invalid_response", "ruleset pages must be arrays"
            )
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
                raise GitHubReadError(
                    "github_invalid_response", "ruleset rules must be an array"
                )
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
                    if integration_id is not None and not isinstance(
                        integration_id, int
                    ):
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
            not includes
            or any(_matches_ref(reference, pattern) for pattern in includes)
        ) and not any(_matches_ref(reference, pattern) for pattern in excludes)

    def live_pull_request(self, pr_number: int) -> dict[str, Any]:
        value = self._json(
            "pr",
            "view",
            str(pr_number),
            "--repo",
            self.repository,
            "--json",
            "headRefName,headRefOid,headRepository,baseRefName,baseRefOid,mergeable,state,mergeCommit",
        )
        data = _mapping(value)
        merge_commit = data.get("mergeCommit")
        integrated_sha = (
            merge_commit.get("oid") if isinstance(merge_commit, dict) else None
        )
        result = {
            "head_branch": data.get("headRefName"),
            "head_sha": data.get("headRefOid"),
            "head_repository": _repository_name(data.get("headRepository")),
            "base_branch": data.get("baseRefName"),
            "base_sha": data.get("baseRefOid"),
            # `gh pr view --repo` addresses the base repository directly.
            # Its JSON field allowlist does not expose GraphQL's
            # `baseRepository`, so querying it would make every read fail
            # before GitHub receives the request.
            "base_repository": self.repository,
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

    def _require_final_run_pr_identity(
        self,
        live: dict[str, Any],
        *,
        branch: str,
        expected_head_sha: str,
        expected_base_branch: str,
        expected_base_sha: str,
    ) -> None:
        if (
            live.get("state") == "OPEN"
            and live.get("head_branch") == branch
            and live.get("head_sha") == expected_head_sha
            and live.get("head_repository") == self.repository
            and live.get("base_branch") == expected_base_branch
            and live.get("base_sha") == expected_base_sha
            and live.get("base_repository") == self.repository
        ):
            return
        raise GitHubReadError(
            "foreign_run_pr", "Final Run PR does not match durable identity"
        )

    def run_pr_narrative_matches(
        self, pr_number: int, *, title: str, body: str
    ) -> bool:
        value = self._json(
            "pr", "view", str(pr_number), "--repo", self.repository,
            "--json", "title,body",
        )
        data = _mapping(value)
        return data.get("title") == title and data.get("body") == body

    def record_acceptance(self, pr_number: int, record: dict[str, Any]) -> None:
        body = (
            f"{_ACCEPTANCE_MARKER}\n"
            "## Fresh Acceptance Record\n\n"
            "```json\n"
            f"{json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True)}\n"
            "```"
        )
        self._record_marker_comment(
            pr_number=pr_number,
            marker=_ACCEPTANCE_MARKER,
            body=body,
            label="Acceptance record",
        )

    def record_agent_run_status(self, pr_number: int, status: dict[str, Any]) -> None:
        body = _render_agent_run_status(status)
        self._record_marker_comment(
            pr_number=pr_number,
            marker=_AGENT_RUN_STATUS_MARKER,
            body=body,
            label="Agent Run status",
        )

    def _record_marker_comment(
        self, *, pr_number: int, marker: str, body: str, label: str
    ) -> None:
        """Apply one marker-owned comment and prove its exact remote body."""

        comments = self._marker_comments(pr_number)
        existing = next(
            (comment for comment in comments if marker in str(comment.get("body", ""))),
            None,
        )
        if existing is not None and existing.get("body") == body:
            return
        if existing is None:
            self._json(
                "api",
                f"repos/{self.repository}/issues/{pr_number}/comments",
                "-f",
                f"body={body}",
            )
        else:
            self._json(
                "api",
                "--method",
                "PATCH",
                f"repos/{self.repository}/issues/comments/{_integer(existing, 'id')}",
                "-f",
                f"body={body}",
            )
        if not any(
            comment.get("body") == body for comment in self._marker_comments(pr_number)
        ):
            raise GitHubReadError(
                "github_write_outcome_unknown",
                f"{label} remote readback did not match the intended body",
            )

    def _marker_comments(self, pr_number: int) -> list[dict[str, Any]]:
        comments = self._json(
            "api",
            f"repos/{self.repository}/issues/{pr_number}/comments",
            "--paginate",
            "--slurp",
        )
        return [
            _mapping(comment) for comment in _flatten_pages(comments, "comments")
        ]

    def has_supersession_close_receipt(
        self, pr_number: int, generation: object, close_nonce: object
    ) -> bool:
        if type(generation) is not int or not isinstance(close_nonce, str):
            return False
        live = self.live_pull_request(pr_number)
        if live.get("state") != "CLOSED":
            return False
        comments = self._json(
            "api",
            f"repos/{self.repository}/issues/{pr_number}/comments",
            "--paginate",
            "--slurp",
        )
        comments = _flatten_pages(comments, "comments")
        events = self._json(
            "api",
            f"repos/{self.repository}/issues/{pr_number}/events",
            "--paginate",
            "--slurp",
        )
        events = _flatten_pages(events, "events")
        viewer = self._json("api", "user")
        viewer_login = _mapping(viewer).get("login")
        if not isinstance(viewer_login, str) or not viewer_login:
            return False
        if not self.has_supersession_close_intent(pr_number, generation, close_nonce):
            return False
        lifecycle_events = [
            _mapping(event)
            for event in events
            if _mapping(event).get("event") in {"closed", "reopened"}
        ]
        if not lifecycle_events:
            return False
        if not all(type(event.get("id")) is int for event in lifecycle_events):
            return False
        latest = max(lifecycle_events, key=lambda event: int(event["id"]))
        return (
            latest.get("event") == "closed"
            and _mapping(latest.get("actor", {})).get("login") == viewer_login
        )

    def has_supersession_close_intent(
        self, pr_number: int, generation: object, close_nonce: object
    ) -> bool:
        if type(generation) is not int or not isinstance(close_nonce, str):
            return False
        comments = self._json(
            "api",
            f"repos/{self.repository}/issues/{pr_number}/comments",
            "--paginate",
            "--slurp",
        )
        viewer = self._json("api", "user")
        viewer_login = _mapping(viewer).get("login")
        if not isinstance(viewer_login, str):
            return False
        comments = _flatten_pages(comments, "comments")
        return any(
            _supersession_status_matches(
                str(_mapping(comment).get("body", "")), generation, close_nonce
            )
            and _mapping(_mapping(comment).get("user", {})).get("login") == viewer_login
            for comment in comments
        )

    def has_supersession_close_record(
        self, pr_number: int, generation: object, close_nonce: object
    ) -> bool:
        if type(generation) is not int or not isinstance(close_nonce, str):
            return False
        comments = self._json(
            "api",
            f"repos/{self.repository}/issues/{pr_number}/comments",
            "--paginate",
            "--slurp",
        )
        viewer = self._json("api", "user")
        viewer_login = _mapping(viewer).get("login")
        if not isinstance(viewer_login, str):
            return False
        comments = _flatten_pages(comments, "comments")
        record = (
            f"{_AGENT_RUN_STATUS_MARKER}\n"
            "## Superseded Change Generation\n\n"
            "- Scope: `superseded_generation`\n"
            f"- Generation: `{generation}`\n"
            "- Retirement: `closed`\n"
            f"- Close nonce: `{close_nonce}`"
        )
        return any(
            record in str(_mapping(comment).get("body", ""))
            and _mapping(_mapping(comment).get("user", {})).get("login") == viewer_login
            for comment in comments
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

    def sync_run_branch(self, *, run_branch: str, integrated_sha: str) -> None:
        fetched = self._write_command(
            ["git", "fetch", "--no-tags", "origin", run_branch]
        )
        if fetched.returncode != 0:
            raise GitHubReadError(
                "github_read_failed",
                fetched.stderr.strip() or "could not fetch merged Run Branch"
            )
        fetched_sha = self.git.resolve("FETCH_HEAD")
        if fetched_sha != integrated_sha:
            raise GitError("remote Run Branch does not match integrated commit")
        self._write_command(
            ["git", "update-ref", f"refs/heads/{run_branch}", integrated_sha]
        ).check_returncode()

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
        before_dispatch: Callable[[], None] | None = None,
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
                "ticket_close_external_conflict",
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
                    "ticket_close_external_conflict",
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
                    "ticket_close_external_conflict",
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
                "ticket_close_external_conflict",
                "Ticket changed after the Publisher close intent",
            )
        if before_dispatch is not None:
            before_dispatch()
        self._require("issue", "close", str(ticket_number), "--repo", self.repository)
        ownership = self._ticket_close_ownership(
            ticket_number=ticket_number,
            run_id=run_id,
            recorded_ownership=prepared,
        )
        if ownership is None:
            raise GitHubReadError(
                "ticket_close_external_conflict",
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
        return (
            self.ticket_close_ownership(
                ticket_number=ticket_number,
                run_id=run_id,
                recorded_ownership=recorded_ownership,
            )
            is not None
        )

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
        require_recovery_currentness: bool = False,
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
                and recorded_ownership.get("dispatch_attempted") is not True
                and issue.get("state") == "OPEN"
            ):
                return None
            if (
                isinstance(recorded_ownership, dict)
                and recorded_ownership.get("event_id") is None
                and recorded_ownership.get("dispatch_attempted") is True
                and issue.get("state") == "OPEN"
            ):
                raise GitHubReadError(
                    "ticket_close_dispatch_unobserved",
                    "GitHub has not exposed the attempted Ticket close yet",
                )
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
                    allow_recovery_marker=require_recovery_currentness,
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
            if (
                issue.get("state") == "OPEN"
                and recorded_ownership.get("dispatch_attempted") is not True
            ):
                return None
            if (
                recorded_ownership.get("dispatch_attempted") is True
                and issue.get("state") == "OPEN"
            ):
                raise GitHubReadError(
                    "ticket_close_dispatch_unobserved",
                    "GitHub has not exposed the attempted Ticket close yet",
                )
            raise GitHubReadError(
                "ticket_close_ownership_pending",
                "GitHub has not exposed a transition after the close intent baseline yet",
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
        actor = latest_after_baseline.get("actor")
        if (
            not isinstance(actor, dict)
            or not isinstance(actor.get("login"), str)
            or not actor["login"]
        ):
            raise GitHubReadError(
                "ticket_close_ownership_pending",
                "GitHub has not exposed the Ticket transition actor yet",
            )
        if len(after_baseline) != 1:
            return None
        owned = (
            latest_after_baseline
            if latest_after_baseline.get("event") == "closed"
            and actor.get("login") == publisher_login
            else None
        )
        if owned is None or latest_after_baseline["id"] != owned["id"]:
            return None
        if not self._ticket_current_at_transition(
            issue,
            latest_after_baseline,
            publisher_login=publisher_login,
            run_id=run_id,
            ticket_number=ticket_number,
            allow_recovery_marker=require_recovery_currentness,
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
        # Issue.updatedAt and the timeline projection converge independently.
        # Callers have already checked the close intent binding, baseline,
        # transition type/actor and the live CLOSED state; timestamp equality
        # would reject a valid Publisher-owned close during normal propagation.
        if not allow_recovery_marker:
            return True
        updated_at = issue.get("updatedAt")
        transition_time = transition.get("created_at")
        if not isinstance(updated_at, str) or not isinstance(transition_time, str):
            return False
        if updated_at == transition_time:
            return True
        marker = f"<!-- agent-run:{run_id}:ticket-{ticket_number}:abandoned -->"
        comments = issue.get("comments")
        return isinstance(comments, list) and any(
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
            require_recovery_currentness=True,
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
                "github_read_failed"
                if _is_read_command(arguments)
                else "github_write_failed",
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
        if (
            arguments[:1] == ("api",)
            and "--method" in arguments
            and arguments[arguments.index("--method") + 1 :][:1] == ("GET",)
        ):
            # Explicit GET keeps its existing single-dispatch readback behavior.
            return run_write_command(command, cwd=self.git.root)
        return self._write_command(command)

    def _write_command(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        if self._write_guard is not None:
            self._write_guard()
        return run_write_command(command, cwd=self.git.root)


def _mapping(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise GitHubReadError("github_invalid_response", "expected an object")
    return value


def _actions_job_id(link: object, repository: str) -> int | None:
    if not isinstance(link, str):
        return None
    match = re.fullmatch(
        rf"https://github\.com/{re.escape(repository)}/"
        rf"(?:actions/runs/\d+/job|runs/\d+/jobs)/(\d+)(?:[/?#].*)?",
        link,
    )
    return int(match.group(1)) if match is not None else None


def _integer(data: dict[str, Any], key: str) -> int:
    value = data.get(key)
    if not isinstance(value, int):
        raise GitHubReadError("github_invalid_response", f"{key} must be an integer")
    return value


def _repository_name(value: object) -> str | None:
    if not isinstance(value, dict):
        return None
    name = value.get("nameWithOwner")
    return name if isinstance(name, str) else None


def _is_read_command(arguments: tuple[str, ...]) -> bool:
    if not arguments:
        return False
    command = arguments[0]
    if command == "api":
        return "--method" not in arguments and not any(
            argument.startswith("body=") for argument in arguments
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
        raise GitHubReadError(
            "github_invalid_response", "ruleset ref pattern must be a string"
        )
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
    if status.get("scope") == "superseded_generation":
        generation = status.get("generation")
        if type(generation) is not int or generation < 1:
            raise GitHubReadError(
                "github_invalid_response", "generation must be positive"
            )
        if status.get("retirement") not in {"closing", "closed"}:
            raise GitHubReadError(
                "github_invalid_response", "superseded status has invalid retirement"
            )
        nonce = _string(status, "close_nonce")
        return (
            f"{_AGENT_RUN_STATUS_MARKER}\n"
            "## Superseded Change Generation\n\n"
            "- Scope: `superseded_generation`\n"
            f"- Generation: `{generation}`\n"
            f"- Retirement: `{_string(status, 'retirement')}`\n"
            f"- Close nonce: `{nonce}`\n"
            f"- Next action: {_string(status, 'next_action')}"
        )
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
        f"- Fresh Validation: `{_string(status, 'validation_outcome')}` "
        f"({lane_summary})\n"
        f"- Required Checks: `{required}`\n"
        f"- Next action: {_string(status, 'next_action')}"
    )


def _string(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise GitHubReadError("github_invalid_response", f"{key} must be a string")
    return value


def _flatten_pages(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise GitHubReadError("github_invalid_response", f"{label} must be an array")
    if all(isinstance(page, list) for page in value):
        return [item for page in value for item in page]
    if all(isinstance(item, dict) for item in value):
        # Mocks and older gh clients can return the one-page form despite
        # requesting --slurp; it carries the same list semantics.
        return list(value)
    raise GitHubReadError("github_invalid_response", f"{label} pages must be arrays")


def _supersession_status_matches(body: str, generation: int, close_nonce: str) -> bool:
    return (
        _AGENT_RUN_STATUS_MARKER in body
        and "## Superseded Change Generation" in body
        and "- Scope: `superseded_generation`" in body
        and f"- Generation: `{generation}`" in body
        and f"- Close nonce: `{close_nonce}`" in body
    )
