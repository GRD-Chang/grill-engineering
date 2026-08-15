from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_run.agents import (
    DevelopmentResult,
    HumanBlockerResult,
    PublicationResult,
    ReviewResult,
)
from agent_run.agent_invocation import (
    canonical_fingerprint,
    select_publication_thread,
)
from agent_run.change_delivery import ChangeDeliveryEngine
from agent_run.controller import Controller
from agent_run.delivery import TicketDeliveryEngine
from agent_run.git import GitError, GitRepository
from agent_run.github import GitHubReadError, MergeOutcomeUnknownError
from agent_run.github_fixture import FixtureGitHubReader
from agent_run.parent_delivery_loop import ParentDeliveryLoop
from agent_run.revisions import effective_revision
from agent_run.state import StateStore

from conftest import write_fixture


E2E_PASS_EVIDENCE = "操作或命令：执行公开候选流程；退出码：0；结果：候选通过端到端复验。"
E2E_FAIL_EVIDENCE = "执行公开候选流程后，delivered.txt 缺少修复标记。"
STANDARDS_PASS_EVIDENCE = "审查范围或基线：仓库编码规范与候选 diff；结论：未发现违反项。"
SPEC_PASS_EVIDENCE = "已核对的验收标准：Ticket 的全部验收标准；覆盖结论：候选完整覆盖。"


def issue(number: int) -> dict[str, Any]:
    return {
        "number": number,
        "title": "Deliver one ticket",
        "body": "Implement the requested behavior.\n\n## Acceptance criteria\n\n- It works.",
        "state": "OPEN",
        "labels": ["ready-for-agent"],
        "blocked_by": [],
    }


def test_publication_input_fingerprint_is_canonical_and_ignores_callbacks() -> None:
    first = {"z": [2, 1], "a": {"value": "kept"}, "_callback": object()}
    second = {"a": {"value": "kept"}, "z": [2, 1], "_other": object()}

    assert canonical_fingerprint(first) == canonical_fingerprint(second)
    assert canonical_fingerprint(first).startswith("sha256:")


@pytest.mark.parametrize(
    ("job", "expected"),
    [
        (
            {
                "publication_new_thread": True,
                "publication_thread_id": "publication",
                "development_thread_id": "development",
                "publication_attempts": 0,
            },
            None,
        ),
        (
            {
                "publication_thread_id": "publication",
                "development_thread_id": "development",
                "publication_attempts": 0,
            },
            "publication",
        ),
        (
            {"development_thread_id": "development", "publication_attempts": 1},
            "development",
        ),
        (
            {"development_thread_id": "development", "publication_attempts": 2},
            None,
        ),
    ],
)
def test_publication_thread_selection_has_one_shared_priority_chain(
    job: dict[str, Any], expected: str | None
) -> None:
    assert select_publication_thread(job, max_context_attempts=2) == expected


@pytest.mark.parametrize(
    ("job", "work_subject", "generation", "expected_boundary"),
    [
        (
            {
                "base_sha": "parent-base",
                "candidate_sha": "parent-candidate",
                "effective_revision": "parent-revision",
                "acceptance_record": {"reviewed_candidate_tree": "parent-tree"},
            },
            "parent-only:run-1",
            1,
            {
                "base_sha": "parent-base",
                "candidate_sha": "parent-candidate",
                "candidate_tree": "parent-tree",
                "effective_revision": "parent-revision",
            },
        ),
        (
            {
                "base_sha": "repair-base",
                "candidate_sha": "repair-candidate",
                "repair_generation": 3,
                "parent_revision": "parent-revision",
                "ticket_graph_revision": "graph-revision",
                "ticket_completion_records": [{"ticket": 3, "revision": "done"}],
                "acceptance_record": {"reviewed_candidate_tree": "repair-tree"},
            },
            "run-repair:run-1",
            3,
            {
                "base_sha": "repair-base",
                "candidate_sha": "repair-candidate",
                "candidate_tree": "repair-tree",
                "parent_revision": "parent-revision",
                "ticket_graph_revision": "graph-revision",
                "ticket_completion_records_fingerprint": canonical_fingerprint(
                    [{"ticket": 3, "revision": "done"}]
                ),
            },
        ),
    ],
)
def test_change_publication_invocation_binds_subject_generation_and_currentness(
    job: dict[str, Any],
    work_subject: str,
    generation: int,
    expected_boundary: dict[str, Any],
) -> None:
    state: dict[str, Any] = {"run_id": "run-1"}
    engine = object.__new__(ChangeDeliveryEngine)
    engine.contract = SimpleNamespace(save=lambda value: value)
    request = {"acceptance_scope": "test", "_callback": object()}

    event = engine._invocation_events(state, job, request, phase="publication")
    event("started", requested_thread_id=None, attempt_count=0)
    event("thread_started", reported_thread_id="publication-thread", attempt_count=1)
    event("completed", reported_thread_id="publication-thread", attempt_count=1)

    invocation = state["active_agent_invocation"]
    assert invocation["work_subject"] == work_subject
    assert invocation["generation"] == generation
    assert invocation["input_fingerprint"] == canonical_fingerprint(request)
    assert invocation["currentness_boundary"] == expected_boundary
    assert state["agent_invocation_history"][-1] == invocation


class ScriptedAgents:
    def __init__(self, checkout: Path) -> None:
        self.checkout = checkout
        self.development_requests: list[dict[str, Any]] = []
        self.publication_requests: list[dict[str, Any]] = []
        self.review_requests: list[dict[str, Any]] = []
        self.development_thread_ids: list[str | None] = []
        self.reviewer_thread_ids: list[str] = []
        self.publication_diffs: list[str] = []
        self.validation_checkouts: list[Path] = []
        self.events: list[str] = []
        self.review_count = 0

    def develop(self, request: dict[str, Any]) -> DevelopmentResult:
        self.events.append("develop")
        self.development_requests.append(request)
        actual_head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.checkout,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        assert request["head_sha"] == actual_head
        self.development_thread_ids.append(request.get("thread_id"))
        thread_id = str(request.get("thread_id") or "development-thread-1")
        target = self.checkout / "delivered.txt"
        text = "first attempt\n" if not target.exists() else target.read_text()
        if request.get("acceptance_artifact"):
            text += "repair applied\n"
        target.write_text(text, encoding="utf-8")
        return DevelopmentResult(
            thread_id=thread_id,
            summary="Implemented and tested the active ticket.",
        )

    def publication(self, request: dict[str, Any]) -> dict[str, Any]:
        self.events.append("publication")
        self.publication_requests.append(request)
        diff = subprocess.run(
            [
                "git",
                "diff",
                str(request["base_sha"]),
                str(request["candidate_sha"]),
            ],
            cwd=Path(str(request["checkout"])),
            text=True,
            capture_output=True,
            check=True,
        ).stdout
        self.publication_diffs.append(diff)
        return {
            "commit_message": "feat(delivery): complete one ticket autonomously",
            "pr_title": "feat(delivery): complete one ticket autonomously",
            "pr_body_markdown": """
## What Problem This Solves

The ticket stopped before publication.

## Why This Change Was Made

The delivery loop now owns the bounded workflow.

## User Impact

The active ticket reaches the Run Branch automatically.

## Evidence

The scripted end-to-end scenario passed.
""".strip(),
        }

    def review(self, request: dict[str, Any]) -> ReviewResult:
        self.events.append("review")
        self.review_requests.append(request)
        validation_checkout = Path(str(request["checkout"]))
        self.validation_checkouts.append(validation_checkout)
        assert validation_checkout != self.checkout
        reviewed_head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=validation_checkout,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        assert reviewed_head == request["candidate_sha"]
        assert "publication_sha" not in request
        assert "publication" not in request
        (validation_checkout / "validation.tmp").write_text(
            "temporary validation evidence\n", encoding="utf-8"
        )
        reviewer_id = f"reviewer-{self.review_count + 1}"
        self.reviewer_thread_ids.append(reviewer_id)
        self.review_count += 1
        common = {
            "checks": {
                "e2e": {
                    "status": "pass" if self.review_count == 2 else "fail",
                    "evidence": (
                        E2E_PASS_EVIDENCE
                        if self.review_count == 2
                        else E2E_FAIL_EVIDENCE
                    ),
                    "findings": [],
                },
                "standards": {
                    "status": "pass",
                    "evidence": STANDARDS_PASS_EVIDENCE,
                    "findings": [],
                },
                "spec": {
                    "status": "pass",
                    "evidence": SPEC_PASS_EVIDENCE,
                    "findings": [],
                },
            },
        }
        if self.review_count == 1:
            artifact = {
                **common,
            }
            artifact["checks"]["e2e"]["findings"] = [
                "问题：修复标记缺失；证据：delivered.txt 只有第一次尝试内容；必须修复：应用修复；复验：检查 delivered.txt。"
            ]
        else:
            artifact = common
        return ReviewResult(thread_id=reviewer_id, artifact=artifact)


class HumanBlockedDevelopmentAgents(ScriptedAgents):
    def __init__(self, checkout: Path) -> None:
        super().__init__(checkout)
        self.blocked = False
        self.resumed = False

    def develop(
        self, request: dict[str, Any]
    ) -> DevelopmentResult | HumanBlockerResult:
        self.development_requests.append(request)
        if not self.blocked:
            self.blocked = True
            return HumanBlockerResult(
                thread_id="blocked-development-thread",
                human_blockers=(
                    "GitHub denied access; tried gh issue view; grant Issue read access.",
                ),
            )
        if not self.resumed:
            assert request["thread_id"] == "blocked-development-thread"
            assert request["prior_human_blockers"] == [
                "GitHub denied access; tried gh issue view; grant Issue read access."
            ]
            self.resumed = True
        text = "resumed\n"
        if request.get("acceptance_artifact"):
            text += "repair applied\n"
        (self.checkout / "delivered.txt").write_text(text, encoding="utf-8")
        return DevelopmentResult(
            thread_id="blocked-development-thread",
            summary="Resumed after the human fixed access.",
        )


class ScriptedPublisher:
    def __init__(self, repo: Path) -> None:
        self.repo = repo
        self.pr_number = 11
        self.created_prs = 0
        self.pr_bodies: list[str] = []
        self.pr_titles: list[str] = []
        self.closed_issues: list[int] = []
        self.acceptance_records: list[dict[str, Any]] = []
        self.agent_run_statuses: list[dict[str, Any]] = []
        self.live_head: str | None = None
        self.base_branch: str | None = None
        self.escalated: list[int] = []
        self.revision_override: str | None = None
        self.checks = ["pass"]
        self.failed_check_evidence = {
            "pr_number": self.pr_number,
            "checks": [
                {
                    "name": "test",
                    "workflow": "ci",
                    "bucket": "fail",
                    "description": "The test job failed.",
                    "link": "https://example.invalid/checks/test",
                }
            ],
        }
        self.check_position = 0
        self.merged_sha: str | None = None
        self.merged_head: str | None = None
        self.publication_context_calls: list[int] = []
        self.prepared_ticket_closes: list[dict[str, Any]] = []

    def ensure_parent_branch(
        self, *, parent_number: int, branch: str, base_branch: str
    ) -> None:
        del parent_number, branch, base_branch

    def ensure_ticket_branch(
        self,
        *,
        ticket_number: int,
        branch: str,
        base_branch: str,
    ) -> None:
        return None

    def publish_branch(
        self,
        branch: str,
        head_sha: str,
        *,
        expected_remote_sha: str,
    ) -> None:
        if self.live_head == head_sha:
            return
        if self.live_head not in {None, expected_remote_sha}:
            raise ValueError("scripted remote ticket branch drifted")
        self.live_head = head_sha

    def ensure_ticket_pr(
        self,
        *,
        branch: str,
        base_branch: str,
        title: str,
        body: str,
        primary_ticket: int,
    ) -> int:
        self.created_prs += 1
        self.base_branch = base_branch
        self.pr_bodies.append(body)
        self.pr_titles.append(title)
        return self.pr_number

    def publication_context(self, pr_number: int) -> dict[str, object]:
        assert pr_number == self.pr_number
        self.publication_context_calls.append(pr_number)
        return {
            "number": pr_number,
            "url": f"https://example.invalid/pull/{pr_number}",
            "title": self.pr_titles[-1],
        }

    def required_checks(self, pr_number: int) -> str:
        value = self.checks[min(self.check_position, len(self.checks) - 1)]
        self.check_position += 1
        return value

    def required_check_evidence(self, pr_number: int) -> dict[str, Any]:
        assert pr_number == self.pr_number
        return self.failed_check_evidence

    def live_pull_request(self, pr_number: int) -> dict[str, Any]:
        result = {
            "head_sha": self.live_head,
            "base_branch": self.base_branch,
            "base_sha": (
                subprocess.run(
                    ["git", "rev-parse", str(self.base_branch)],
                    cwd=self.repo,
                    text=True,
                    capture_output=True,
                    check=True,
                ).stdout.strip()
                if self.base_branch
                else None
            ),
            "mergeable": True,
        }
        if self.merged_sha is not None and self.merged_head is not None:
            result.update(
                {
                    "head_sha": self.merged_head,
                    "base_sha": GitRepository(self.repo).commit_parents(
                        self.merged_sha
                    )[0],
                    "mergeable": False,
                    "state": "MERGED",
                    "integrated_sha": self.merged_sha,
                    "head_tree": _tree(self.repo, self.merged_head),
                    "integrated_tree": _tree(self.repo, self.merged_sha),
                    "integrated_message": GitRepository(self.repo).commit_subject(
                        self.merged_sha
                    ),
                    "integrated_parents": GitRepository(self.repo).commit_parents(
                        self.merged_sha
                    ),
                }
            )
        return result

    def record_acceptance(self, pr_number: int, record: dict[str, Any]) -> None:
        self.acceptance_records.append(record)

    def record_agent_run_status(self, pr_number: int, status: dict[str, Any]) -> None:
        self.agent_run_statuses[:] = [
            existing
            for existing in self.agent_run_statuses
            if existing["pr_number"] != pr_number
        ]
        self.agent_run_statuses.append({"pr_number": pr_number, **status})

    def squash_merge(
        self,
        *,
        pr_number: int,
        expected_head_sha: str,
        run_branch: str,
        commit_message: str,
    ) -> str:
        del pr_number
        tree = subprocess.run(
            ["git", "rev-parse", f"{expected_head_sha}^{{tree}}"],
            cwd=self.repo,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        parent = subprocess.run(
            ["git", "rev-parse", run_branch],
            cwd=self.repo,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        integrated = subprocess.run(
            ["git", "commit-tree", tree, "-p", parent, "-m", commit_message],
            cwd=self.repo,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        subprocess.run(
            [
                "git",
                "update-ref",
                f"refs/heads/{run_branch}",
                integrated,
                parent,
            ],
            cwd=self.repo,
            check=True,
        )
        self.merged_sha = integrated
        self.merged_head = expected_head_sha
        return integrated

    def sync_run_branch(self, *, run_branch: str, integrated_sha: str) -> None:
        subprocess.run(
            ["git", "update-ref", f"refs/heads/{run_branch}", integrated_sha],
            cwd=self.repo,
            check=True,
        )

    def prepare_primary_ticket_close(
        self,
        *,
        ticket_number: int,
        run_id: str,
        pr_number: int,
        integrated_sha: str,
    ) -> dict[str, Any]:
        self.prepared_ticket_closes.append(
            {"pr_number": pr_number, "integrated_sha": integrated_sha}
        )
        return {
            "actor": "scripted-publisher",
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
    ) -> dict[str, Any]:
        del run_id, pr_number, integrated_sha, close_intent
        if before_dispatch is not None:
            before_dispatch()
        self.closed_issues.append(ticket_number)
        return {"actor": "scripted-publisher", "event_id": ticket_number}

    def mark_ready_for_human(self, ticket_number: int) -> None:
        self.escalated.append(ticket_number)

    def current_effective_revision(
        self,
        *,
        parent_number: int,
        ticket_number: int,
        expected_revision: str,
    ) -> str:
        return self.revision_override or expected_revision


class CrashAfterMergePublisher(ScriptedPublisher):
    def __init__(self, repo: Path) -> None:
        super().__init__(repo)
        self.merged_sha: str | None = None
        self.merged_head: str | None = None
        self.crash_once = True

    def live_pull_request(self, pr_number: int) -> dict[str, Any]:
        if self.merged_sha is not None:
            assert self.merged_head is not None
            return {
                "head_sha": self.merged_head,
                "base_branch": self.base_branch,
                "base_sha": subprocess.run(
                    ["git", "rev-parse", f"{self.merged_sha}^"],
                    cwd=self.repo,
                    text=True,
                    capture_output=True,
                    check=True,
                ).stdout.strip(),
                "mergeable": False,
                "state": "MERGED",
                "integrated_sha": self.merged_sha,
                "head_tree": _tree(self.repo, self.merged_head),
                "integrated_tree": _tree(self.repo, self.merged_sha),
                "integrated_message": subprocess.run(
                    ["git", "log", "-1", "--format=%s", self.merged_sha],
                    cwd=self.repo,
                    text=True,
                    capture_output=True,
                    check=True,
                ).stdout.strip(),
                "integrated_parents": GitRepository(self.repo).commit_parents(
                    self.merged_sha
                ),
            }
        return super().live_pull_request(pr_number)

    def squash_merge(
        self,
        *,
        pr_number: int,
        expected_head_sha: str,
        run_branch: str,
        commit_message: str,
    ) -> str:
        integrated = super().squash_merge(
            pr_number=pr_number,
            expected_head_sha=expected_head_sha,
            run_branch=run_branch,
            commit_message=commit_message,
        )
        self.merged_sha = integrated
        self.merged_head = expected_head_sha
        self.live_head = None
        if self.crash_once:
            self.crash_once = False
            raise OSError("simulated crash after remote merge")
        return integrated


class UnknownMergeOutcomePublisher(ScriptedPublisher):
    def __init__(self, repo: Path) -> None:
        super().__init__(repo)
        self.merge_attempts = 0

    def squash_merge(
        self,
        *,
        pr_number: int,
        expected_head_sha: str,
        run_branch: str,
        commit_message: str,
    ) -> str:
        self.merge_attempts += 1
        if self.merge_attempts < 3:
            raise MergeOutcomeUnknownError("simulated lost squash merge response")
        return super().squash_merge(
            pr_number=pr_number,
            expected_head_sha=expected_head_sha,
            run_branch=run_branch,
            commit_message=commit_message,
        )


class MissingCloseIntentPublisher(ScriptedPublisher):
    def prepare_primary_ticket_close(
        self,
        *,
        ticket_number: int,
        run_id: str,
        pr_number: int,
        integrated_sha: str,
    ) -> None:
        del ticket_number, run_id, pr_number, integrated_sha


class MissingCloseOwnershipPublisher(ScriptedPublisher):
    def close_primary_ticket(
        self,
        *,
        ticket_number: int,
        run_id: str,
        pr_number: int,
        integrated_sha: str,
        close_intent: dict[str, Any] | None = None,
        before_dispatch: Callable[[], None] | None = None,
    ) -> None:
        del ticket_number, run_id, pr_number, integrated_sha, close_intent
        if before_dispatch is not None:
            before_dispatch()


class BaseMovesThenMergeResponseIsLostPublisher(CrashAfterMergePublisher):
    def squash_merge(
        self,
        *,
        pr_number: int,
        expected_head_sha: str,
        run_branch: str,
        commit_message: str,
    ) -> str:
        _advance_branch_with_same_tree(self.repo, run_branch)
        try:
            return super().squash_merge(
                pr_number=pr_number,
                expected_head_sha=expected_head_sha,
                run_branch=run_branch,
                commit_message=commit_message,
            )
        except GitError as error:
            raise OSError("simulated lost merge response") from error


class RemoteMergeBeforeLocalSyncPublisher(CrashAfterMergePublisher):
    def squash_merge(
        self,
        *,
        pr_number: int,
        expected_head_sha: str,
        run_branch: str,
        commit_message: str,
    ) -> str:
        del pr_number, run_branch, commit_message
        self.merged_sha = expected_head_sha
        self.merged_head = expected_head_sha
        self.live_head = None
        raise OSError("simulated crash before local Run Branch sync")


class SyncFailsOncePublisher(ScriptedPublisher):
    sync_attempts = 0

    def sync_run_branch(self, *, run_branch: str, integrated_sha: str) -> None:
        self.sync_attempts += 1
        if self.sync_attempts == 1:
            raise GitError("simulated transient fetch failure")
        super().sync_run_branch(
            run_branch=run_branch,
            integrated_sha=integrated_sha,
        )


class RevisionDriftsAfterMergePublisher(ScriptedPublisher):
    def __init__(self, repo: Path, new_revision: str) -> None:
        super().__init__(repo)
        self.new_revision = new_revision
        self.lose_first_merge_response = True
        self.historical_pr_number: int | None = None
        self.historical_merged_sha: str | None = None
        self.historical_merged_head: str | None = None

    def live_pull_request(self, pr_number: int) -> dict[str, Any]:
        if (
            pr_number == self.historical_pr_number
            and self.historical_merged_sha is not None
            and self.historical_merged_head is not None
        ):
            integrated = self.historical_merged_sha
            head = self.historical_merged_head
            return {
                "head_sha": head,
                "base_branch": self.base_branch,
                "base_sha": GitRepository(self.repo).commit_parents(integrated)[0],
                "mergeable": False,
                "state": "MERGED",
                "integrated_sha": integrated,
                "head_tree": _tree(self.repo, head),
                "integrated_tree": _tree(self.repo, integrated),
                "integrated_message": GitRepository(self.repo).commit_subject(
                    integrated
                ),
                "integrated_parents": GitRepository(self.repo).commit_parents(
                    integrated
                ),
            }
        return super().live_pull_request(pr_number)

    def squash_merge(
        self,
        *,
        pr_number: int,
        expected_head_sha: str,
        run_branch: str,
        commit_message: str,
    ) -> str:
        old_run_head = GitRepository(self.repo).resolve(run_branch)
        integrated = super().squash_merge(
            pr_number=pr_number,
            expected_head_sha=expected_head_sha,
            run_branch=run_branch,
            commit_message=commit_message,
        )
        if self.historical_pr_number is None:
            self.historical_pr_number = pr_number
            self.historical_merged_sha = integrated
            self.historical_merged_head = expected_head_sha
        subprocess.run(
            [
                "git",
                "update-ref",
                f"refs/heads/{run_branch}",
                old_run_head,
                integrated,
            ],
            cwd=self.repo,
            check=True,
        )
        self.revision_override = self.new_revision
        if self.lose_first_merge_response:
            self.lose_first_merge_response = False
            raise OSError("simulated lost response after revision-drifted merge")
        return integrated


class ExternalMergePublisher(ScriptedPublisher):
    def __init__(self, repo: Path) -> None:
        super().__init__(repo)
        self.external_sha: str | None = None

    def live_pull_request(self, pr_number: int) -> dict[str, Any]:
        if self.external_sha is None and self.live_head and self.base_branch:
            self.external_sha = super().squash_merge(
                pr_number=pr_number,
                expected_head_sha=self.live_head,
                run_branch=self.base_branch,
                commit_message="feat(delivery): complete one ticket autonomously",
            )
        assert self.external_sha is not None
        assert self.live_head is not None
        return {
            "head_sha": self.live_head,
            "base_branch": self.base_branch,
            "base_sha": subprocess.run(
                ["git", "rev-parse", f"{self.external_sha}^"],
                cwd=self.repo,
                text=True,
                capture_output=True,
                check=True,
            ).stdout.strip(),
            "mergeable": False,
            "state": "MERGED",
            "integrated_sha": self.external_sha,
            "head_tree": _tree(self.repo, self.live_head),
            "integrated_tree": _tree(self.repo, self.external_sha),
            "integrated_message": subprocess.run(
                ["git", "log", "-1", "--format=%s", self.external_sha],
                cwd=self.repo,
                text=True,
                capture_output=True,
                check=True,
            ).stdout.strip(),
            "integrated_parents": GitRepository(self.repo).commit_parents(
                self.external_sha
            ),
        }


class CrashAfterEscalationPublisher(ScriptedPublisher):
    def __init__(self, repo: Path) -> None:
        super().__init__(repo)
        self.crash_once = True

    def mark_ready_for_human(self, ticket_number: int) -> None:
        super().mark_ready_for_human(ticket_number)
        if self.crash_once:
            self.crash_once = False
            raise OSError("simulated lost escalation response")


class AlwaysRejectAgents(ScriptedAgents):
    def develop(self, request: dict[str, Any]) -> DevelopmentResult:
        self.development_thread_ids.append(request.get("thread_id"))
        thread_id = str(request.get("thread_id") or "development-thread-1")
        attempt = len(self.development_thread_ids)
        (self.checkout / "attempt.txt").write_text(
            f"attempt {attempt}\n", encoding="utf-8"
        )
        return DevelopmentResult(thread_id=thread_id, summary="Changed the code.")

    def review(self, request: dict[str, Any]) -> ReviewResult:
        validation_checkout = Path(str(request["checkout"]))
        assert validation_checkout != self.checkout
        self.review_count += 1
        return ReviewResult(
            thread_id=f"reviewer-{self.review_count}",
            artifact={
                "checks": {
                    "e2e": {
                        "status": "fail",
                        "evidence": "The scripted reviewer rejects this attempt.",
                        "findings": [
                            "问题：脚本化缺陷仍然存在；证据：脚本化 reviewer 发现该缺陷；必须修复：解决该缺陷；复验：运行脚本化 reviewer。"
                        ],
                    },
                    "standards": {
                        "status": "pass",
                        "evidence": STANDARDS_PASS_EVIDENCE,
                        "findings": [],
                    },
                    "spec": {
                        "status": "pass",
                        "evidence": SPEC_PASS_EVIDENCE,
                        "findings": [],
                    },
                },
            },
        )


class NoChangeAgents(ScriptedAgents):
    calls = 0

    def develop(self, request: dict[str, Any]) -> DevelopmentResult:
        self.calls += 1
        return DevelopmentResult(
            thread_id="development-thread-1",
            summary="No change was necessary.",
        )

    def publication(self, request: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError("no-change attempts must not publish")

    def review(self, request: dict[str, Any]) -> ReviewResult:
        raise AssertionError("no-change attempts must not start acceptance")


class CancelledAgents(ScriptedAgents):
    def develop(self, request: dict[str, Any]) -> DevelopmentResult:
        raise KeyboardInterrupt


class PassAgents(ScriptedAgents):
    def review(self, request: dict[str, Any]) -> ReviewResult:
        validation_checkout = Path(str(request["checkout"]))
        assert validation_checkout != self.checkout
        assert (validation_checkout / "delivered.txt").is_file()
        self.review_count += 1
        return ReviewResult(
            thread_id=f"reviewer-{self.review_count}",
            artifact={
                "checks": {
                    "e2e": {
                        "status": "pass",
                        "evidence": E2E_PASS_EVIDENCE,
                        "findings": [],
                    },
                    "standards": {
                        "status": "pass",
                        "evidence": STANDARDS_PASS_EVIDENCE,
                        "findings": [],
                    },
                    "spec": {
                        "status": "pass",
                        "evidence": SPEC_PASS_EVIDENCE,
                        "findings": [],
                    },
                },
            },
        )


class HumanThenHistoricalReviewerAgents(PassAgents):
    def review(self, request: dict[str, Any]) -> ReviewResult:
        result = super().review(request)
        if self.review_count == 1:
            artifact = result.artifact
            artifact["checks"]["e2e"] = {
                "status": "blocked",
                "evidence": "发生：GitHub Issue 读取权限被拒绝；尝试：执行 gh issue view；人必须：授予 Issue 读取权限。",
                "findings": [],
            }
            return ReviewResult("blocked-latest-reviewer", artifact)
        return ReviewResult("older-reviewer", result.artifact)


class RevisionAgents(PassAgents):
    def develop(self, request: dict[str, Any]) -> DevelopmentResult:
        result = super().develop(request)
        target = self.checkout / "delivered.txt"
        target.write_text(
            target.read_text(encoding="utf-8")
            + f"revision {len(self.development_requests)}\n",
            encoding="utf-8",
        )
        return result


class DevelopmentThreadReviewer(PassAgents):
    def review(self, request: dict[str, Any]) -> ReviewResult:
        result = super().review(request)
        return ReviewResult(
            thread_id="development-thread-1",
            artifact=result.artifact,
        )


class MalformedThenDuplicateReviewer(PassAgents):
    def review(self, request: dict[str, Any]) -> ReviewResult:
        return ReviewResult(
            thread_id="reviewer-1",
            artifact={"checks": {}},
        )


class ReplacementDevelopmentAgents(ScriptedAgents):
    def develop(self, request: dict[str, Any]) -> DevelopmentResult:
        if request.get("thread_id") is None:
            return super().develop(request)
        self.development_requests.append(request)
        target = self.checkout / "delivered.txt"
        target.write_text(
            target.read_text(encoding="utf-8") + "repair applied\n",
            encoding="utf-8",
        )
        return DevelopmentResult(
            thread_id="development-thread-2",
            summary="A replacement Development Thread completed the repair.",
            replaced_thread_id="development-thread-1",
        )


class ReplacementThenHistoricalReviewer(ReplacementDevelopmentAgents):
    def review(self, request: dict[str, Any]) -> ReviewResult:
        result = super().review(request)
        if self.review_count == 2:
            return ReviewResult(
                thread_id="development-thread-1",
                artifact=result.artifact,
            )
        return result


class PublicationFailsOnceAgents(PassAgents):
    def __init__(self, checkout: Path) -> None:
        super().__init__(checkout)
        self.fail_once = True

    def publication(self, request: dict[str, Any]) -> dict[str, Any]:
        if self.fail_once:
            self.fail_once = False
            raise ValueError("simulated Publication timeout")
        return super().publication(request)


class PublicationFailsUntilPendingAgents(PassAgents):
    def __init__(self, checkout: Path) -> None:
        super().__init__(checkout)
        self.fail = True
        self.publication_calls = 0

    def publication(self, request: dict[str, Any]) -> dict[str, Any]:
        self.publication_calls += 1
        if self.fail:
            self.events.append("publication")
            self.publication_requests.append(request)
            raise ValueError("simulated Publication timeout")
        return super().publication(request)


class PublicationDriftsBaseThenSucceedsAgents(PassAgents):
    def __init__(self, checkout: Path, repository: Path, run_branch: str) -> None:
        super().__init__(checkout)
        self.repository = repository
        self.run_branch = run_branch
        self.publication_calls = 0

    def publication(self, request: dict[str, Any]) -> dict[str, Any]:
        self.publication_calls += 1
        if self.publication_calls == 1:
            self.events.append("publication")
            self.publication_requests.append(request)
            _advance_branch_with_same_tree(self.repository, self.run_branch)
            raise ValueError("simulated Publication timeout")
        return super().publication(request)


class PublicationSucceedsDuringBaseDriftAgents(PassAgents):
    def __init__(self, checkout: Path, repository: Path, run_branch: str) -> None:
        super().__init__(checkout)
        self.repository = repository
        self.run_branch = run_branch
        self.publication_calls = 0

    def publication(self, request: dict[str, Any]) -> dict[str, Any]:
        self.publication_calls += 1
        if self.publication_calls == 1:
            _advance_branch_with_same_tree(self.repository, self.run_branch)
        return super().publication(request)


class CheckReadFailsOncePublisher(ScriptedPublisher):
    def __init__(self, repo: Path) -> None:
        super().__init__(repo)
        self.check_read_failures = 1

    def required_checks(self, pr_number: int) -> str:
        if self.check_read_failures:
            self.check_read_failures -= 1
            raise TimeoutError("simulated Required Checks timeout")
        return super().required_checks(pr_number)


class PublicationContextFailsUntilResumedPublisher(ScriptedPublisher):
    def __init__(self, repo: Path) -> None:
        super().__init__(repo)
        self.fail_publication_context = True

    def publication_context(self, pr_number: int) -> dict[str, object]:
        self.publication_context_calls.append(pr_number)
        if self.fail_publication_context:
            raise GitHubReadError(
                "github_timeout", "timed out reading the existing PR context"
            )
        return {
            "number": pr_number,
            "state": "OPEN",
            "title": "Existing PR",
            "body": "Existing body",
        }


class PublicationReplacementAgents(PassAgents):
    def publication(self, request: dict[str, Any]) -> PublicationResult:
        artifact = super().publication(request)
        return PublicationResult(
            thread_id="development-thread-2",
            artifact={
                "result_kind": "publication",
                **artifact,
                "human_blockers": None,
            },
            replaced_thread_id="development-thread-1",
        )


class CheckRepairAgents(PassAgents):
    def develop(self, request: dict[str, Any]) -> DevelopmentResult:
        result = super().develop(request)
        if request.get("ci_evidence"):
            target = self.checkout / "delivered.txt"
            target.write_text(
                target.read_text(encoding="utf-8") + "ci repaired\n",
                encoding="utf-8",
            )
        return result


class RepairPublicationFailsUntilResumedAgents(CheckRepairAgents):
    def __init__(self, checkout: Path) -> None:
        super().__init__(checkout)
        self.publication_calls = 0
        self.fail_repair_publication = True

    def publication(self, request: dict[str, Any]) -> dict[str, Any]:
        self.publication_calls += 1
        if self.publication_calls > 1 and self.fail_repair_publication:
            self.events.append("publication")
            self.publication_requests.append(request)
            raise ValueError("simulated repair Publication timeout")
        return super().publication(request)


class AcceptanceRepairPublicationFailsUntilResumedAgents(ScriptedAgents):
    def __init__(self, checkout: Path) -> None:
        super().__init__(checkout)
        self.fail_publication = True

    def publication(self, request: dict[str, Any]) -> dict[str, Any]:
        if self.fail_publication:
            self.events.append("publication")
            self.publication_requests.append(request)
            raise ValueError("simulated acceptance repair Publication timeout")
        return super().publication(request)

    def review(self, request: dict[str, Any]) -> ReviewResult:
        if self.review_count >= 2:
            return PassAgents.review(self, request)
        return super().review(request)


class CrashAfterCandidateGit(GitRepository):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.crash_once = True

    def commit_candidate(
        self, checkout: Path, *, ticket_number: int, attempt: int
    ) -> str | None:
        candidate = super().commit_candidate(
            checkout, ticket_number=ticket_number, attempt=attempt
        )
        if self.crash_once:
            self.crash_once = False
            raise OSError("simulated crash after Candidate commit")
        return candidate


class LiveBaseDriftPublisher(ScriptedPublisher):
    def live_pull_request(self, pr_number: int) -> dict[str, Any]:
        live = super().live_pull_request(pr_number)
        live["base_sha"] = "0" * 40
        return live


class ClosedUnmergedPublisher(ScriptedPublisher):
    def __init__(self, repo: Path) -> None:
        super().__init__(repo)
        self.closed = False

    def live_pull_request(self, pr_number: int) -> dict[str, Any]:
        live = super().live_pull_request(pr_number)
        if self.closed:
            live.update({"state": "CLOSED", "mergeable": False})
        return live


class ClosesAfterEnsurePublisher(ClosedUnmergedPublisher):
    def ensure_ticket_pr(
        self,
        *,
        branch: str,
        base_branch: str,
        title: str,
        body: str,
        primary_ticket: int,
    ) -> int:
        number = super().ensure_ticket_pr(
            branch=branch,
            base_branch=base_branch,
            title=title,
            body=body,
            primary_ticket=primary_ticket,
        )
        self.closed = True
        return number


class CrashBeforeClosePublisher(ScriptedPublisher):
    def __init__(self, repo: Path) -> None:
        super().__init__(repo)
        self.crash_once = True

    def close_primary_ticket(
        self,
        *,
        ticket_number: int,
        run_id: str,
        pr_number: int,
        integrated_sha: str,
        close_intent: dict[str, Any] | None = None,
        before_dispatch: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        if self.crash_once:
            self.crash_once = False
            raise OSError("simulated crash before Primary Ticket close")
        return super().close_primary_ticket(
            ticket_number=ticket_number,
            run_id=run_id,
            pr_number=pr_number,
            integrated_sha=integrated_sha,
            close_intent=close_intent,
            before_dispatch=before_dispatch,
        )


class DelayedMergedStatePublisher(ScriptedPublisher):
    open_responses_remaining = 2

    def live_pull_request(self, pr_number: int) -> dict[str, Any]:
        live = super().live_pull_request(pr_number)
        if self.merged_sha is not None and self.open_responses_remaining:
            self.open_responses_remaining -= 1
            live["state"] = "OPEN"
        return live


class SavedIntegratedOpenPublisher(DelayedMergedStatePublisher):
    def __init__(self, repo: Path) -> None:
        super().__init__(repo)
        self.open_responses_remaining = 100
        self.merge_calls = 0
        self.stale_base_sha: str | None = None

    def live_pull_request(self, pr_number: int) -> dict[str, Any]:
        live = super().live_pull_request(pr_number)
        if live.get("state") == "OPEN" and self.stale_base_sha is not None:
            live["base_sha"] = self.stale_base_sha
        return live

    def squash_merge(
        self,
        *,
        pr_number: int,
        expected_head_sha: str,
        run_branch: str,
        commit_message: str,
    ) -> str:
        self.merge_calls += 1
        base_sha = GitRepository(self.repo).resolve(run_branch)
        if self.stale_base_sha is None:
            self.stale_base_sha = base_sha
        integrated = super().squash_merge(
            pr_number=pr_number,
            expected_head_sha=expected_head_sha,
            run_branch=run_branch,
            commit_message=commit_message,
        )
        distinct = subprocess.run(
            [
                "git",
                "-c",
                "user.name=Remote GitHub",
                "-c",
                "user.email=remote@example.invalid",
                "commit-tree",
                f"{expected_head_sha}^{{tree}}",
                "-p",
                base_sha,
                "-m",
                commit_message,
            ],
            cwd=self.repo,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        subprocess.run(
            [
                "git",
                "update-ref",
                f"refs/heads/{run_branch}",
                distinct,
                integrated,
            ],
            cwd=self.repo,
            check=True,
        )
        self.merged_sha = distinct
        self.merged_head = expected_head_sha
        return distinct


class MismatchedMergeResultPublisher(ScriptedPublisher):
    def squash_merge(
        self,
        *,
        pr_number: int,
        expected_head_sha: str,
        run_branch: str,
        commit_message: str,
    ) -> str:
        result = super().squash_merge(
            pr_number=pr_number,
            expected_head_sha=expected_head_sha,
            run_branch=run_branch,
            commit_message=commit_message,
        )
        wrong = subprocess.run(
            [
                "git",
                "commit-tree",
                f"{result}^{{tree}}",
                "-p",
                f"{result}^",
                "-m",
                "wrong integrated message",
            ],
            cwd=self.repo,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        self.merged_sha = wrong
        return wrong


class BaseMovesDuringMergePublisher(ScriptedPublisher):
    drift_sha: str | None = None

    def squash_merge(
        self,
        *,
        pr_number: int,
        expected_head_sha: str,
        run_branch: str,
        commit_message: str,
    ) -> str:
        self.drift_sha = _advance_branch_with_same_tree(self.repo, run_branch)
        return super().squash_merge(
            pr_number=pr_number,
            expected_head_sha=expected_head_sha,
            run_branch=run_branch,
            commit_message=commit_message,
        )


def test_ticket_delivery_repairs_then_squash_merges_and_closes_primary(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": issue(3)})
    states = StateStore(git_repo / ".agent-run")
    controller = Controller(
        FixtureGitHubReader(fixture), GitRepository(git_repo), states
    )
    state, _ = controller.start(1)
    checkout = git_repo / ".agent-run" / "worktrees" / state["run_id"] / "ticket-3"
    agents = ScriptedAgents(checkout)
    github = ScriptedPublisher(git_repo)
    engine = TicketDeliveryEngine(
        git=GitRepository(git_repo),
        states=states,
        github=github,
        agents=agents,
    )

    delivered = engine.deliver(state["run_id"])

    assert delivered["status"] == "ticket_completed"
    job = delivered["active_ticket_job"]
    assert job["modification_attempts"] == 2
    assert job["development_thread_id"] == "development-thread-1"
    assert agents.development_thread_ids == [None, "development-thread-1"]
    assert agents.events == ["develop", "review", "develop", "review", "publication"]
    assert len(agents.publication_diffs) == 1
    assert "repair applied" in agents.publication_diffs[0]
    assert all(
        "change_diff" not in request
        for request in (
            *agents.development_requests,
            *agents.publication_requests,
            *agents.review_requests,
        )
    )
    assert agents.development_requests[0]["ticket"]["url"].endswith(
        "/example/project/issues/3"
    )

    assert agents.review_requests[0]["parent"]["url"].endswith(
        "/example/project/issues/1"
    )
    assert "development_summary" not in agents.review_requests[0]
    assert agents.development_requests[1]["repair_source"] == "acceptance"
    assert agents.development_requests[1]["acceptance_artifact"] == {
        "checks": {
            "e2e": {
                "status": "fail",
                "evidence": E2E_FAIL_EVIDENCE,
                "findings": [
                    "问题：修复标记缺失；证据：delivered.txt 只有第一次尝试内容；必须修复：应用修复；复验：检查 delivered.txt。"
                ],
            },
            "standards": {
                "status": "pass",
                "evidence": STANDARDS_PASS_EVIDENCE,
                "findings": [],
            },
            "spec": {
                "status": "pass",
                "evidence": SPEC_PASS_EVIDENCE,
                "findings": [],
            },
        }
    }
    assert (
        agents.development_requests[1]["head_sha"]
        == agents.review_requests[0]["candidate_sha"]
    )
    assert len(set(agents.reviewer_thread_ids)) == 2
    assert len(set(agents.validation_checkouts)) == 2
    assert all(not path.exists() for path in agents.validation_checkouts)
    assert github.created_prs == 1
    assert github.pr_bodies == [
        "Parent Issue: #1\nPrimary Ticket: #3\nDelivery Type: Ticket\n\n"
        "## What Problem This Solves\n\nThe ticket stopped before publication.\n\n"
        "## Why This Change Was Made\n\nThe delivery loop now owns the bounded workflow.\n\n"
        "## User Impact\n\nThe active ticket reaches the Run Branch automatically.\n\n"
        "## Evidence\n\nThe scripted end-to-end scenario passed."
    ]
    assert github.closed_issues == [3]
    assert github.escalated == []
    assert github.acceptance_records == []
    assert github.agent_run_statuses == [
        {
            "pr_number": 11,
            "scope": "ticket-3",
            "base_sha": job["base_sha"],
            "candidate_sha": job["candidate_sha"],
            "validation_outcome": "pass",
            "lane_statuses": {"e2e": "pass", "standards": "pass", "spec": "pass"},
            "required_checks": "pass",
            "next_action": "squash merge into the Run Branch",
        }
    ]


@pytest.mark.parametrize(
    "publisher_type",
    [MissingCloseIntentPublisher, MissingCloseOwnershipPublisher],
)
def test_ticket_delivery_requires_exact_close_evidence(
    git_repo: Path,
    publisher_type: type[ScriptedPublisher],
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": issue(3)})
    states = StateStore(git_repo / ".agent-run")
    state, _ = Controller(
        FixtureGitHubReader(fixture), GitRepository(git_repo), states
    ).start(1)
    checkout = git_repo / ".agent-run" / "worktrees" / state["run_id"] / "ticket-3"
    engine = TicketDeliveryEngine(
        git=GitRepository(git_repo),
        states=states,
        github=publisher_type(git_repo),
        agents=ScriptedAgents(checkout),
    )

    blocked = engine.deliver(state["run_id"])

    assert blocked["status"] == "ready_for_human"
    assert blocked["active_ticket_job"]["phase"] == "blocked"

    persisted = states.load_run(state["run_id"])
    assert persisted is not None
    assert persisted["active_ticket_job"]["phase"] == "blocked"
    assert persisted["status"] != "ticket_completed"
    assert "ticket_close_ownership" not in persisted["active_ticket_job"]


def test_development_human_blocker_preserves_workspace_and_resumes_same_thread(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": issue(3)})
    states = StateStore(git_repo / ".agent-run")
    controller = Controller(
        FixtureGitHubReader(fixture), GitRepository(git_repo), states
    )
    state, _ = controller.start(1)
    checkout = git_repo / ".agent-run" / "worktrees" / state["run_id"] / "ticket-3"
    agents = HumanBlockedDevelopmentAgents(checkout)
    publisher = ScriptedPublisher(git_repo)
    engine = TicketDeliveryEngine(
        git=GitRepository(git_repo), states=states, github=publisher, agents=agents
    )

    blocked = engine.deliver(str(state["run_id"]))

    assert blocked["status"] == "ready_for_human"
    job = blocked["active_ticket_job"]
    assert job["human_blockers"] == [
        "GitHub denied access; tried gh issue view; grant Issue read access."
    ]
    assert job["human_blocker_phase"] == "developing"
    assert checkout.exists()
    assert publisher.created_prs == 0
    assert blocked["diagnostics"][0]["message"] == job["human_blockers"][0]

    resumed, _ = controller.resume(str(state["run_id"]), resume_human_blocker=True)
    assert resumed["active_ticket_job"]["phase"] == "developing"
    assert resumed["active_ticket_job"]["prior_human_blockers"] == job["human_blockers"]

    engine.deliver(str(state["run_id"]))

    assert len(agents.development_requests) >= 2
    assert agents.development_requests[1]["thread_id"] == "blocked-development-thread"
    run_count = subprocess.run(
        [
            "git",
            "rev-list",
            "--count",
            f"{state['base']['sha']}..{state['run_branch']}",
        ],
        cwd=git_repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    assert run_count == "1"
    message = subprocess.run(
        ["git", "log", "-1", "--format=%s", state["run_branch"]],
        cwd=git_repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    assert message == "feat(delivery): complete one ticket autonomously"
    assert not checkout.exists()
    persisted = json.loads(
        (git_repo / ".agent-run" / "runs" / f"{state['run_id']}.json").read_text(
            encoding="utf-8"
        )
    )
    assert persisted["status"] == "ticket_completed"
    completed_job = persisted["ticket_jobs"]["3"]
    assert completed_job["human_blocker_history"] == [
        {
            "phase": "developing",
            "human_blockers": [
                "GitHub denied access; tried gh issue view; grant Issue read access."
            ],
        }
    ]
    for key in ("human_blockers", "human_blocker_phase", "prior_human_blockers"):
        assert key not in completed_job
    assert "human_blockers" not in persisted["timeline"][-1]


def test_fresh_validation_human_resume_rejects_an_older_reviewer_thread(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": issue(3)})
    states = StateStore(git_repo / ".agent-run")
    state, _ = Controller(
        FixtureGitHubReader(fixture), GitRepository(git_repo), states
    ).start(1)
    checkout = git_repo / ".agent-run" / "worktrees" / state["run_id"] / "ticket-3"
    agents = HumanThenHistoricalReviewerAgents(checkout)
    engine = TicketDeliveryEngine(
        git=GitRepository(git_repo),
        states=states,
        github=ScriptedPublisher(git_repo),
        agents=agents,
    )

    blocked = engine.deliver(str(state["run_id"]))
    assert blocked["status"] == "ready_for_human"
    job = blocked["active_ticket_job"]
    assert job["reviewer_thread_ids"] == ["blocked-latest-reviewer"]
    job["reviewer_thread_ids"].insert(0, "older-reviewer")
    blocked["ticket_jobs"]["3"] = dict(job)
    states.save_run(str(state["run_id"]), blocked)

    Controller(FixtureGitHubReader(fixture), GitRepository(git_repo), states).resume(
        str(state["run_id"]), resume_human_blocker=True
    )

    with pytest.raises(
        ValueError, match="Human Blocker resume requires the latest Reviewer Thread"
    ):
        engine.deliver(str(state["run_id"]))


def test_tenth_changed_attempt_escalates_without_merge_or_close(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": issue(3)})
    states = StateStore(git_repo / ".agent-run")
    controller = Controller(
        FixtureGitHubReader(fixture), GitRepository(git_repo), states
    )
    state, _ = controller.start(1)
    checkout = git_repo / ".agent-run" / "worktrees" / state["run_id"] / "ticket-3"
    github = ScriptedPublisher(git_repo)
    agents = AlwaysRejectAgents(checkout)

    result = TicketDeliveryEngine(
        git=GitRepository(git_repo),
        states=states,
        github=github,
        agents=agents,
    ).deliver(state["run_id"])

    assert result["status"] == "blocked"
    assert result["diagnostics"][0]["code"] == "modification_budget_exhausted"
    assert result["active_ticket_job"]["modification_attempts"] == 10
    assert github.escalated == [3]
    assert github.created_prs == 0
    assert github.closed_issues == []


def test_no_change_attempt_does_not_consume_modification_budget(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": issue(3)})
    states = StateStore(git_repo / ".agent-run")
    state, _ = Controller(
        FixtureGitHubReader(fixture), GitRepository(git_repo), states
    ).start(1)
    checkout = git_repo / ".agent-run" / "worktrees" / state["run_id"] / "ticket-3"
    github = ScriptedPublisher(git_repo)

    first_agents = NoChangeAgents(checkout)
    result = TicketDeliveryEngine(
        git=GitRepository(git_repo),
        states=states,
        github=github,
        agents=first_agents,
    ).deliver(state["run_id"])

    assert result["status"] == "blocked"
    assert result["diagnostics"][0]["code"] == "no_code_changes"
    assert result["active_ticket_job"]["modification_attempts"] == 0
    assert github.escalated == []

    resumed, _ = Controller(
        FixtureGitHubReader(fixture), GitRepository(git_repo), states
    ).resume(state["run_id"])
    assert resumed["status"] == "progress_exhausted"
    assert resumed["active_ticket_job"] is None
    assert resumed["ticket_jobs"]["3"]["modification_attempts"] == 0
    assert resumed["ticket_jobs"]["3"]["blocked_reason"] == ("no_code_changes")
    assert first_agents.calls == 1

    fixture_data = json.loads(fixture.read_text(encoding="utf-8"))
    fixture_data["issues"]["3"]["body"] += "\nNew authoritative detail."
    fixture.write_text(json.dumps(fixture_data), encoding="utf-8")
    revised, _ = Controller(
        FixtureGitHubReader(fixture), GitRepository(git_repo), states
    ).resume(state["run_id"])
    assert revised["status"] == "requeue_required"
    assert revised["requeue_required"]["work_subject"] == "ticket:3"


def test_cancelled_worker_cleans_stable_ticket_checkout(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": issue(3)})
    states = StateStore(git_repo / ".agent-run")
    state, _ = Controller(
        FixtureGitHubReader(fixture), GitRepository(git_repo), states
    ).start(1)
    checkout = git_repo / ".agent-run" / "worktrees" / state["run_id"] / "ticket-3"

    with pytest.raises(KeyboardInterrupt):
        TicketDeliveryEngine(
            git=GitRepository(git_repo),
            states=states,
            github=ScriptedPublisher(git_repo),
            agents=CancelledAgents(checkout),
        ).deliver(state["run_id"])

    assert not checkout.exists()


def test_resume_reconciles_pr_merged_before_state_save(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": issue(3)})
    states = StateStore(git_repo / ".agent-run")
    state, _ = Controller(
        FixtureGitHubReader(fixture), GitRepository(git_repo), states
    ).start(1)
    checkout = git_repo / ".agent-run" / "worktrees" / state["run_id"] / "ticket-3"
    publisher = CrashAfterMergePublisher(git_repo)
    engine = TicketDeliveryEngine(
        git=GitRepository(git_repo),
        states=states,
        github=publisher,
        agents=ScriptedAgents(checkout),
    )

    with pytest.raises(OSError, match="after remote merge"):
        engine.deliver(state["run_id"])
    resumed = engine.deliver(state["run_id"])

    assert resumed["status"] == "ticket_completed"
    assert publisher.closed_issues == [3]
    count = subprocess.run(
        [
            "git",
            "rev-list",
            "--count",
            f"{state['base']['sha']}..{state['run_branch']}",
        ],
        cwd=git_repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    assert count == "1"


def test_unknown_merge_outcome_retries_the_same_intent_at_most_three_times(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": issue(3)})
    states = StateStore(git_repo / ".agent-run")
    state, _ = Controller(
        FixtureGitHubReader(fixture), GitRepository(git_repo), states
    ).start(1)
    checkout = git_repo / ".agent-run" / "worktrees" / state["run_id"] / "ticket-3"
    publisher = UnknownMergeOutcomePublisher(git_repo)
    engine = TicketDeliveryEngine(
        git=GitRepository(git_repo),
        states=states,
        github=publisher,
        agents=ScriptedAgents(checkout),
    )

    first = engine.deliver(state["run_id"])
    second = engine.deliver(state["run_id"])
    completed = engine.deliver(state["run_id"])

    assert first["status"] == "waiting_external"
    assert second["status"] == "waiting_external"
    assert completed["status"] == "ticket_completed"
    job = completed["active_ticket_job"]
    assert job["merge_intent"]["attempts"] == 3
    assert [entry["attempt"] for entry in job["merge_reconciliation_history"]] == [1, 2]
    assert publisher.merge_attempts == 3
    assert publisher.closed_issues == [3]


def test_live_effective_revision_drift_blocks_merge(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": issue(3)})
    states = StateStore(git_repo / ".agent-run")
    state, _ = Controller(
        FixtureGitHubReader(fixture), GitRepository(git_repo), states
    ).start(1)
    checkout = git_repo / ".agent-run" / "worktrees" / state["run_id"] / "ticket-3"
    publisher = ScriptedPublisher(git_repo)
    publisher.revision_override = "sha256:new-live-revision"

    result = TicketDeliveryEngine(
        git=GitRepository(git_repo),
        states=states,
        github=publisher,
        agents=PassAgents(checkout),
    ).deliver(state["run_id"])

    assert result["status"] == "blocked"
    assert result["diagnostics"][0]["code"] == "effective_revision_mismatch"
    assert publisher.closed_issues == []


def test_failed_required_check_evidence_reaches_development_thread(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": issue(3)})
    states = StateStore(git_repo / ".agent-run")
    state, _ = Controller(
        FixtureGitHubReader(fixture), GitRepository(git_repo), states
    ).start(1)
    checkout = git_repo / ".agent-run" / "worktrees" / state["run_id"] / "ticket-3"
    publisher = ScriptedPublisher(git_repo)
    publisher.checks = ["fail", "pass"]
    agents = CheckRepairAgents(checkout)

    result = TicketDeliveryEngine(
        git=GitRepository(git_repo),
        states=states,
        github=publisher,
        agents=agents,
    ).deliver(state["run_id"])

    assert result["status"] == "ticket_completed"
    assert agents.development_requests[1]["ci_evidence"] == (
        publisher.failed_check_evidence
    )
    assert agents.development_requests[1]["repair_source"] == ("required_checks")
    assert len(agents.publication_requests) == 2
    assert agents.publication_requests[1]["existing_pr"] == {
        "number": 11,
        "url": "https://example.invalid/pull/11",
        "title": "feat(delivery): complete one ticket autonomously",
    }
    assert publisher.publication_context_calls == [11]
    assert publisher.created_prs == 2
    assert len(publisher.pr_bodies) == 2
    assert len(publisher.agent_run_statuses) == 1
    assert publisher.agent_run_statuses[0]["validation_outcome"] == "pass"


def test_parent_publication_reads_existing_pr_context_once(git_repo: Path) -> None:
    publisher = ScriptedPublisher(git_repo)
    publisher.pr_titles.append("feat(parent): deliver parent scope")
    loop = ParentDeliveryLoop(
        git=GitRepository(git_repo),
        states=StateStore(git_repo / ".agent-run"),
        github=publisher,
        agents=ScriptedAgents(git_repo),
    )

    request = loop._publication_request(
        {
            "repository": "example/project",
            "run_id": "run-1",
            "parent": {"number": 1},
            "base": {"branch": "main"},
        },
        {
            "effective_revision": "revision-1",
            "base_sha": "base-sha",
            "candidate_sha": "candidate-sha",
            "development_thread_id": "development-thread",
            "publication_attempts": 0,
            "acceptance_artifact": {},
            "pr_number": 11,
        },
        git_repo,
    )

    assert request["existing_pr"]["number"] == 11
    assert publisher.publication_context_calls == [11]


def test_fresh_validation_rejects_development_thread_identity(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": issue(3)})
    states = StateStore(git_repo / ".agent-run")
    state, _ = Controller(
        FixtureGitHubReader(fixture), GitRepository(git_repo), states
    ).start(1)
    checkout = git_repo / ".agent-run" / "worktrees" / state["run_id"] / "ticket-3"

    with pytest.raises(ValueError, match="Development Thread"):
        TicketDeliveryEngine(
            git=GitRepository(git_repo),
            states=states,
            github=ScriptedPublisher(git_repo),
            agents=DevelopmentThreadReviewer(checkout),
        ).deliver(state["run_id"])


def test_fresh_reviewer_identity_is_persisted_before_artifact_parsing(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": issue(3)})
    states = StateStore(git_repo / ".agent-run")
    state, _ = Controller(
        FixtureGitHubReader(fixture), GitRepository(git_repo), states
    ).start(1)
    checkout = git_repo / ".agent-run" / "worktrees" / state["run_id"] / "ticket-3"
    agents = MalformedThenDuplicateReviewer(checkout)
    engine = TicketDeliveryEngine(
        git=GitRepository(git_repo),
        states=states,
        github=ScriptedPublisher(git_repo),
        agents=agents,
    )

    with pytest.raises(ValueError, match="checks has missing fields"):
        engine.deliver(state["run_id"])
    interrupted = states.load_run(state["run_id"])
    assert interrupted is not None
    assert interrupted["active_ticket_job"]["reviewer_thread_ids"] == ["reviewer-1"]

    with pytest.raises(ValueError, match="new Reviewer Thread"):
        engine.deliver(state["run_id"])


def test_fresh_validation_rejects_historical_development_thread(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": issue(3)})
    states = StateStore(git_repo / ".agent-run")
    state, _ = Controller(
        FixtureGitHubReader(fixture), GitRepository(git_repo), states
    ).start(1)
    checkout = git_repo / ".agent-run" / "worktrees" / state["run_id"] / "ticket-3"
    publisher = ScriptedPublisher(git_repo)

    with pytest.raises(ValueError, match="Development Thread"):
        TicketDeliveryEngine(
            git=GitRepository(git_repo),
            states=states,
            github=publisher,
            agents=ReplacementThenHistoricalReviewer(checkout),
        ).deliver(state["run_id"])

    interrupted = states.load_run(state["run_id"])
    assert interrupted is not None
    job = interrupted["active_ticket_job"]
    assert job["development_thread_id"] == "development-thread-2"
    assert job["development_thread_history"] == ["development-thread-1"]
    assert publisher.created_prs == 0


def test_replacement_development_thread_continues_same_ticket_job(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": issue(3)})
    states = StateStore(git_repo / ".agent-run")
    state, _ = Controller(
        FixtureGitHubReader(fixture), GitRepository(git_repo), states
    ).start(1)
    checkout = git_repo / ".agent-run" / "worktrees" / state["run_id"] / "ticket-3"
    agents = ReplacementDevelopmentAgents(checkout)
    publisher = ScriptedPublisher(git_repo)

    delivered = TicketDeliveryEngine(
        git=GitRepository(git_repo),
        states=states,
        github=publisher,
        agents=agents,
    ).deliver(state["run_id"])

    job = delivered["active_ticket_job"]
    assert delivered["status"] == "ticket_completed"
    assert job["development_thread_id"] == "development-thread-2"
    assert job["development_thread_history"] == ["development-thread-1"]
    assert job["modification_attempts"] == 2
    assert publisher.created_prs == 1


def test_publication_replacement_thread_is_rejected(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": issue(3)})
    states = StateStore(git_repo / ".agent-run")
    state, _ = Controller(
        FixtureGitHubReader(fixture), GitRepository(git_repo), states
    ).start(1)
    checkout = git_repo / ".agent-run" / "worktrees" / state["run_id"] / "ticket-3"
    agents = PublicationReplacementAgents(checkout)
    publisher = ScriptedPublisher(git_repo)

    with pytest.raises(ValueError, match="cannot replace"):
        TicketDeliveryEngine(
            git=GitRepository(git_repo),
            states=states,
            github=publisher,
            agents=agents,
        ).deliver(state["run_id"])

    assert publisher.created_prs == 0


def test_published_head_gate_rejects_live_base_sha_drift(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": issue(3)})
    states = StateStore(git_repo / ".agent-run")
    state, _ = Controller(
        FixtureGitHubReader(fixture), GitRepository(git_repo), states
    ).start(1)
    checkout = git_repo / ".agent-run" / "worktrees" / state["run_id"] / "ticket-3"
    publisher = LiveBaseDriftPublisher(git_repo)

    result = TicketDeliveryEngine(
        git=GitRepository(git_repo),
        states=states,
        github=publisher,
        agents=PassAgents(checkout),
    ).deliver(state["run_id"])

    assert result["status"] == "blocked"
    assert result["diagnostics"][0]["code"] == "published_head_mismatch"
    assert publisher.agent_run_statuses[-1]["next_action"] == (
        "blocked: Published-Head Gate rejected live PR state"
    )
    assert publisher.closed_issues == []


def test_persisted_closed_unmerged_pr_blocks_without_creating_another(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": issue(3)})
    states = StateStore(git_repo / ".agent-run")
    state, _ = Controller(
        FixtureGitHubReader(fixture), GitRepository(git_repo), states
    ).start(1)
    checkout = git_repo / ".agent-run" / "worktrees" / state["run_id"] / "ticket-3"
    publisher = ClosedUnmergedPublisher(git_repo)
    publisher.checks = ["pending"]
    engine = TicketDeliveryEngine(
        git=GitRepository(git_repo),
        states=states,
        github=publisher,
        agents=PassAgents(checkout),
    )
    waiting = engine.deliver(state["run_id"])
    assert waiting["status"] == "waiting_checks"
    assert waiting["active_ticket_job"]["pr_number"] == 11
    assert publisher.created_prs == 1
    publisher.closed = True
    refreshed = states.load_run(state["run_id"])
    assert refreshed is not None
    refreshed["ticket_graph"]["tickets"]["3"][
        "content_revision"
    ] = "sha256:closed-pr-new-content"
    states.save_run(state["run_id"], refreshed)

    blocked = engine.deliver(state["run_id"])

    assert blocked["status"] == "blocked"
    assert blocked["diagnostics"][0]["code"] == "ticket_pr_closed_unmerged"
    assert blocked["active_ticket_job"]["blocked_reason"] == (
        "ticket_pr_closed_unmerged"
    )
    assert blocked["active_ticket_job"]["pr_number"] == 11
    assert publisher.created_prs == 1
    assert publisher.closed_issues == []


def test_pr_closed_between_ensure_and_first_live_read_is_recoverable(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": issue(3)})
    states = StateStore(git_repo / ".agent-run")
    state, _ = Controller(
        FixtureGitHubReader(fixture), GitRepository(git_repo), states
    ).start(1)
    checkout = git_repo / ".agent-run" / "worktrees" / state["run_id"] / "ticket-3"
    publisher = ClosesAfterEnsurePublisher(git_repo)
    engine = TicketDeliveryEngine(
        git=GitRepository(git_repo),
        states=states,
        github=publisher,
        agents=PassAgents(checkout),
    )

    blocked = engine.deliver(state["run_id"])

    assert blocked["status"] == "blocked"
    assert blocked["diagnostics"][0]["code"] == "ticket_pr_closed_unmerged"
    assert blocked["active_ticket_job"]["blocked_reason"] == (
        "ticket_pr_closed_unmerged"
    )
    assert blocked["active_ticket_job"]["pr_number"] == 11
    assert publisher.created_prs == 1
    projected = engine.deliver(state["run_id"])

    assert projected["status"] == "blocked"
    assert projected["diagnostics"] == [
        {
            "code": "ticket_pr_closed_unmerged",
            "message": "Current Ticket PR was closed without merging",
            "ticket_number": 3,
        }
    ]
    assert projected["active_ticket_job"]["pr_number"] == 11
    assert publisher.created_prs == 1
    resumed, _ = Controller(
        FixtureGitHubReader(fixture), GitRepository(git_repo), states
    ).resume(state["run_id"])
    assert resumed["status"] == "progress_exhausted"
    assert resumed["active_ticket_job"] is None
    assert resumed["diagnostics"] == [
        {
            "code": "no_executable_ticket",
            "message": "No open, ready and unblocked Ticket is executable",
            "remaining_tickets": [
                {
                    "ticket_number": 3,
                    "reason": "ticket_pr_closed_unmerged",
                }
            ],
        }
    ]
    assert publisher.closed_issues == []


def test_integrated_branch_waits_for_pr_merged_state_without_remerging(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": issue(3)})
    states = StateStore(git_repo / ".agent-run")
    state, _ = Controller(
        FixtureGitHubReader(fixture), GitRepository(git_repo), states
    ).start(1)
    checkout = git_repo / ".agent-run" / "worktrees" / state["run_id"] / "ticket-3"
    publisher = DelayedMergedStatePublisher(git_repo)
    engine = TicketDeliveryEngine(
        git=GitRepository(git_repo),
        states=states,
        github=publisher,
        agents=PassAgents(checkout),
    )

    waiting = engine.deliver(state["run_id"])
    assert waiting["status"] == "waiting_merge"
    still_waiting = engine.deliver(state["run_id"])
    assert still_waiting["status"] == "waiting_merge"
    completed = engine.deliver(state["run_id"])

    assert completed["status"] == "ticket_completed"
    assert completed["active_ticket_job"]["modification_attempts"] == 1
    assert publisher.closed_issues == [3]


def test_saved_distinct_integrated_sha_waits_while_live_pr_is_stale_open(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": issue(3)})
    states = StateStore(git_repo / ".agent-run")
    state, _ = Controller(
        FixtureGitHubReader(fixture), GitRepository(git_repo), states
    ).start(1)
    checkout = git_repo / ".agent-run" / "worktrees" / state["run_id"] / "ticket-3"
    publisher = SavedIntegratedOpenPublisher(git_repo)
    engine = TicketDeliveryEngine(
        git=GitRepository(git_repo),
        states=states,
        github=publisher,
        agents=PassAgents(checkout),
    )

    waiting = engine.deliver(state["run_id"])

    job = waiting["active_ticket_job"]
    assert waiting["status"] == "waiting_merge"
    assert job["integrated_sha"] != job["merge_intent"]["head_sha"]
    assert publisher.merge_calls == 1

    recovered = engine.deliver(state["run_id"])

    assert recovered["status"] == "waiting_merge"
    assert publisher.merge_calls == 1
    assert publisher.closed_issues == []


def test_mismatched_integrated_result_does_not_close_primary_ticket(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": issue(3)})
    states = StateStore(git_repo / ".agent-run")
    state, _ = Controller(
        FixtureGitHubReader(fixture), GitRepository(git_repo), states
    ).start(1)
    checkout = git_repo / ".agent-run" / "worktrees" / state["run_id"] / "ticket-3"
    publisher = MismatchedMergeResultPublisher(git_repo)

    result = TicketDeliveryEngine(
        git=GitRepository(git_repo),
        states=states,
        github=publisher,
        agents=PassAgents(checkout),
    ).deliver(state["run_id"])

    assert result["status"] == "blocked"
    assert result["diagnostics"][0]["code"] == "merged_result_mismatch"
    assert publisher.closed_issues == []


def test_merge_window_base_drift_does_not_close_primary_ticket(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": issue(3)})
    states = StateStore(git_repo / ".agent-run")
    state, _ = Controller(
        FixtureGitHubReader(fixture), GitRepository(git_repo), states
    ).start(1)
    checkout = git_repo / ".agent-run" / "worktrees" / state["run_id"] / "ticket-3"
    publisher = BaseMovesDuringMergePublisher(git_repo)

    result = TicketDeliveryEngine(
        git=GitRepository(git_repo),
        states=states,
        github=publisher,
        agents=PassAgents(checkout),
    ).deliver(state["run_id"])

    assert result["status"] == "blocked"
    assert result["diagnostics"][0]["code"] == "merged_result_mismatch"
    assert publisher.closed_issues == []


def test_merge_window_base_drift_is_rejected_after_response_loss(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": issue(3)})
    states = StateStore(git_repo / ".agent-run")
    state, _ = Controller(
        FixtureGitHubReader(fixture), GitRepository(git_repo), states
    ).start(1)
    checkout = git_repo / ".agent-run" / "worktrees" / state["run_id"] / "ticket-3"
    publisher = BaseMovesThenMergeResponseIsLostPublisher(git_repo)
    engine = TicketDeliveryEngine(
        git=GitRepository(git_repo),
        states=states,
        github=publisher,
        agents=PassAgents(checkout),
    )

    with pytest.raises(OSError, match="remote merge"):
        engine.deliver(state["run_id"])
    result = engine.deliver(state["run_id"])

    assert result["status"] == "blocked"
    assert result["diagnostics"][0]["code"] == "merged_result_mismatch"
    assert publisher.closed_issues == []


def test_merged_recovery_syncs_local_run_branch_before_ticket_close(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": issue(3)})
    states = StateStore(git_repo / ".agent-run")
    state, _ = Controller(
        FixtureGitHubReader(fixture), GitRepository(git_repo), states
    ).start(1)
    checkout = git_repo / ".agent-run" / "worktrees" / state["run_id"] / "ticket-3"
    publisher = RemoteMergeBeforeLocalSyncPublisher(git_repo)
    engine = TicketDeliveryEngine(
        git=GitRepository(git_repo),
        states=states,
        github=publisher,
        agents=PassAgents(checkout),
    )
    original_run_head = GitRepository(git_repo).resolve(str(state["run_branch"]))

    with pytest.raises(OSError, match="before local Run Branch sync"):
        engine.deliver(state["run_id"])
    assert (
        GitRepository(git_repo).resolve(str(state["run_branch"])) == original_run_head
    )
    completed = engine.deliver(state["run_id"])

    integrated = str(completed["active_ticket_job"]["integrated_sha"])
    assert completed["status"] == "ticket_completed"
    assert GitRepository(git_repo).resolve(str(state["run_branch"])) == integrated
    assert publisher.closed_issues == [3]


def test_post_merge_revision_drift_continues_same_job_with_new_pr(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": issue(3)})
    states = StateStore(git_repo / ".agent-run")
    state, _ = Controller(
        FixtureGitHubReader(fixture), GitRepository(git_repo), states
    ).start(1)
    checkout = git_repo / ".agent-run" / "worktrees" / state["run_id"] / "ticket-3"
    publisher = RevisionDriftsAfterMergePublisher(
        git_repo, "sha256:pending-new-revision"
    )
    agents = RevisionAgents(checkout)
    engine = TicketDeliveryEngine(
        git=GitRepository(git_repo),
        states=states,
        github=publisher,
        agents=agents,
    )
    old_run_head = GitRepository(git_repo).resolve(str(state["run_branch"]))

    with pytest.raises(OSError, match="revision-drifted merge"):
        engine.deliver(state["run_id"])
    interrupted = states.load_run(state["run_id"])
    assert interrupted is not None
    old_effective_revision = str(interrupted["active_ticket_job"]["effective_revision"])
    first_integrated = str(publisher.merged_sha)
    assert old_run_head != first_integrated
    fixture_data = json.loads(fixture.read_text(encoding="utf-8"))
    fixture_data["issues"]["3"][
        "body"
    ] += "\nNew authoritative requirement after merge."
    fixture.write_text(json.dumps(fixture_data), encoding="utf-8")
    refreshed, _ = Controller(
        FixtureGitHubReader(fixture), GitRepository(git_repo), states
    ).resume(state["run_id"])
    new_effective_revision = effective_revision(
        ticket_revision=str(
            refreshed["ticket_graph"]["tickets"]["3"]["content_revision"]
        ),
        parent_revision=str(refreshed["parent"]["revision"]),
        graph_revision=str(refreshed["ticket_graph"]["revision"]),
    )
    publisher.new_revision = new_effective_revision
    publisher.revision_override = new_effective_revision

    drifted = engine.deliver(state["run_id"])

    assert drifted["status"] == "blocked"
    assert drifted["diagnostics"][0]["code"] == "merged_revision_mismatch"
    assert GitRepository(git_repo).resolve(str(state["run_branch"])) == first_integrated
    assert drifted["active_ticket_job"]["pr_number"] == 11
    assert drifted["active_ticket_job"]["superseded_integrations"] == [
        {
            "pr_number": 11,
            "integrated_sha": first_integrated,
            "effective_revision": old_effective_revision,
        }
    ]
    assert publisher.closed_issues == []

    resumed, _ = Controller(
        FixtureGitHubReader(fixture), GitRepository(git_repo), states
    ).resume(state["run_id"])
    assert resumed["status"] == "requeue_required"
    assert resumed["requeue_required"] == {
        "work_subject": "ticket:3",
        "generation": 1,
        "reason": "ticket_requirements_changed",
    }
    prepared, retired = Controller(
        FixtureGitHubReader(fixture), GitRepository(git_repo), states
    ).requeue(state["run_id"])
    assert prepared["status"] == "requeue_required"
    queued = Controller(
        FixtureGitHubReader(fixture), GitRepository(git_repo), states
    ).finalize_requeue(state["run_id"])
    assert queued["status"] == "active"
    assert retired["pr_number"] == 11
    assert retired["effective_revision"] == old_effective_revision
    assert queued["retired_job_generations"] == [retired]


def test_revision_drift_after_merged_save_archives_before_reset(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": issue(3)})
    states = StateStore(git_repo / ".agent-run")
    state, _ = Controller(
        FixtureGitHubReader(fixture), GitRepository(git_repo), states
    ).start(1)
    checkout = git_repo / ".agent-run" / "worktrees" / state["run_id"] / "ticket-3"
    publisher = CrashBeforeClosePublisher(git_repo)
    engine = TicketDeliveryEngine(
        git=GitRepository(git_repo),
        states=states,
        github=publisher,
        agents=RevisionAgents(checkout),
    )

    with pytest.raises(OSError, match="before Primary Ticket close"):
        engine.deliver(state["run_id"])
    interrupted = states.load_run(state["run_id"])
    assert interrupted is not None
    old_job = interrupted["active_ticket_job"]
    assert old_job["phase"] == "merged"
    assert old_job["ticket_close_intent"]["actor"] == "scripted-publisher"
    assert len(publisher.prepared_ticket_closes) == 1
    old_pr_number = int(old_job["pr_number"])
    old_integrated_sha = str(old_job["integrated_sha"])
    old_effective_revision = str(old_job["effective_revision"])
    new_ticket_revision = "sha256:after-merged-save"
    new_effective_revision = effective_revision(
        ticket_revision=new_ticket_revision,
        parent_revision=str(interrupted["parent"]["revision"]),
        graph_revision=str(interrupted["ticket_graph"]["revision"]),
    )
    interrupted["ticket_graph"]["tickets"]["3"][
        "content_revision"
    ] = new_ticket_revision
    states.save_run(state["run_id"], interrupted)
    publisher.pr_number = 12
    publisher.merged_sha = None
    publisher.merged_head = None
    publisher.revision_override = new_effective_revision

    completed = engine.deliver(state["run_id"])

    expected_archive = [
        {
            "pr_number": old_pr_number,
            "integrated_sha": old_integrated_sha,
            "effective_revision": old_effective_revision,
        }
    ]
    assert completed["status"] == "ticket_completed"
    assert completed["active_ticket_job"]["pr_number"] == 12
    assert completed["active_ticket_job"]["superseded_integrations"] == (
        expected_archive
    )
    assert publisher.created_prs == 2
    assert publisher.closed_issues == [3]
    assert publisher.prepared_ticket_closes == [
        {"pr_number": old_pr_number, "integrated_sha": old_integrated_sha},
        {
            "pr_number": 12,
            "integrated_sha": completed["active_ticket_job"]["integrated_sha"],
        },
    ]

    repeated = engine.deliver(state["run_id"])

    assert repeated["active_ticket_job"]["superseded_integrations"] == (
        expected_archive
    )
    assert publisher.created_prs == 2
    assert publisher.closed_issues == [3]


def test_transient_sync_git_error_remains_recoverable(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": issue(3)})
    states = StateStore(git_repo / ".agent-run")
    state, _ = Controller(
        FixtureGitHubReader(fixture), GitRepository(git_repo), states
    ).start(1)
    checkout = git_repo / ".agent-run" / "worktrees" / state["run_id"] / "ticket-3"
    publisher = SyncFailsOncePublisher(git_repo)
    engine = TicketDeliveryEngine(
        git=GitRepository(git_repo),
        states=states,
        github=publisher,
        agents=PassAgents(checkout),
    )

    with pytest.raises(GitError, match="transient fetch failure"):
        engine.deliver(state["run_id"])
    interrupted = states.load_run(state["run_id"])
    assert interrupted is not None
    job = interrupted["active_ticket_job"]
    assert job["phase"] == "merging"
    assert job["integrated_sha"]
    completed = engine.deliver(state["run_id"])

    assert completed["status"] == "ticket_completed"
    assert publisher.sync_attempts == 2
    assert publisher.closed_issues == [3]


def test_publication_worker_failure_resumes_from_persisted_candidate(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": issue(3)})
    states = StateStore(git_repo / ".agent-run")
    state, _ = Controller(
        FixtureGitHubReader(fixture), GitRepository(git_repo), states
    ).start(1)
    checkout = git_repo / ".agent-run" / "worktrees" / state["run_id"] / "ticket-3"
    agents = PublicationFailsOnceAgents(checkout)
    engine = TicketDeliveryEngine(
        git=GitRepository(git_repo),
        states=states,
        github=ScriptedPublisher(git_repo),
        agents=agents,
    )

    with pytest.raises(ValueError, match="simulated Publication timeout"):
        engine.deliver(state["run_id"])

    failed = states.load_run(str(state["run_id"]))
    assert failed is not None
    assert failed["active_ticket_job"]["phase"] == "accepted"

    completed = engine.deliver(state["run_id"])

    assert completed["status"] == "ticket_completed"
    assert completed["active_ticket_job"]["modification_attempts"] == 1
    assert agents.development_thread_ids == [None]
    assert len(agents.publication_requests) == 1


def test_publication_worker_failure_is_not_retried_or_marked_pending(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": issue(3)})
    states = StateStore(git_repo / ".agent-run")
    state, _ = Controller(
        FixtureGitHubReader(fixture), GitRepository(git_repo), states
    ).start(1)
    checkout = git_repo / ".agent-run" / "worktrees" / state["run_id"] / "ticket-3"
    agents = PublicationFailsUntilPendingAgents(checkout)
    engine = TicketDeliveryEngine(
        git=GitRepository(git_repo),
        states=states,
        github=ScriptedPublisher(git_repo),
        agents=agents,
    )

    with pytest.raises(ValueError, match="simulated Publication timeout"):
        engine.deliver(state["run_id"])

    failed = states.load_run(str(state["run_id"]))
    assert failed is not None
    job = failed["active_ticket_job"]
    assert job["phase"] == "accepted"
    assert job["publication_attempts"] == 1
    assert job["modification_attempts"] == 1
    assert job["acceptance_artifact"]["checks"] == {
        "e2e": {
            "status": "pass",
            "evidence": E2E_PASS_EVIDENCE,
            "findings": [],
        },
        "standards": {
            "status": "pass",
            "evidence": STANDARDS_PASS_EVIDENCE,
            "findings": [],
        },
        "spec": {
            "status": "pass",
            "evidence": SPEC_PASS_EVIDENCE,
            "findings": [],
        },
    }
    assert agents.development_thread_ids == [None]
    assert agents.review_count == 1
    assert agents.publication_calls == 1
    assert [request.get("thread_id") for request in agents.publication_requests] == [
        "development-thread-1"
    ]


def test_required_checks_timeout_waits_without_starting_a_repair(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": issue(3)})
    states = StateStore(git_repo / ".agent-run")
    state, _ = Controller(
        FixtureGitHubReader(fixture), GitRepository(git_repo), states
    ).start(1)
    checkout = git_repo / ".agent-run" / "worktrees" / state["run_id"] / "ticket-3"
    agents = PassAgents(checkout)
    publisher = CheckReadFailsOncePublisher(git_repo)
    engine = TicketDeliveryEngine(
        git=GitRepository(git_repo),
        states=states,
        github=publisher,
        agents=agents,
    )

    waiting = engine.deliver(state["run_id"])

    job = waiting["active_ticket_job"]
    assert waiting["status"] == "waiting_external"
    assert job["phase"] == "publishing"
    assert "repair_source" not in job
    assert job["modification_attempts"] == 1
    assert agents.development_thread_ids == [None]
    assert agents.review_count == 1
    assert len(agents.publication_requests) == 1

    completed = engine.deliver(state["run_id"])

    assert completed["status"] == "ticket_completed"
    assert completed["active_ticket_job"]["modification_attempts"] == 1
    assert agents.development_thread_ids == [None]
    assert agents.review_count == 1


def test_publication_worker_failure_rechecks_base_before_resume(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": issue(3)})
    states = StateStore(git_repo / ".agent-run")
    state, _ = Controller(
        FixtureGitHubReader(fixture), GitRepository(git_repo), states
    ).start(1)
    checkout = git_repo / ".agent-run" / "worktrees" / state["run_id"] / "ticket-3"
    agents = PublicationFailsUntilPendingAgents(checkout)
    engine = TicketDeliveryEngine(
        git=GitRepository(git_repo),
        states=states,
        github=ScriptedPublisher(git_repo),
        agents=agents,
    )

    with pytest.raises(ValueError, match="simulated Publication timeout"):
        engine.deliver(state["run_id"])
    failed = states.load_run(str(state["run_id"]))
    assert failed is not None
    old_candidate = str(failed["active_ticket_job"]["candidate_sha"])
    new_base = _advance_branch_with_same_tree(git_repo, str(state["run_branch"]))
    agents.fail = False

    completed = engine.deliver(state["run_id"])

    job = completed["active_ticket_job"]
    assert completed["status"] == "ticket_completed"
    assert job["base_sha"] == new_base
    assert job["candidate_sha"] != old_candidate
    assert job["acceptance_record"]["reviewed_base_sha"] == new_base
    assert job["modification_attempts"] == 2
    assert agents.development_thread_ids == [None, "development-thread-1"]
    assert agents.review_count == 2


def test_publication_resume_rechecks_base_before_next_attempt(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": issue(3)})
    states = StateStore(git_repo / ".agent-run")
    state, _ = Controller(
        FixtureGitHubReader(fixture), GitRepository(git_repo), states
    ).start(1)
    checkout = git_repo / ".agent-run" / "worktrees" / state["run_id"] / "ticket-3"
    agents = PublicationDriftsBaseThenSucceedsAgents(
        checkout, git_repo, str(state["run_branch"])
    )
    engine = TicketDeliveryEngine(
        git=GitRepository(git_repo),
        states=states,
        github=ScriptedPublisher(git_repo),
        agents=agents,
    )

    with pytest.raises(ValueError, match="simulated Publication timeout"):
        engine.deliver(state["run_id"])
    result = engine.deliver(state["run_id"])

    job = result["active_ticket_job"]
    assert result["status"] == "ticket_completed"
    assert job["modification_attempts"] == 2
    assert job["validation_attempts"] == 1
    assert job["reviewer_thread_ids"] == ["reviewer-1", "reviewer-2"]
    assert agents.publication_calls == 2
    assert agents.publication_requests[0]["candidate_sha"] != job["candidate_sha"]
    assert agents.publication_requests[1]["candidate_sha"] == job["candidate_sha"]


def test_publication_success_rechecks_base_before_creating_a_commit(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": issue(3)})
    states = StateStore(git_repo / ".agent-run")
    state, _ = Controller(
        FixtureGitHubReader(fixture), GitRepository(git_repo), states
    ).start(1)
    checkout = git_repo / ".agent-run" / "worktrees" / state["run_id"] / "ticket-3"
    agents = PublicationSucceedsDuringBaseDriftAgents(
        checkout, git_repo, str(state["run_branch"])
    )
    result = TicketDeliveryEngine(
        git=GitRepository(git_repo),
        states=states,
        github=ScriptedPublisher(git_repo),
        agents=agents,
    ).deliver(state["run_id"])

    job = result["active_ticket_job"]
    assert result["status"] == "ticket_completed"
    assert job["modification_attempts"] == 2
    assert job["reviewer_thread_ids"] == ["reviewer-1", "reviewer-2"]
    assert agents.publication_calls == 2
    assert agents.publication_requests[0]["candidate_sha"] != job["candidate_sha"]
    assert agents.publication_requests[1]["candidate_sha"] == job["candidate_sha"]


def test_existing_pr_recovers_after_publication_pending_base_drift(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": issue(3)})
    states = StateStore(git_repo / ".agent-run")
    state, _ = Controller(
        FixtureGitHubReader(fixture), GitRepository(git_repo), states
    ).start(1)
    checkout = git_repo / ".agent-run" / "worktrees" / state["run_id"] / "ticket-3"
    agents = CheckRepairAgents(checkout)
    publisher = PublicationContextFailsUntilResumedPublisher(git_repo)
    publisher.checks = ["fail", "pass"]
    engine = TicketDeliveryEngine(
        git=GitRepository(git_repo),
        states=states,
        github=publisher,
        agents=agents,
    )

    pending = engine.deliver(state["run_id"])

    pending_job = pending["active_ticket_job"]
    old_published_sha = str(pending_job["published_sha"])
    assert pending["status"] == "publication_pending"
    assert pending_job["pr_number"] == publisher.pr_number
    assert publisher.live_head == old_published_sha
    _advance_branch_with_same_tree(git_repo, str(state["run_branch"]))
    publisher.fail_publication_context = False

    completed = engine.deliver(state["run_id"])

    assert completed["status"] == "ticket_completed"
    assert completed["active_ticket_job"]["pr_number"] == publisher.pr_number
    assert publisher.live_head == completed["active_ticket_job"]["publication_sha"]


def test_acceptance_repair_base_drift_rebuilds_without_stale_repair_input(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": issue(3)})
    states = StateStore(git_repo / ".agent-run")
    state, _ = Controller(
        FixtureGitHubReader(fixture), GitRepository(git_repo), states
    ).start(1)
    checkout = git_repo / ".agent-run" / "worktrees" / state["run_id"] / "ticket-3"
    agents = AcceptanceRepairPublicationFailsUntilResumedAgents(checkout)
    engine = TicketDeliveryEngine(
        git=GitRepository(git_repo),
        states=states,
        github=ScriptedPublisher(git_repo),
        agents=agents,
    )

    with pytest.raises(
        ValueError, match="simulated acceptance repair Publication timeout"
    ):
        engine.deliver(state["run_id"])
    failed = states.load_run(str(state["run_id"]))
    assert failed is not None
    assert failed["active_ticket_job"]["repair_source"] == "acceptance"
    _advance_branch_with_same_tree(git_repo, str(state["run_branch"]))
    agents.fail_publication = False
    completed = engine.deliver(state["run_id"])

    assert completed["status"] == "ticket_completed"
    assert completed["active_ticket_job"]["modification_attempts"] == 3
    assert "repair_source" not in agents.development_requests[-1]
    assert "acceptance_artifact" not in agents.development_requests[-1]


def _tree(repository: Path, sha: str) -> str:
    return subprocess.run(
        ["git", "rev-parse", f"{sha}^{{tree}}"],
        cwd=repository,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()


def _advance_branch_with_same_tree(repository: Path, branch: str) -> str:
    parent = subprocess.run(
        ["git", "rev-parse", branch],
        cwd=repository,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    advanced = subprocess.run(
        [
            "git",
            "commit-tree",
            f"{parent}^{{tree}}",
            "-p",
            parent,
            "-m",
            "chore(run): concurrent same-tree update",
        ],
        cwd=repository,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    subprocess.run(
        ["git", "update-ref", f"refs/heads/{branch}", advanced, parent],
        cwd=repository,
        check=True,
    )
    return advanced


def test_candidate_commit_crash_recovers_without_new_development_attempt(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": issue(3)})
    states = StateStore(git_repo / ".agent-run")
    state, _ = Controller(
        FixtureGitHubReader(fixture), GitRepository(git_repo), states
    ).start(1)
    checkout = git_repo / ".agent-run" / "worktrees" / state["run_id"] / "ticket-3"
    agents = PassAgents(checkout)
    crash_git = CrashAfterCandidateGit(git_repo)
    engine = TicketDeliveryEngine(
        git=crash_git,
        states=states,
        github=ScriptedPublisher(git_repo),
        agents=agents,
    )

    with pytest.raises(OSError, match="after Candidate"):
        engine.deliver(state["run_id"])
    interrupted = states.load_run(state["run_id"])
    assert interrupted is not None
    assert interrupted["active_ticket_job"]["phase"] == "committing_candidate"
    completed = engine.deliver(state["run_id"])

    assert completed["status"] == "ticket_completed"
    assert completed["active_ticket_job"]["modification_attempts"] == 1
    assert agents.development_thread_ids == [None]


def test_external_merge_without_persisted_intent_does_not_close_ticket(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": issue(3)})
    states = StateStore(git_repo / ".agent-run")
    state, _ = Controller(
        FixtureGitHubReader(fixture), GitRepository(git_repo), states
    ).start(1)
    checkout = git_repo / ".agent-run" / "worktrees" / state["run_id"] / "ticket-3"
    publisher = ExternalMergePublisher(git_repo)

    result = TicketDeliveryEngine(
        git=GitRepository(git_repo),
        states=states,
        github=publisher,
        agents=PassAgents(checkout),
    ).deliver(state["run_id"])

    assert result["status"] == "blocked"
    assert result["diagnostics"][0]["code"] == "unexpected_external_merge"
    assert result["active_ticket_job"]["blocked_reason"] == (
        "unexpected_external_merge"
    )
    assert publisher.closed_issues == []


def test_unknown_persisted_phase_fails_instead_of_spinning(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": issue(3)})
    states = StateStore(git_repo / ".agent-run")
    state, _ = Controller(
        FixtureGitHubReader(fixture), GitRepository(git_repo), states
    ).start(1)
    engine = TicketDeliveryEngine(
        git=GitRepository(git_repo),
        states=states,
        github=ScriptedPublisher(git_repo),
        agents=PassAgents(git_repo),
    )
    engine._job(state)["phase"] = "corrupt"
    states.save_run(state["run_id"], state)

    with pytest.raises(ValueError, match="unknown Ticket phase"):
        engine.deliver(state["run_id"])


def test_remote_branch_drift_is_not_force_overwritten(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": issue(3)})
    states = StateStore(git_repo / ".agent-run")
    state, _ = Controller(
        FixtureGitHubReader(fixture), GitRepository(git_repo), states
    ).start(1)
    checkout = git_repo / ".agent-run" / "worktrees" / state["run_id"] / "ticket-3"
    publisher = ScriptedPublisher(git_repo)
    publisher.checks = ["pending", "pass"]
    engine = TicketDeliveryEngine(
        git=GitRepository(git_repo),
        states=states,
        github=publisher,
        agents=PassAgents(checkout),
    )
    waiting = engine.deliver(state["run_id"])
    assert waiting["status"] == "waiting_checks"
    publisher.live_head = "f" * 40

    with pytest.raises(ValueError, match="remote ticket branch drifted"):
        engine.deliver(state["run_id"])

    assert publisher.live_head == "f" * 40
    assert publisher.closed_issues == []


def test_lost_escalation_response_cannot_return_to_acceptance(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": issue(3)})
    states = StateStore(git_repo / ".agent-run")
    state, _ = Controller(
        FixtureGitHubReader(fixture), GitRepository(git_repo), states
    ).start(1)
    checkout = git_repo / ".agent-run" / "worktrees" / state["run_id"] / "ticket-3"
    publisher = CrashAfterEscalationPublisher(git_repo)
    engine = TicketDeliveryEngine(
        git=GitRepository(git_repo),
        states=states,
        github=publisher,
        agents=AlwaysRejectAgents(checkout),
    )

    with pytest.raises(OSError, match="lost escalation"):
        engine.deliver(state["run_id"])
    interrupted = states.load_run(state["run_id"])
    assert interrupted is not None
    interrupted_job = interrupted["active_ticket_job"]
    assert interrupted_job["phase"] == "escalating"
    fixture_data = json.loads(fixture.read_text(encoding="utf-8"))
    fixture_data["issues"]["3"]["labels"].append("ready-for-human")
    fixture.write_text(json.dumps(fixture_data), encoding="utf-8")

    resumed, _ = Controller(
        FixtureGitHubReader(fixture), GitRepository(git_repo), states
    ).resume(state["run_id"])

    assert resumed["active_ticket_job"]["ticket_number"] == 3
    assert resumed["active_ticket_job"]["phase"] == "escalating"
    assert resumed["active_ticket_job"] == interrupted_job
    assert resumed["ticket_jobs"]["3"] == resumed["active_ticket_job"]
    recovery_agents = PassAgents(checkout)
    result = TicketDeliveryEngine(
        git=GitRepository(git_repo),
        states=states,
        github=publisher,
        agents=recovery_agents,
    ).deliver(state["run_id"])

    assert result["status"] == "blocked"
    assert result["active_ticket_job"]["phase"] == "blocked"
    assert result["active_ticket_job"]["blocked_reason"] == (
        "modification_budget_exhausted"
    )
    assert result["ticket_jobs"]["3"] == result["active_ticket_job"]
    assert recovery_agents.development_requests == []
    assert publisher.created_prs == 0
    assert publisher.closed_issues == []

    projected, _ = Controller(
        FixtureGitHubReader(fixture), GitRepository(git_repo), states
    ).resume(state["run_id"])
    assert projected["active_ticket_job"] is None
    assert projected["ticket_jobs"]["3"]["phase"] == "blocked"
    assert projected["status"] == "progress_exhausted"
    assert projected["diagnostics"] == [
        {
            "code": "no_executable_ticket",
            "message": "No open, ready and unblocked Ticket is executable",
            "remaining_tickets": [
                {
                    "ticket_number": 3,
                    "reason": "modification_budget_exhausted",
                }
            ],
        }
    ]


def test_resume_switches_frontier_without_deleting_blocked_ticket_job(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={
            "3": issue(3),
            "4": {**issue(4), "state": "CLOSED"},
            "5": issue(5),
        },
    )
    states = StateStore(git_repo / ".agent-run")
    controller = Controller(
        FixtureGitHubReader(fixture), GitRepository(git_repo), states
    )
    state, _ = controller.start(1)
    old_job = state["active_ticket_job"]
    old_job.update(
        {
            "phase": "blocked",
            "blocked_reason": "reviewer_requires_human",
            "human_blockers": ["A maintainer must resolve the review block."],
            "development_thread_id": "developer-3",
            "reviewer_thread_ids": ["standards-3", "spec-3"],
            "pr_number": 33,
            "merge_intent": {"expected_head_sha": "a" * 40},
        }
    )
    state["ticket_jobs"]["4"] = {
        "ticket_number": 4,
        "phase": "completed",
        "development_thread_id": "developer-4",
        "pr_number": 44,
        "integrated_sha": "b" * 40,
    }
    states.save_run(state["run_id"], state)
    fixture_data = json.loads(fixture.read_text(encoding="utf-8"))
    fixture_data["issues"]["3"]["labels"].append("ready-for-human")
    fixture.write_text(json.dumps(fixture_data), encoding="utf-8")

    resumed, _ = Controller(
        FixtureGitHubReader(fixture), GitRepository(git_repo), states
    ).resume(state["run_id"])

    assert resumed["active_ticket_job"]["ticket_number"] == 5
    assert resumed["ticket_jobs"]["3"] == old_job
    assert resumed["ticket_jobs"]["3"]["blocked_reason"] == ("reviewer_requires_human")
    assert resumed["ticket_jobs"]["3"]["development_thread_id"] == ("developer-3")
    assert resumed["ticket_jobs"]["3"]["reviewer_thread_ids"] == [
        "standards-3",
        "spec-3",
    ]
    assert resumed["ticket_jobs"]["3"]["pr_number"] == 33
    assert resumed["ticket_jobs"]["3"]["merge_intent"] == {
        "expected_head_sha": "a" * 40
    }
    assert resumed["ticket_jobs"]["4"] == {
        "ticket_number": 4,
        "phase": "completed",
        "development_thread_id": "developer-4",
        "pr_number": 44,
        "integrated_sha": "b" * 40,
    }
