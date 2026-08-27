from __future__ import annotations

import json
import os
import subprocess
import tempfile
from copy import deepcopy
from collections.abc import Callable
from pathlib import Path
from typing import Any

from agent_run.git import GitError, GitRepository, is_managed_delivery_branch
from agent_run.github import GitHubReadError, MergeOutcomeUnknownError
from agent_run.models import Blocker, DeliveryGraph, Issue, ParentIssue, Repository
from agent_run.required_checks import annotate_configured_code_failures
from agent_run.revisions import effective_revision_from_graph


def _fixture_required_check_identity(bucket: str) -> dict[str, str]:
    return {
        "name": "fixture-required-check",
        "workflow": "fixture-ci",
        "bucket": bucket,
        "link": "https://example.invalid/checks/fixture",
    }


def _fixture_observation_check(
    result: str, source: dict[str, Any] | None = None
) -> dict[str, str]:
    check = _fixture_required_check_identity(result)
    if source is not None:
        for key in ("name", "workflow", "link"):
            value = source.get(key)
            if isinstance(value, str) and value:
                check[key] = value
    check["state"] = {
        "pass": "SUCCESS",
        "pending": "PENDING",
        "unknown": "UNKNOWN",
        "fail": "FAILURE",
    }[result]
    return check


def _fixture_check_for_head(
    source: dict[str, Any], head_sha: str
) -> dict[str, Any]:
    check = dict(source)
    job = check.get("job")
    if isinstance(job, dict) and job.get("head_sha") == "$CURRENT_HEAD":
        check["job"] = {**job, "head_sha": head_sha}
    return check


class FixtureGitHubReader:
    """供黑盒测试使用的确定性 GitHub 只读适配器。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.data = self._load()

    def repository(self) -> Repository:
        self.data = self._load()
        failures = self.data.get("repository_read_failures")
        if isinstance(failures, list) and failures:
            configured_error = failures.pop(0)
            self._save_reader_data()
            if not isinstance(configured_error, dict):
                raise GitHubReadError(
                    "invalid_fixture",
                    "repository_read_failures must contain objects",
                )
            raise GitHubReadError(
                str(configured_error.get("code", "github_read_failed")),
                str(configured_error.get("message", "GitHub read failed")),
            )
        default_head = self.data.get("default_head_sha")
        return Repository(
            name_with_owner=_string(self.data, "repository"),
            default_branch=_string(self.data, "default_branch"),
            default_head_sha=default_head if isinstance(default_head, str) else None,
        )

    def repository_hint(self) -> str | None:
        self.data = self._load()
        repository = self.data.get("repository")
        return repository if isinstance(repository, str) else None

    def delivery_graph(self, parent_number: int) -> DeliveryGraph:
        return self._delivery_graph_once(parent_number)

    def live_pull_request(self, pr_number: int) -> dict[str, Any]:
        delivery = _mapping(self._load(), "delivery")
        pulls = delivery.get("pull_requests")
        if not isinstance(pulls, list):
            raise GitHubReadError("invalid_fixture", "delivery.pull_requests must be an array")
        for pull in pulls:
            if isinstance(pull, dict) and pull.get("number") == pr_number:
                branch = _string(pull, "branch")
                published = _mapping(delivery, "published_branches")
                head = published.get(branch) or pull.get("head_sha")
                if not isinstance(head, str):
                    raise GitHubReadError("invalid_fixture", "PR head is missing")
                base_branch = _string(pull, "base_branch")
                base_sha = _mapping(delivery, "published_branches").get(base_branch)
                if not isinstance(base_sha, str):
                    base_sha = self.repository().default_head_sha
                result: dict[str, Any] = {
                    "state": pull.get("state"), "head_sha": head,
                    "base_branch": base_branch,
                    "base_sha": base_sha,
                    "integrated_sha": pull.get("integrated_sha"),
                }
                integrated = pull.get("integrated_sha")
                if isinstance(integrated, str):
                    git = GitRepository(self.path.parent)
                    result.update(
                        {
                            "head_tree": git.resolve(f"{head}^{{tree}}"),
                            "integrated_tree": git.resolve(f"{integrated}^{{tree}}"),
                            "integrated_parents": git.commit_parents(integrated),
                        }
                    )
                return result
        raise GitHubReadError("missing_pull_request", f"PR #{pr_number} is missing")

    def _delivery_graph_once(self, parent_number: int) -> DeliveryGraph:
        self.data = self._load()
        configured_failures = self.data.get("delivery_graph_read_failures")
        if isinstance(configured_failures, list) and configured_failures:
            configured_error = configured_failures.pop(0)
            self._save_reader_data()
            if not isinstance(configured_error, dict):
                raise GitHubReadError(
                    "invalid_fixture",
                    "delivery_graph_read_failures must contain objects",
                )
            raise GitHubReadError(
                str(configured_error.get("code", "github_read_failed")),
                str(configured_error.get("message", "GitHub read failed")),
            )
        configured_error = self.data.get("error")
        if isinstance(configured_error, dict):
            raise GitHubReadError(
                str(configured_error.get("code", "github_read_failed")),
                str(configured_error.get("message", "GitHub read failed")),
            )
        parent_data = _mapping(self.data, "parent")
        actual_parent = _integer(parent_data, "number")
        if actual_parent != parent_number:
            raise GitHubReadError(
                "missing_parent",
                f"fixture contains parent #{actual_parent}, not #{parent_number}",
            )
        sub_issues = parent_data.get("sub_issues")
        if not isinstance(sub_issues, list) or not all(
            isinstance(number, int) for number in sub_issues
        ):
            raise GitHubReadError(
                "invalid_parent", "parent.sub_issues must contain issue numbers"
            )
        parent = ParentIssue(
            number=actual_parent,
            title=_string(parent_data, "title"),
            body=_string(parent_data, "body"),
            sub_issue_numbers=tuple(sub_issues),
            sub_issue_order_reliable=bool(
                parent_data.get("sub_issue_order_reliable", True)
            ),
        )
        raw_issues = _mapping(self.data, "issues")
        issues: dict[int, Issue] = {}
        for number in sub_issues:
            raw_issue = raw_issues.get(str(number))
            if raw_issue is None:
                continue
            if not isinstance(raw_issue, dict):
                raise GitHubReadError(
                    "invalid_fixture", f"issues.{number} must be an object"
                )
            issues[number] = _parse_issue(raw_issue)
        return DeliveryGraph(parent=parent, issues=issues)

    def _load(self) -> dict[str, Any]:
        loaded: object = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise GitHubReadError(
                "invalid_fixture", "fixture root must be an object"
            )
        return loaded

    def _save_reader_data(self) -> None:
        self.path.write_text(json.dumps(self.data), encoding="utf-8")


class FixtureGitHubPublisher:
    """Mutable GitHub substitute for black-box delivery scenarios."""

    def __init__(self, path: Path, git: GitRepository) -> None:
        self.path = path
        self.git = git
        loaded: object = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError("fixture root must be an object")
        self.data = loaded
        delivery = self.data.setdefault("delivery", {})
        if not isinstance(delivery, dict):
            raise ValueError("fixture delivery must be an object")
        delivery.setdefault("linked_branches", {})
        delivery.setdefault("linked_branch_display_attempts", [])
        delivery.setdefault("linked_branch_display_outcomes", "linked")
        delivery.setdefault("published_branches", {})
        delivery.setdefault("pull_requests", [])
        delivery.setdefault("closed_issues", [])
        delivery.setdefault("acceptance_records", [])
        delivery.setdefault("agent_run_status", [])
        delivery.setdefault("run_publication_records", [])
        delivery.setdefault("mutations", [])
        delivery.setdefault("check_position", 0)

    def ensure_run_branch(self, branch: str, base_sha: str) -> None:
        self.git.ensure_run_branch(branch, base_sha)
        self._crash_once("ensure_run_branch")

    def ensure_parent_branch(
        self, *, parent_number: int, branch: str, base_branch: str
    ) -> None:
        parent = _mutable_mapping(self.data, "parent")
        if _integer(parent, "number") != parent_number:
            raise ValueError("fixture parent is missing")
        linked = _mutable_mapping(self._delivery(), "linked_branches")
        linked[str(parent_number)] = branch
        published = _mutable_mapping(self._delivery(), "published_branches")
        published.setdefault(branch, self.git.resolve(base_branch))
        self._save()

    def delete_managed_branch(self, branch: str) -> None:
        if not is_managed_delivery_branch(branch):
            raise ValueError(f"refusing to delete unmanaged branch {branch!r}")
        delivery = self._delivery()
        published = _mutable_mapping(delivery, "published_branches")
        head = published.pop(branch, None)
        if isinstance(head, str):
            for pull in _mutable_list(delivery, "pull_requests"):
                if isinstance(pull, dict) and pull.get("branch") == branch:
                    pull.setdefault("head_sha", head)
        _mutable_list(delivery, "mutations").append(
            {"action": "delete_managed_branch", "branch": branch}
        )
        self._save()

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
        published = _mutable_mapping(self._delivery(), "published_branches")
        current = published.get(branch)
        if current is None:
            if expected_remote_sha != expected_base_sha:
                raise ValueError("fixture Ticket ref is missing after publication")
            published[branch] = expected_base_sha
        elif current not in {expected_remote_sha, recovery_remote_sha}:
            raise ValueError("fixture Ticket ref has a foreign identity")
        self._save()
        self._crash_once("ensure_ticket_branch")

    def link_issue_branch_display(
        self, *, issue_number: int, branch: str, head_sha: str
    ) -> str:
        delivery = self._delivery()
        attempts = _mutable_list(delivery, "linked_branch_display_attempts")
        attempts.append(
            {"issue_number": issue_number, "branch": branch, "head_sha": head_sha}
        )
        configured = delivery.get("linked_branch_display_outcomes", "linked")
        if isinstance(configured, list):
            outcome = configured.pop(0) if configured else "linked"
        else:
            outcome = configured
        if outcome == "linked":
            _mutable_mapping(delivery, "linked_branches")[str(issue_number)] = branch
        elif outcome not in {"api_error", "empty", "missing_readback"}:
            raise ValueError("fixture linked branch display outcome is invalid")
        self._save()
        self._crash_once("link_issue_branch_display")
        if issue_number == _integer(_mutable_mapping(self.data, "parent"), "number"):
            self._crash_once("final_link_issue_branch_display")
        return "linked" if outcome == "linked" else "unavailable"

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
            self._ensure_managed_base_ref(base_branch, expected_base_sha)
        published = _mutable_mapping(self._delivery(), "published_branches")
        current = published.get(branch)
        if current is None:
            if expected_remote_sha != expected_base_sha:
                raise ValueError("fixture Change ref is missing after publication")
            published[branch] = expected_base_sha
        elif current not in {expected_remote_sha, recovery_remote_sha}:
            raise ValueError("fixture Change ref has a foreign identity")
        self._save()
        self._crash_once("ensure_change_branch")
        self._crash_once("ensure_ticket_branch")

    def ensure_run_repair_branch(self, *, branch: str, base_branch: str) -> None:
        published = _mutable_mapping(self._delivery(), "published_branches")
        published.setdefault(branch, self.git.resolve(base_branch))
        self._save()

    def ensure_run_repair_pr(
        self, *, branch: str, base_branch: str, title: str, body: str
    ) -> int:
        pulls = _mutable_list(self._delivery(), "pull_requests")
        matching = [
            pr for pr in pulls if isinstance(pr, dict)
            and pr.get("branch") == branch and pr.get("base_branch") == base_branch
        ]
        if len(matching) > 1:
            raise ValueError("fixture contains duplicate Run Repair PRs")
        if matching:
            pull = matching[0]
        else:
            pull = {
                "number": len(pulls) + 1, "branch": branch,
                "base_branch": base_branch, "state": "OPEN", "scope": "run_repair",
            }
            pulls.append(pull)
        pull.update({"title": title, "body": body})
        self._save()
        return int(pull["number"])

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
        pulls = _mutable_list(self._delivery(), "pull_requests")
        matching = [
            pr for pr in pulls if isinstance(pr, dict)
            and pr.get("branch") == branch and pr.get("base_branch") == base_branch
            and pr.get("scope") == "final_run"
            and pr.get("state") == "OPEN"
        ]
        if len(matching) > 1:
            raise ValueError("fixture contains duplicate final Run PRs")
        if matching:
            pull = matching[0]
            self._require_final_run_pr_identity(
                pull,
                branch=branch,
                expected_head_sha=expected_head_sha,
                expected_base_branch=base_branch,
                expected_base_sha=expected_base_sha,
            )
            return int(pull["number"])
        else:
            pull = {
                "number": len(pulls) + 1,
                "branch": branch,
                "base_branch": base_branch,
                "state": "OPEN",
                "scope": "final_run",
            }
            pulls.append(pull)
        pull.update({"title": title, "body": body})
        self._save()
        self._crash_once("ensure_run_pr")
        self._require_final_run_pr_identity(
            pull,
            branch=branch,
            expected_head_sha=expected_head_sha,
            expected_base_branch=base_branch,
            expected_base_sha=expected_base_sha,
        )
        return int(pull["number"])

    def ensure_final_run_ref(self, *, branch: str, expected_head_sha: str) -> None:
        outcomes = self._delivery().get("final_run_ref_outcomes")
        outcome = outcomes.pop(0) if isinstance(outcomes, list) and outcomes else None
        if outcome in {"foreign", "cas_race"}:
            _mutable_mapping(self._delivery(), "published_branches")[branch] = "foreign"
            self._save()
            raise GitError("fixture Run ref has a foreign identity")
        self._ensure_managed_base_ref(branch, expected_head_sha)
        if outcome == "lost_response":
            raise OSError("fixture lost Run ref response")
        if outcome is not None:
            raise ValueError("fixture final Run ref outcome is invalid")

    def final_run_ref_matches(
        self, *, branch: str, expected_head_sha: str
    ) -> bool:
        return (
            _mutable_mapping(self._delivery(), "published_branches").get(branch)
            == expected_head_sha
        )

    def _ensure_managed_base_ref(self, branch: str, expected_sha: str) -> None:
        """Fixture equivalent of expected-absent Run/Parent ref authority."""
        published = _mutable_mapping(self._delivery(), "published_branches")
        remote_sha = published.get(branch)
        if remote_sha == expected_sha:
            return
        if remote_sha is not None:
            raise ValueError("fixture Run ref has a foreign identity")
        races = self._delivery().get("expected_absent_ref_races")
        if isinstance(races, list) and (branch in races or "*" in races):
            races.remove(branch if branch in races else "*")
            published[branch] = "foreign"
            self._save()
            raise ValueError("fixture Run ref compare-and-swap failed")
        published[branch] = expected_sha
        self._save()
        self._crash_once("ensure_run_ref")

    def find_run_pr(
        self,
        *,
        branch: str,
        expected_head_sha: str,
        expected_base_branch: str,
        expected_base_sha: str,
    ) -> int | None:
        pulls = _mutable_list(self._delivery(), "pull_requests")
        matching = [
            pr
            for pr in pulls
            if isinstance(pr, dict)
            and pr.get("branch") == branch
            and pr.get("scope") == "final_run"
            and pr.get("state") == "OPEN"
        ]
        if len(matching) > 1:
            raise ValueError("fixture contains duplicate final Run PRs")
        if not matching:
            return None
        pull = matching[0]
        self._require_final_run_pr_identity(
            pull,
            branch=branch,
            expected_head_sha=expected_head_sha,
            expected_base_branch=expected_base_branch,
            expected_base_sha=expected_base_sha,
        )
        return int(pull["number"])

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
        self._require_final_run_pr_identity(
            self._pull(pr_number),
            branch=expected_head_branch,
            expected_head_sha=expected_head_sha,
            expected_base_branch=expected_base_branch,
            expected_base_sha=expected_base_sha,
        )
        self._pull(pr_number).update({"title": title, "body": body})
        self._save()

    def ensure_parent_pr(
        self, *, branch: str, base_branch: str, title: str, body: str
    ) -> int:
        _mutable_mapping(self._delivery(), "published_branches").setdefault(
            branch, self.git.resolve(branch)
        )
        pulls = _mutable_list(self._delivery(), "pull_requests")
        matching = [
            pr for pr in pulls if isinstance(pr, dict)
            and pr.get("branch") == branch and pr.get("base_branch") == base_branch
            and pr.get("scope") == "parent_only"
        ]
        if len(matching) > 1:
            raise ValueError("fixture contains duplicate Parent PRs")
        if matching:
            pull = matching[0]
            if pull.get("state") != "OPEN":
                raise ValueError("fixture Parent PR is not open")
        else:
            pull = {
                "number": len(pulls) + 1,
                "branch": branch,
                "base_branch": base_branch,
                "state": "OPEN",
                "scope": "parent_only",
            }
            pulls.append(pull)
        pull.update({"title": title, "body": body})
        self._save()
        self._crash_once("ensure_parent_pr")
        return int(pull["number"])

    def abandon_parent_pr(self, pr_number: int) -> bool:
        pull = self._pull(pr_number)
        self._inject_external_close_before_abandon("parent", pull)
        if pull.get("state") == "OPEN":
            pull["state"] = "CLOSED"
            pull["closed_by"] = self._publisher_login()
            _mutable_list(self._delivery(), "mutations").append(
                {"action": "close_parent_pr", "pr_number": pr_number}
            )
            self._save()
            self._crash_once("abandon_parent_pr")
            return True
        return False

    def record_run_publication(
        self, pr_number: int, record: dict[str, Any]
    ) -> None:
        records = _mutable_list(self._delivery(), "run_publication_records")
        replacement = {"pr_number": pr_number, **record}
        for position, existing in enumerate(records):
            if isinstance(existing, dict) and existing.get("pr_number") == pr_number:
                records[position] = replacement
                break
        else:
            records.append(replacement)
        self._save()
        self._crash_once("record_run_publication")

    def normal_merge(self, *, pr_number: int, expected_head_sha: str) -> str:
        pull = self._pull(pr_number)
        if pull.get("state") == "MERGED":
            return str(pull["integrated_sha"])
        live = self.live_pull_request(pr_number)
        if live["head_sha"] != expected_head_sha or not live["mergeable"]:
            raise ValueError("fixture final merge does not match expected open head")
        base = self.git.resolve(str(pull["base_branch"]))
        result = subprocess.run(
            ["git", "merge-tree", "--write-tree", base, expected_head_sha],
            cwd=self.git.root,
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            raise ValueError(result.stderr.strip() or "fixture final merge conflicts")
        tree = result.stdout.splitlines()[0].strip()
        merged = subprocess.run(
            [
                "git", "commit-tree", tree, "-p", base, "-p", expected_head_sha,
                "-m", f"merge: Delivery Run {pull['branch']}",
            ],
            cwd=self.git.root,
            text=True,
            capture_output=True,
            check=False,
        )
        if merged.returncode != 0:
            raise ValueError(merged.stderr.strip() or "fixture final merge failed")
        integrated = merged.stdout.strip()
        subprocess.run(
            ["git", "update-ref", f"refs/heads/{pull['base_branch']}", integrated, base],
            cwd=self.git.root,
            check=True,
        )
        pull.update({"state": "MERGED", "integrated_sha": integrated})
        readback_identity_error = self._delivery().get(
            "normal_merge_readback_identity_error"
        )
        if readback_identity_error is not None:
            if not isinstance(readback_identity_error, dict) or not all(
                key in {"head_ref", "head_repository", "base_repository"}
                and isinstance(value, str)
                for key, value in readback_identity_error.items()
            ):
                raise ValueError(
                    "normal_merge_readback_identity_error must contain PR identity strings"
                )
            pull.update(readback_identity_error)
        self._save()
        self._crash_once("normal_merge")
        return integrated

    def close_parent_issue(
        self,
        *,
        parent_number: int,
        run_id: str,
        pr_number: int,
        integrated_sha: str,
        delivery_type: str,
    ) -> None:
        parent = _mutable_mapping(self.data, "parent")
        if _integer(parent, "number") != parent_number:
            raise ValueError("fixture parent is missing")
        mutations = _mutable_list(self._delivery(), "mutations")
        marker = {
            "parent_number": parent_number,
            "run_id": run_id,
            "pr_number": pr_number,
            "integrated_sha": integrated_sha,
            "delivery_type": delivery_type,
        }
        if not any(
            isinstance(item, dict)
            and item.get("action") == "parent_completion_comment"
            and item.get("parent_number") == parent_number
            for item in mutations
        ):
            mutations.append({"action": "parent_completion_comment", **marker})
        closed = _mutable_list(self._delivery(), "closed_issues")
        if parent_number not in closed:
            closed.append(parent_number)
            mutations.append({"action": "close_parent_issue", **marker})
        parent["state"] = "CLOSED"
        delivery = self._delivery()
        crash_flag = next(
            (
                key
                for key in (
                    "crash_after_parent_close_once",
                    "crash_after_close_parent_issue_once",
                )
                if delivery.get(key)
            ),
            None,
        )
        crash_after_close = crash_flag is not None
        if crash_flag is not None:
            delivery[crash_flag] = False
        self._save()
        if crash_after_close:
            raise OSError("simulated lost response after Parent Issue close")

    def abandon_run_pr(self, pr_number: int) -> None:
        pull = self._pull(pr_number)
        if pull.get("state") == "OPEN":
            pull["state"] = "CLOSED"
            _mutable_list(self._delivery(), "mutations").append(
                {"action": "close_final_run_pr", "pr_number": pr_number}
            )
            self._save()
            self._crash_once("abandon_run_pr")

    def abandon_change_pr(self, pr_number: int) -> bool:
        pull = self._pull(pr_number)
        self._inject_external_close_before_abandon("change", pull)
        if pull.get("state") == "OPEN":
            pull["state"] = "CLOSED"
            pull["closed_by"] = self._publisher_login()
            _mutable_list(self._delivery(), "mutations").append(
                {"action": "close_change_pr", "pr_number": pr_number}
            )
            self._save()
            self._crash_once("abandon_change_pr")
            return True
        return False

    def publish_branch(
        self,
        branch: str,
        head_sha: str,
        *,
        expected_remote_sha: str,
    ) -> None:
        published = _mutable_mapping(self._delivery(), "published_branches")
        if published.get(branch) == head_sha:
            return
        if published.get(branch) != expected_remote_sha:
            raise ValueError("fixture remote ticket branch drifted")
        published[branch] = head_sha
        for pull in _mutable_list(self._delivery(), "pull_requests"):
            if (
                isinstance(pull, dict)
                and pull.get("branch") == branch
                and pull.get("state") == "OPEN"
            ):
                pull["head_sha"] = head_sha
        self._save()
        self._inject_revision_drift("publish_branch")
        self._crash_once("publish_branch")

    def verify_ticket_pr_before_publish(
        self,
        *,
        branch: str,
        base_branch: str,
        expected_head_sha: str,
        expected_base_sha: str,
    ) -> None:
        matching = [
            pull
            for pull in _mutable_list(self._delivery(), "pull_requests")
            if (
                isinstance(pull, dict)
                and pull.get("branch") == branch
                and pull.get("state") == "OPEN"
            )
        ]
        if len(matching) > 1:
            raise ValueError("fixture contains duplicate Ticket PRs")
        if matching:
            pull = matching[0]
            if (
                pull.get("head_sha") != expected_head_sha
                or pull.get("base_sha") != expected_base_sha
                or pull.get("base_branch") != base_branch
                or pull.get("head_repository", self.data["repository"])
                != self.data["repository"]
                or pull.get("base_repository", self.data["repository"])
                != self.data["repository"]
            ):
                raise ValueError("fixture Ticket PR has a foreign identity")

    def verify_change_pr_before_publish(
        self,
        *,
        branch: str,
        base_branch: str,
        expected_head_sha: str,
        expected_base_sha: str,
    ) -> None:
        self.verify_ticket_pr_before_publish(
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
        pulls = _mutable_list(self._delivery(), "pull_requests")
        matching = [
            pull for pull in pulls
            if isinstance(pull, dict) and pull.get("branch") == branch
            and pull.get("state") == "OPEN"
        ]
        if len(matching) > 1:
            raise ValueError("fixture contains duplicate Change PRs")
        if matching:
            pull = matching[0]
            if (
                pull.get("head_sha") != expected_head_sha
                or pull.get("base_sha") != expected_base_sha
                or pull.get("base_branch") != base_branch
                or pull.get("head_repository", self.data["repository"])
                != self.data["repository"]
                or pull.get("base_repository", self.data["repository"])
                != self.data["repository"]
            ):
                raise ValueError("fixture Change PR has a foreign identity")
        else:
            pull = {
                "number": len(pulls) + 1,
                "branch": branch,
                "base_branch": base_branch,
                "state": "OPEN",
                "head_sha": expected_head_sha,
                "base_sha": expected_base_sha,
                "scope": (
                    "run_repair"
                    if branch.startswith("agent-run-repair/")
                    else "parent_only" if branch.endswith("/parent") else "ticket"
                ),
            }
            pulls.append(pull)
        pull.update({"title": title, "body": body})
        self._save()
        self._inject_revision_drift("ensure_ticket_pr")
        self._crash_once("ensure_change_pr")
        self._crash_once("ensure_ticket_pr")
        return int(pull["number"])

    def ensure_ticket_pr(
        self,
        *,
        branch: str,
        base_branch: str,
        title: str,
        body: str,
        primary_ticket: int,
        expected_head_sha: str,
        expected_base_sha: str,
    ) -> int:
        pulls = _mutable_list(self._delivery(), "pull_requests")
        matching = [
            pr
            for pr in pulls
            if isinstance(pr, dict)
            and pr.get("branch") == branch
            and pr.get("state") == "OPEN"
        ]
        if len(matching) > 1:
            raise ValueError("fixture contains duplicate Ticket PRs")
        if matching:
            pull = matching[0]
            if (
                pull.get("head_sha") != expected_head_sha
                or pull.get("base_sha") != expected_base_sha
                or pull.get("primary_ticket") != primary_ticket
                or pull.get("base_branch") != base_branch
            ):
                raise ValueError("fixture Ticket PR has a foreign identity")
        else:
            pull = {
                "number": len(pulls) + 1,
                "branch": branch,
                "base_branch": base_branch,
                "primary_ticket": primary_ticket,
                "state": "OPEN",
                "head_sha": expected_head_sha,
                "base_sha": expected_base_sha,
            }
            pulls.append(pull)
        pull.update({"title": title, "body": body})
        self._save()
        self._inject_revision_drift("ensure_ticket_pr")
        self._crash_once("ensure_ticket_pr")
        return int(pull["number"])

    def publication_context(self, pr_number: int) -> dict[str, object]:
        pull = self._pull(pr_number)
        title = pull.get("title")
        if not isinstance(title, str) or not title:
            raise ValueError("fixture Ticket PR title is missing")
        return {
            "number": pr_number,
            "url": f"https://github.com/{_string(self.data, 'repository')}/pull/{pr_number}",
            "title": title,
        }

    def required_checks(self, pr_number: int) -> str:
        return self._next_required_checks_result(pr_number)

    def _next_required_checks_result(self, pr_number: int) -> str:
        delivery = self._delivery()
        pull = self._pull(pr_number)
        run_failures = delivery.get("run_required_checks_read_failures", [])
        if "primary_ticket" not in pull and isinstance(run_failures, list) and run_failures:
            configured = run_failures.pop(0)
            self._save()
            if not isinstance(configured, dict):
                raise ValueError(
                    "fixture run_required_checks_read_failures must contain objects"
                )
            raise GitHubReadError(
                str(configured.get("code", "github_read_failed")),
                str(configured.get("message", "Final Run Required Checks read failed")),
            )
        sequence = delivery.get("required_checks", ["none"])
        if not isinstance(sequence, list) or not all(
            value in {
                "none",
                "pass",
                "pending",
                "fail",
                "unknown",
                "skipping",
                "neutral",
            }
            for value in sequence
        ):
            raise ValueError("fixture required_checks is invalid")
        position = int(delivery.get("check_position", 0))
        value = str(sequence[min(position, len(sequence) - 1)])
        delivery["check_position"] = position + 1
        self._save()
        self._inject_revision_drift("required_checks")
        if value in {"skipping", "neutral"}:
            return "pass"
        return value

    def required_checks_snapshot(
        self, pr_number: int, *, expected_head_sha: str
    ) -> dict[str, Any]:
        actual_head_sha = str(self.live_pull_request(pr_number)["head_sha"])
        if actual_head_sha != expected_head_sha:
            raise GitHubReadError(
                "change_pr_head_drift",
                "Required Checks snapshot does not match the expected PR head",
            )
        result = self._next_required_checks_result(pr_number)
        actual_head_sha = str(self.live_pull_request(pr_number)["head_sha"])
        if actual_head_sha != expected_head_sha:
            raise GitHubReadError(
                "change_pr_head_drift",
                "Required Checks snapshot does not match the expected PR head",
            )
        configured = self._delivery().get("required_check_evidence")
        configured_checks = (
            configured.get("checks", []) if isinstance(configured, dict) else []
        )
        if result == "none":
            checks: list[object] = []
        elif (
            result == "fail"
            and isinstance(configured_checks, list)
            and configured_checks
        ):
            if all(isinstance(check, dict) for check in configured_checks):
                checks = [
                    _fixture_check_for_head(check, actual_head_sha)
                    for check in configured_checks
                ]
            else:
                checks = deepcopy(configured_checks)
        else:
            checks = []
            if isinstance(configured_checks, list):
                checks = [
                    _fixture_observation_check(result, check)
                    for check in configured_checks
                    if isinstance(check, dict)
                ]
            if not checks:
                checks = [_fixture_observation_check(result)]
        return {
            "pr_number": pr_number,
            "head_sha": expected_head_sha,
            "result": result,
            "checks": checks,
        }

    def required_check_evidence(
        self, pr_number: int, *, expected_head_sha: str | None = None
    ) -> dict[str, Any]:
        delivery = self._delivery()
        configured = delivery.get("required_check_evidence")
        if expected_head_sha is None:
            if isinstance(configured, dict):
                return dict(configured)
            return {
                "pr_number": pr_number,
                "checks": [
                    {
                        **_fixture_required_check_identity("fail"),
                        "description": "The fixture required check failed.",
                    }
                ],
            }
        pull = self._pull(pr_number)
        failures = delivery.get("required_check_evidence_failures")
        if isinstance(failures, list) and failures:
            failure = failures[0]
            if not isinstance(failure, dict):
                raise ValueError(
                    "fixture required_check_evidence_failures must contain objects"
                )
            scope = failure.get("scope")
            if scope is None or scope == pull.get("scope"):
                remaining = failure.get("remaining_successes", 0)
                if type(remaining) is not int or remaining < 0:
                    raise ValueError(
                        "fixture evidence failure remaining_successes is invalid"
                    )
                if remaining > 0:
                    failure["remaining_successes"] = remaining - 1
                    self._save()
                else:
                    failures.pop(0)
                    self._save()
                    error_type = failure.get("type", "github")
                    message = str(failure.get("message", "evidence read failed"))
                    if error_type == "github":
                        raise GitHubReadError(
                            str(failure.get("code", "github_timeout")), message
                        )
                    if error_type == "os":
                        raise OSError(message)
                    if error_type == "timeout":
                        raise TimeoutError(message)
                    raise ValueError("fixture evidence failure type is invalid")
        actual_head_sha = str(self.live_pull_request(pr_number)["head_sha"])
        if isinstance(configured, dict):
            evidence = dict(configured)
        else:
            evidence = {
                "pr_number": pr_number,
                "checks": [
                    {
                        **_fixture_required_check_identity("fail"),
                        "state": "FAILURE",
                        "description": "The fixture required check failed.",
                        "job": {
                            "id": 1,
                            "head_sha": actual_head_sha,
                            "name": "fixture-required-check",
                            "workflow_name": "fixture-ci",
                            "status": "completed",
                            "conclusion": "failure",
                            "steps": [
                                {
                                    "name": "Run tests",
                                    "status": "completed",
                                    "conclusion": "failure",
                                    "number": 1,
                                }
                            ],
                        },
                    }
                ],
            }
        checks = evidence.get("checks")
        if isinstance(checks, list) and all(
            isinstance(check, dict) for check in checks
        ):
            normalized_checks = [
                _fixture_check_for_head(check, actual_head_sha) for check in checks
            ]
            evidence["checks"] = annotate_configured_code_failures(
                normalized_checks,
                self.path.parent,
                expected_head_sha=expected_head_sha,
            )
        return evidence

    def live_pull_request(self, pr_number: int) -> dict[str, Any]:
        pull = self._pull(pr_number)
        reported_state = pull.get("state")
        if reported_state == "MERGED":
            configured_overrides = self._delivery().get(
                "merged_live_pull_request_state_overrides"
            )
            if isinstance(configured_overrides, dict):
                scoped_overrides = configured_overrides.get(str(pull.get("scope")))
                if isinstance(scoped_overrides, list) and scoped_overrides:
                    reported_state = scoped_overrides.pop(0)
                    if reported_state not in {"OPEN", "MERGED"}:
                        raise GitHubReadError(
                            "invalid_fixture",
                            "merged live PR state override must be OPEN or MERGED",
                        )
                    self._save()
        failure_key = (
            "merged_live_pull_request_failures"
            if pull.get("state") == "MERGED"
            else "open_live_pull_request_failures"
        )
        failures = self._delivery().get(failure_key)
        failure_position = None
        if isinstance(failures, list):
            failure_position = next(
                (
                    position
                    for position, failure in enumerate(failures)
                    if not isinstance(failure, dict)
                    or failure.get("scope", pull.get("scope")) == pull.get("scope")
                ),
                None,
            )
        if isinstance(failures, list) and failure_position is not None:
            configured = failures.pop(failure_position)
            self._save()
            if not isinstance(configured, dict):
                raise GitHubReadError(
                    "invalid_fixture",
                    f"{failure_key} must contain objects",
                )
            message = str(configured.get("message", "GitHub read failed"))
            error_type = configured.get("type", "github")
            if error_type == "github":
                raise GitHubReadError(
                    str(configured.get("code", "github_read_failed")), message
                )
            if error_type == "os":
                raise OSError(message)
            if error_type == "timeout":
                raise TimeoutError(message)
            raise ValueError(f"{failure_key} failure type is invalid")
        published = _mutable_mapping(self._delivery(), "published_branches")
        live_head = published.get(str(pull["branch"])) or pull.get("head_sha")
        override = self._delivery().get("live_head_override")
        result = {
            "head_branch": pull.get("head_ref", pull["branch"]),
            "head_sha": override if isinstance(override, str) else live_head,
            "head_repository": pull.get("head_repository", self.data["repository"]),
            "base_branch": pull["base_branch"],
            "base_sha": self.git.resolve(str(pull["base_branch"])),
            "base_repository": pull.get("base_repository", self.data["repository"]),
            "mergeable": bool(self._delivery().get("mergeable", True)),
            "state": reported_state,
            "integrated_sha": pull.get("integrated_sha"),
        }
        integrated = pull.get("integrated_sha")
        if isinstance(integrated, str) and isinstance(live_head, str):
            result.update(
                {
                    "head_tree": self.git.resolve(f"{live_head}^{{tree}}"),
                    "integrated_tree": self.git.resolve(
                        f"{integrated}^{{tree}}"
                    ),
                    "integrated_message": _commit_subject(
                        self.git.root, integrated
                    ),
                    "integrated_parents": self.git.commit_parents(integrated),
                }
            )
        return result

    def _require_final_run_pr_identity(
        self,
        pull: dict[str, Any],
        *,
        branch: str,
        expected_head_sha: str,
        expected_base_branch: str,
        expected_base_sha: str,
    ) -> None:
        live = self.live_pull_request(int(pull["number"]))
        if (
            live.get("state") == "OPEN"
            and live.get("head_branch") == branch
            and live.get("head_sha") == expected_head_sha
            and live.get("head_repository") == self.data["repository"]
            and live.get("base_branch") == expected_base_branch
            and live.get("base_sha") == expected_base_sha
            and live.get("base_repository") == self.data["repository"]
        ):
            return
        raise GitHubReadError(
            "foreign_run_pr", "fixture Final Run PR has a foreign identity"
        )

    def run_pr_narrative_matches(
        self, pr_number: int, *, title: str, body: str
    ) -> bool:
        pull = self._pull(pr_number)
        return pull.get("title") == title and pull.get("body") == body

    def record_acceptance(
        self, pr_number: int, record: dict[str, Any]
    ) -> None:
        records = _mutable_list(self._delivery(), "acceptance_records")
        replacement = {"pr_number": pr_number, **record}
        for position, existing in enumerate(records):
            if isinstance(existing, dict) and existing.get("pr_number") == pr_number:
                records[position] = replacement
                break
        else:
            records.append(replacement)
        self._save()
        self._crash_once("record_acceptance")

    def record_agent_run_status(
        self, pr_number: int, status: dict[str, Any]
    ) -> None:
        statuses = _mutable_list(self._delivery(), "agent_run_status")
        replacement = {"pr_number": pr_number, **status}
        for position, existing in enumerate(statuses):
            if isinstance(existing, dict) and existing.get("pr_number") == pr_number:
                statuses[position] = replacement
                break
        else:
            statuses.append(replacement)
        self._save()
        if status.get("retirement") == "closed":
            self._inject_external_reopen_after_receipt(pr_number)
        self._crash_once("record_agent_run_status")

    def has_supersession_close_receipt(
        self, pr_number: int, generation: object, close_nonce: object
    ) -> bool:
        pull = self._pull(pr_number)
        return (
            pull.get("state") == "CLOSED"
            and pull.get("closed_by") == self._publisher_login()
            and self.has_supersession_close_intent(
                pr_number, generation, close_nonce
            )
        )

    def has_supersession_close_record(
        self, pr_number: int, generation: object, close_nonce: object
    ) -> bool:
        return any(
            isinstance(status, dict)
            and status.get("pr_number") == pr_number
            and status.get("scope") == "superseded_generation"
            and status.get("generation") == generation
            and status.get("close_nonce") == close_nonce
            and status.get("retirement") == "closed"
            for status in _mutable_list(self._delivery(), "agent_run_status")
        )

    def has_supersession_close_intent(
        self, pr_number: int, generation: object, close_nonce: object
    ) -> bool:
        return any(
            isinstance(status, dict)
            and status.get("pr_number") == pr_number
            and status.get("scope") == "superseded_generation"
            and status.get("generation") == generation
            and status.get("close_nonce") == close_nonce
            and status.get("retirement") in {"closing", "closed"}
            for status in _mutable_list(self._delivery(), "agent_run_status")
        )

    def squash_merge(
        self,
        *,
        pr_number: int,
        expected_head_sha: str,
        run_branch: str,
        commit_message: str,
    ) -> str:
        pull = self._pull(pr_number)
        if pull.get("state") == "MERGED":
            return str(pull["integrated_sha"])
        live = self.live_pull_request(pr_number)
        if live["head_sha"] != expected_head_sha:
            raise ValueError("fixture merge head does not match expected head")
        outcomes = self._delivery().get("squash_merge_outcomes", [])
        if not isinstance(outcomes, list) or not all(
            outcome in {"unknown", "merge"} for outcome in outcomes
        ):
            raise ValueError("fixture squash_merge_outcomes is invalid")
        if outcomes:
            outcome = outcomes.pop(0)
            self._save()
            if outcome == "unknown":
                raise MergeOutcomeUnknownError("fixture squash merge response is unknown")
        tree = self.git.resolve(f"{expected_head_sha}^{{tree}}")
        parent = self.git.resolve(run_branch)
        result = subprocess.run(
            [
                "git",
                "commit-tree",
                tree,
                "-p",
                parent,
                "-m",
                commit_message,
            ],
            cwd=self.git.root,
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            raise ValueError(
                result.stderr.strip() or "fixture merge failed"
            )
        integrated = result.stdout.strip()
        subprocess.run(
            [
                "git",
                "update-ref",
                f"refs/heads/{run_branch}",
                integrated,
                parent,
            ],
            cwd=self.git.root,
            check=True,
        )
        pull.update(
            {"state": "MERGED", "integrated_sha": integrated}
        )
        self._save()
        self._crash_once("squash_merge")
        return integrated

    def sync_run_branch(
        self, *, run_branch: str, integrated_sha: str
    ) -> None:
        published = _mutable_mapping(self._delivery(), "published_branches")
        published[run_branch] = integrated_sha
        if self.git.resolve(run_branch) == integrated_sha:
            self._save()
            self._crash_once("sync_run_branch")
            return
        subprocess.run(
            ["git", "update-ref", f"refs/heads/{run_branch}", integrated_sha],
            cwd=self.git.root,
            check=True,
        )
        self._save()
        self._crash_once("sync_run_branch")

    def prepare_primary_ticket_close(
        self,
        *,
        ticket_number: int,
        run_id: str,
        pr_number: int,
        integrated_sha: str,
    ) -> dict[str, Any] | None:
        mutations = _mutable_list(self._delivery(), "mutations")
        marker = {
            "ticket_number": ticket_number,
            "run_id": run_id,
            "pr_number": pr_number,
            "integrated_sha": integrated_sha,
        }
        if not any(
            isinstance(item, dict)
            and item.get("action") == "completion_comment"
            and item.get("ticket_number") == ticket_number
            for item in mutations
        ):
            mutations.append({"action": "completion_comment", **marker})
        self._save()
        if self._delivery().pop("ticket_close_intent_missing_once", False):
            self._save()
            return None
        return {
            "actor": "fixture-publisher",
            "event_id": None,
            "intent_created_at": f"{run_id}:ticket-{ticket_number}:intent",
            "baseline_event_id": 0,
            "intent_binding": f"pr-{pr_number}:sha-{integrated_sha}",
        }

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
        mutations = _mutable_list(self._delivery(), "mutations")
        marker = {
            "ticket_number": ticket_number,
            "run_id": run_id,
            "pr_number": pr_number,
            "integrated_sha": integrated_sha,
        }
        if not any(
            isinstance(item, dict)
            and item.get("action") == "completion_comment"
            and item.get("ticket_number") == ticket_number
            for item in mutations
        ):
            mutations.append({"action": "completion_comment", **marker})
        closed = _mutable_list(self._delivery(), "closed_issues")
        publisher_closed = ticket_number in closed
        raw_ownerships = self._delivery().setdefault(
            "ticket_close_ownership", {}
        )
        if not isinstance(raw_ownerships, dict):
            raise ValueError("delivery.ticket_close_ownership must be an object")
        ownerships = raw_ownerships
        raw_issues = _mutable_mapping(self.data, "issues")
        issue = raw_issues.get(str(ticket_number))
        if self._delivery().pop("external_close_before_primary_ticket", False):
            if isinstance(issue, dict):
                issue["state"] = "CLOSED"
        if (
            not publisher_closed
            and isinstance(issue, dict)
            and issue.get("state") == "CLOSED"
        ):
            self._save()
            raise GitHubReadError(
                "ticket_close_external_conflict",
                "Ticket was closed outside the Publisher close intent",
            )
        if (
            not publisher_closed
            and isinstance(issue, dict)
            and issue.get("state") != "CLOSED"
        ):
            crash_before_dispatch = bool(
                self._delivery().pop("crash_before_close_dispatch_once", False)
            )
            if crash_before_dispatch:
                self._save()
                raise OSError("simulated crash before Primary Ticket close dispatch")
            if before_dispatch is not None:
                before_dispatch()
            crash_after_boundary = bool(
                self._delivery().pop("crash_after_close_dispatch_boundary_once", False)
            )
            if crash_after_boundary:
                self._save()
                raise OSError("simulated crash after durable close dispatch boundary")
            closed.append(ticket_number)
            mutations.append({"action": "close_issue", **marker})
            issue["state"] = "CLOSED"
            publisher_closed = True
        if publisher_closed and str(ticket_number) not in ownerships:
            ownerships[str(ticket_number)] = {
                "event_id": f"{run_id}:ticket-{ticket_number}:closed",
                "actor": "fixture-publisher",
                "intent_binding": (
                    close_intent.get("intent_binding")
                    if isinstance(close_intent, dict)
                    else None
                ),
            }
        for raw_issue in raw_issues.values():
            if not isinstance(raw_issue, dict):
                continue
            blockers = raw_issue.get("blocked_by")
            if not isinstance(blockers, list):
                continue
            for blocker in blockers:
                if (
                    isinstance(blocker, dict)
                    and blocker.get("number") == ticket_number
                ):
                    blocker["state"] = "CLOSED"
        crash_after_close = bool(
            self._delivery().get("crash_after_close_once")
        )
        if crash_after_close:
            self._delivery()["crash_after_close_once"] = False
        self._save()
        if crash_after_close:
            raise OSError(
                "simulated lost response after Primary Ticket close"
            )
        return self.ticket_close_ownership(
            ticket_number=ticket_number,
            run_id=run_id,
            recorded_ownership=close_intent,
        )

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
        del run_id
        configured_failures = self._delivery().get(
            "ticket_close_ownership_read_failures"
        )
        if isinstance(configured_failures, list) and configured_failures:
            configured_error = configured_failures.pop(0)
            self._save()
            if not isinstance(configured_error, dict):
                raise GitHubReadError(
                    "invalid_fixture",
                    "ticket_close_ownership_read_failures must contain objects",
                )
            raise GitHubReadError(
                str(configured_error.get("code", "github_read_failed")),
                str(configured_error.get("message", "Ticket close ownership read failed")),
            )
        issue = _mutable_mapping(self.data, "issues").get(str(ticket_number))
        raw_ownerships = self._delivery().get("ticket_close_ownership", {})
        if not isinstance(raw_ownerships, dict):
            raise ValueError("delivery.ticket_close_ownership must be an object")
        current = raw_ownerships.get(str(ticket_number))
        expected = recorded_ownership if recorded_ownership is not None else current
        lag_reads = self._delivery().get("ticket_close_event_lag_reads", 0)
        if (
            isinstance(lag_reads, int)
            and not isinstance(lag_reads, bool)
            and lag_reads > 0
            and isinstance(issue, dict)
            and issue.get("state") == "CLOSED"
        ):
            self._delivery()["ticket_close_event_lag_reads"] = lag_reads - 1
            self._save()
            raise GitHubReadError(
                "ticket_close_ownership_pending",
                "fixture has not exposed the Ticket close event yet",
            )
        if (
            isinstance(issue, dict)
            and issue.get("state") == "OPEN"
            and isinstance(expected, dict)
            and expected.get("event_id") is None
            and expected.get("dispatch_attempted") is True
            and not isinstance(current, dict)
        ):
            external = self._delivery().get("external_ticket_transitions", [])
            if isinstance(external, list) and ticket_number in external:
                return None
            raise GitHubReadError(
                "ticket_close_dispatch_unobserved",
                "fixture has not observed the attempted Ticket close",
            )
        owned = (
            isinstance(issue, dict)
            and issue.get("state") == "CLOSED"
            and ticket_number
            in _mutable_list(self._delivery(), "closed_issues")
            and isinstance(current, dict)
            and isinstance(expected, dict)
            and (
                current.get("event_id") == expected.get("event_id")
                or (
                    expected.get("event_id") is None
                    and current.get("actor") == expected.get("actor")
                    and current.get("intent_binding")
                    == expected.get("intent_binding")
                )
            )
        )
        return dict(current) if owned and isinstance(current, dict) else None

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
        mutations = _mutable_list(self._delivery(), "mutations")
        marker = {
            "ticket_number": ticket_number,
            "run_id": run_id,
            "pr_number": pr_number,
            "integrated_sha": integrated_sha,
        }
        already_recorded = any(
            isinstance(item, dict)
            and item.get("action") == "abandonment_recovery_comment"
            and item.get("ticket_number") == ticket_number
            and item.get("run_id") == run_id
            for item in mutations
        )
        raw_issues = _mutable_mapping(self.data, "issues")
        issue = raw_issues.get(str(ticket_number))
        if not isinstance(issue, dict) or issue.get("state") != "CLOSED":
            return (
                already_recorded
                and ticket_number
                not in _mutable_list(self._delivery(), "closed_issues")
            )
        current_ownership = self.ticket_close_ownership(
            ticket_number=ticket_number,
            run_id=run_id,
            recorded_ownership=expected_ownership,
        )
        if current_ownership is None:
            self._save()
            return False
        issue["state"] = "OPEN"
        closed = _mutable_list(self._delivery(), "closed_issues")
        if ticket_number in closed:
            closed.remove(ticket_number)
        mutations.append(
            {"action": "abandonment_reopen_issue", **marker}
        )
        for raw_issue in raw_issues.values():
            if not isinstance(raw_issue, dict):
                continue
            blockers = raw_issue.get("blocked_by")
            if not isinstance(blockers, list):
                continue
            for blocker in blockers:
                if (
                    isinstance(blocker, dict)
                    and blocker.get("number") == ticket_number
                ):
                    blocker["state"] = "OPEN"
        if not already_recorded:
            mutations.append(
                {"action": "abandonment_recovery_comment", **marker}
            )
        self._save()
        self._crash_once("recover_abandoned_ticket")
        return True

    def mark_ready_for_human(self, ticket_number: int) -> None:
        raw_issues = _mutable_mapping(self.data, "issues")
        issue = raw_issues.get(str(ticket_number))
        if not isinstance(issue, dict):
            raise ValueError("fixture ticket is missing")
        labels = issue.get("labels")
        if not isinstance(labels, list):
            raise ValueError("fixture labels must be a list")
        issue["labels"] = [
            label for label in labels if label != "ready-for-agent"
        ]
        if "ready-for-human" not in issue["labels"]:
            issue["labels"].append("ready-for-human")
        self._save()

    def current_effective_revision(
        self,
        *,
        parent_number: int,
        ticket_number: int,
        expected_revision: str,
    ) -> str:
        reader = FixtureGitHubReader(self.path)
        graph = reader.delivery_graph(parent_number)
        self.data = reader.data
        return effective_revision_from_graph(graph, ticket_number)

    def _delivery(self) -> dict[str, Any]:
        return _mutable_mapping(self.data, "delivery")

    def _pull(self, pr_number: int) -> dict[str, Any]:
        for pull in _mutable_list(self._delivery(), "pull_requests"):
            if isinstance(pull, dict) and pull.get("number") == pr_number:
                return pull
        raise ValueError(f"fixture PR #{pr_number} is missing")

    def _save(self) -> None:
        # The foreground-supervision fixture clock is advanced by the CLI,
        # while this publisher intentionally retains one in-memory fixture
        # view for a lifecycle command.  Preserve the newer clock value so a
        # later publisher mutation cannot reset a persisted wait deadline.
        live: object = json.loads(self.path.read_text(encoding="utf-8"))
        if isinstance(live, dict) and isinstance(
            live.get("supervision_clock"), (int, float)
        ):
            self.data["supervision_clock"] = live["supervision_clock"]
        descriptor, name = tempfile.mkstemp(
            dir=self.path.parent,
            prefix=f".{self.path.name}.",
            suffix=".tmp",
            text=True,
        )
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                json.dump(self.data, file, ensure_ascii=False, indent=2, sort_keys=True)
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, self.path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    def _crash_once(self, action: str) -> None:
        key = f"crash_after_{action}_once"
        if not bool(self._delivery().get(key)):
            return
        self._delivery()[key] = False
        self._save()
        raise OSError(f"simulated lost response after {action}")

    def _inject_external_close_before_abandon(
        self, subject: str, pull: dict[str, Any]
    ) -> None:
        key = f"external_close_before_abandon_{subject}_pr_once"
        if not bool(self._delivery().get(key)):
            return
        self._delivery()[key] = False
        pull["state"] = "CLOSED"
        pull["closed_by"] = "external"
        self._save()

    def _inject_external_reopen_after_receipt(self, pr_number: int) -> None:
        key = "external_reopen_after_supersession_receipt_once"
        if not bool(self._delivery().get(key)):
            return
        self._delivery()[key] = False
        pull = self._pull(pr_number)
        pull["state"] = "OPEN"
        pull["closed_by"] = "external"
        self._save()

    def _publisher_login(self) -> str:
        configured = self._delivery().get("publisher_login", "fixture-publisher")
        if not isinstance(configured, str) or not configured:
            raise ValueError("fixture publisher_login must be a non-empty string")
        return configured

    def _inject_revision_drift(self, action: str) -> None:
        configured = self._delivery().get("drift_after")
        if (
            not isinstance(configured, dict)
            or configured.get("action") != action
            or configured.get("injected") is True
        ):
            return
        parent = _mutable_mapping(self.data, "parent")
        sub_issues = parent.get("sub_issues")
        if not isinstance(sub_issues, list) or not sub_issues:
            raise ValueError("fixture parent.sub_issues must be a non-empty list")
        raw_number = configured.get("ticket_number", sub_issues[0])
        if not isinstance(raw_number, int):
            raise ValueError("fixture drift ticket_number must be an integer")
        kind = configured.get("kind")
        if kind == "ticket_content":
            issue = _mutable_mapping(
                _mutable_mapping(self.data, "issues"), str(raw_number)
            )
            issue["body"] = f"{_string(issue, 'body')}\nChanged during publish."
        elif kind == "ticket_removal":
            parent["sub_issues"] = [
                number for number in sub_issues if number != raw_number
            ]
        else:
            raise ValueError("fixture drift kind is invalid")
        configured["injected"] = True
        self._save()


def _parse_issue(data: dict[str, Any]) -> Issue:
    raw_labels = data.get("labels")
    raw_blockers = data.get("blocked_by")
    if not isinstance(raw_labels, list) or not all(
        isinstance(label, str) for label in raw_labels
    ):
        raise GitHubReadError("invalid_fixture", "issue.labels must contain strings")
    if not isinstance(raw_blockers, list):
        raise GitHubReadError("invalid_fixture", "issue.blocked_by must be a list")
    return Issue(
        number=_integer(data, "number"),
        title=_string(data, "title"),
        body=_string(data, "body"),
        state=_string(data, "state"),
        labels=frozenset(raw_labels),
        blocked_by=tuple(
            Blocker(
                number=_integer(_as_mapping(blocker), "number"),
                state=_string(_as_mapping(blocker), "state"),
            )
            for blocker in raw_blockers
        ),
    )


def _mapping(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise GitHubReadError("invalid_fixture", f"{key} must be an object")
    return value


def _as_mapping(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise GitHubReadError("invalid_fixture", "blocker must be an object")
    return value


def _string(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        raise GitHubReadError("invalid_fixture", f"{key} must be a string")
    return value


def _integer(data: dict[str, Any], key: str) -> int:
    value = data.get(key)
    if not isinstance(value, int):
        raise GitHubReadError("invalid_fixture", f"{key} must be an integer")
    return value


def _mutable_mapping(
    data: dict[str, Any], key: str
) -> dict[str, Any]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be an object")
    return value


def _mutable_list(data: dict[str, Any], key: str) -> list[Any]:
    value = data.get(key)
    if not isinstance(value, list):
        raise ValueError(f"{key} must be a list")
    return value


def _commit_subject(repository: Path, sha: str) -> str:
    result = subprocess.run(
        ["git", "log", "-1", "--format=%s", sha],
        cwd=repository,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise ValueError(result.stderr.strip() or "fixture commit is missing")
    return result.stdout.strip()
