from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from agent_run.git import GitRepository
from agent_run.github import GitHubReadError
from agent_run.models import Blocker, DeliveryGraph, Issue, ParentIssue, Repository
from agent_run.revisions import effective_revision_from_graph


class FixtureGitHubReader:
    """供黑盒测试使用的确定性 GitHub 只读适配器。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.data = self._load()

    def repository(self) -> Repository:
        self.data = self._load()
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
        self.data = self._load()
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
        delivery.setdefault("published_branches", {})
        delivery.setdefault("pull_requests", [])
        delivery.setdefault("closed_issues", [])
        delivery.setdefault("acceptance_records", [])
        delivery.setdefault("agent_run_status", [])
        delivery.setdefault("run_publication_records", [])
        delivery.setdefault("mutations", [])
        delivery.setdefault("check_position", 0)

    def ensure_parent_branch(
        self, *, parent_number: int, branch: str, base_branch: str
    ) -> None:
        parent = _mutable_mapping(self.data, "parent")
        if _integer(parent, "number") != parent_number:
            raise ValueError("fixture parent is missing")
        linked = _mutable_mapping(self._delivery(), "linked_branches")
        linked["parent"] = branch
        published = _mutable_mapping(self._delivery(), "published_branches")
        published.setdefault(branch, self.git.resolve(base_branch))
        self._save()

    def ensure_ticket_branch(
        self,
        *,
        ticket_number: int,
        branch: str,
        base_branch: str,
    ) -> None:
        linked = _mutable_mapping(self._delivery(), "linked_branches")
        linked[str(ticket_number)] = branch
        published = _mutable_mapping(self._delivery(), "published_branches")
        published.setdefault(branch, self.git.resolve(base_branch))
        self._save()
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
        self, *, branch: str, base_branch: str, title: str, body: str
    ) -> int:
        _mutable_mapping(self._delivery(), "published_branches").setdefault(
            branch, self.git.resolve(branch)
        )
        pulls = _mutable_list(self._delivery(), "pull_requests")
        matching = [
            pr for pr in pulls if isinstance(pr, dict)
            and pr.get("branch") == branch and pr.get("base_branch") == base_branch
            and pr.get("scope") == "final_run"
        ]
        if len(matching) > 1:
            raise ValueError("fixture contains duplicate final Run PRs")
        if matching:
            pull = matching[0]
            if pull.get("state") != "OPEN":
                raise ValueError("fixture final Run PR is not open")
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
        return int(pull["number"])

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

    def abandon_parent_pr(self, pr_number: int) -> None:
        pull = self._pull(pr_number)
        if pull.get("state") == "OPEN":
            pull["state"] = "CLOSED"
            _mutable_list(self._delivery(), "mutations").append(
                {"action": "close_parent_pr", "pr_number": pr_number}
            )
            self._save()

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
        self._save()
        self._inject_revision_drift("publish_branch")
        self._crash_once("publish_branch")

    def ensure_ticket_pr(
        self,
        *,
        branch: str,
        base_branch: str,
        title: str,
        body: str,
        primary_ticket: int,
    ) -> int:
        pulls = _mutable_list(self._delivery(), "pull_requests")
        matching = [
            pr
            for pr in pulls
            if isinstance(pr, dict)
            and pr.get("branch") == branch
            and pr.get("base_branch") == base_branch
        ]
        if len(matching) > 1:
            raise ValueError("fixture contains duplicate Ticket PRs")
        if matching:
            pull = matching[0]
        else:
            pull = {
                "number": len(pulls) + 1,
                "branch": branch,
                "base_branch": base_branch,
                "primary_ticket": primary_ticket,
                "state": "OPEN",
            }
            pulls.append(pull)
        pull.update({"title": title, "body": body})
        self._save()
        self._inject_revision_drift("ensure_ticket_pr")
        self._crash_once("ensure_ticket_pr")
        return int(pull["number"])

    def required_checks(self, pr_number: int) -> str:
        delivery = self._delivery()
        sequence = delivery.get("required_checks", ["none"])
        if not isinstance(sequence, list) or not all(
            value in {"none", "pass", "pending", "fail"} for value in sequence
        ):
            raise ValueError("fixture required_checks is invalid")
        position = int(delivery.get("check_position", 0))
        value = str(sequence[min(position, len(sequence) - 1)])
        delivery["check_position"] = position + 1
        self._save()
        self._inject_revision_drift("required_checks")
        return value

    def required_check_evidence(self, pr_number: int) -> dict[str, Any]:
        configured = self._delivery().get("required_check_evidence")
        if isinstance(configured, dict):
            return dict(configured)
        return {
            "pr_number": pr_number,
            "checks": [
                {
                    "name": "fixture-required-check",
                    "workflow": "fixture-ci",
                    "bucket": "fail",
                    "description": "The fixture required check failed.",
                    "link": "https://example.invalid/checks/fixture",
                }
            ],
        }

    def live_pull_request(self, pr_number: int) -> dict[str, Any]:
        pull = self._pull(pr_number)
        published = _mutable_mapping(self._delivery(), "published_branches")
        live_head = published.get(str(pull["branch"]))
        override = self._delivery().get("live_head_override")
        result = {
            "head_sha": override if isinstance(override, str) else live_head,
            "base_branch": pull["base_branch"],
            "base_sha": self.git.resolve(str(pull["base_branch"])),
            "mergeable": bool(self._delivery().get("mergeable", True)),
            "state": pull.get("state"),
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
        self._crash_once("record_agent_run_status")

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

    def close_primary_ticket(
        self,
        *,
        ticket_number: int,
        run_id: str,
        pr_number: int,
        integrated_sha: str,
    ) -> None:
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
        if ticket_number not in closed:
            closed.append(ticket_number)
            mutations.append({"action": "close_issue", **marker})
        raw_issues = _mutable_mapping(self.data, "issues")
        issue = raw_issues.get(str(ticket_number))
        if isinstance(issue, dict):
            issue["state"] = "CLOSED"
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
