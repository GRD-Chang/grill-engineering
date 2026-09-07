from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from copy import deepcopy
from dataclasses import FrozenInstanceError, fields
from inspect import signature
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import agent_run.change_delivery as change_delivery_module

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
from agent_run.change_delivery import (
    ChangeDeliveryAdapter,
    ChangeDeliveryEngine,
    ChangeJobContract,
    fallback_publication_context,
)
from agent_run.controller import Controller
from agent_run.delivery import TicketDeliveryEngine
from agent_run.delivery_policy import DeliveryPolicy
from agent_run.delivery_loop import TicketDeliveryAdapter
from agent_run.git import GitError, GitRepository
from agent_run.github import GitHubReadError, MergeOutcomeUnknownError
from agent_run.github_publish import GhGitHubPublisher
from agent_run.github_fixture import FixtureGitHubReader
from agent_run.parent_delivery_loop import ParentDeliveryAdapter, ParentDeliveryLoop
from agent_run.revisions import effective_revision
from agent_run.requeue import requeue_change_job
from agent_run.state import SimulatedProcessCrash, StateStore
from agent_run.state_contract import IncompatibleRunStateError, require_current_run_state
from agent_run.run_repair_delivery import RunRepairAdapter
from agent_run.review_budget import TICKET_POLICY

from conftest import write_fixture


E2E_PASS_EVIDENCE = "操作或命令：执行公开候选流程；退出码：0；结果：候选通过端到端复验。"
E2E_FAIL_EVIDENCE = "执行公开候选流程后，delivered.txt 缺少修复标记。"
STANDARDS_PASS_EVIDENCE = "审查范围或基线：仓库编码规范与候选 diff；结论：未发现违反项。"
SPEC_PASS_EVIDENCE = "已核对的验收标准：Ticket 的全部验收标准；覆盖结论：候选完整覆盖。"


def test_change_delivery_seams_keep_facts_and_semantics_separate() -> None:
    contract = ChangeJobContract(
        label="ticket-1",
        branch="ticket/1",
        base_branch="run/main",
    )
    assert (contract.label, contract.branch, contract.base_branch) == (
        "ticket-1",
        "ticket/1",
        "run/main",
    )
    with pytest.raises(FrozenInstanceError):
        contract.label = "mutated"  # type: ignore[misc]

    assert not hasattr(ChangeDeliveryAdapter, "__dataclass_fields__")
    assert {
        "development_request",
        "publication_request",
        "review_request",
        "acceptance_record",
        "acceptance_is_current",
    } <= set(vars(ChangeDeliveryAdapter))
    assert set(signature(ChangeDeliveryEngine.__init__).parameters) == {
        "self",
        "git",
        "github",
        "agents",
        "contract",
        "adapter",
        "publisher",
        "state_store",
    }
    for concrete in (
        TicketDeliveryAdapter,
        ParentDeliveryAdapter,
        RunRepairAdapter,
    ):
        assert "owner" not in signature(concrete.__init__).parameters
        assert concrete.development_request is not ChangeDeliveryAdapter.development_request
        assert concrete.publication_request is not ChangeDeliveryAdapter.publication_request
        assert concrete.review_request is not ChangeDeliveryAdapter.review_request


def test_fallback_publication_context_is_a_minimal_non_null_projection() -> None:
    receipt = {
        "base_sha": "base",
        "candidate_sha": "candidate",
        "candidate_tree": "tree",
        "review_artifacts": [
            {
                "review_identity": {
                    "reviewed_base_sha": "base",
                    "reviewed_candidate_sha": "previous",
                    "reviewed_candidate_tree": "previous-tree",
                }
            }
        ],
        "code_delta": ["M delivered.txt"],
        "repair_delta": ["M delivered.txt"],
        "repair_source": "acceptance",
        "failure_evidence_source": "acceptance",
        "git_integrity": {"status": "pass"},
        "last_acceptance_artifact": {
            "checks": {
                "e2e": {"status": "pass"},
                "standards": {"status": "fail"},
            }
        },
        "review_budget": {"reviewer_invocations": 3},
        "currentness": {"is_current": True},
        "next_phase": "publication",
    }

    context = fallback_publication_context(receipt)

    assert context == {
        "last_review_identity": {
            "reviewed_base_sha": "base",
            "reviewed_candidate_sha": "previous",
            "reviewed_candidate_tree": "previous-tree",
        },
        "current_candidate_identity": {
            "base_sha": "base",
            "candidate_sha": "candidate",
            "candidate_tree": "tree",
        },
        "development_delta": True,
        "current_candidate_has_additional_review": False,
        "candidate_delta": ["M delivered.txt"],
        "repair_delta": ["M delivered.txt"],
        "repair_source": "acceptance",
        "failure_evidence_source": "acceptance",
        "git_integrity": {"status": "pass"},
        "last_review_lane_statuses": {"e2e": "pass", "standards": "fail"},
    }
    assert "previous_publication_authority" not in context
    assert all(value is not None for value in context.values())

    minimal = fallback_publication_context(
        {"base_sha": "base", "candidate_sha": "candidate"}
    )
    assert minimal == {
        "current_candidate_identity": {
            "base_sha": "base",
            "candidate_sha": "candidate",
        },
        "current_candidate_has_additional_review": False,
    }


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
    engine.git = SimpleNamespace(
        resolve=lambda revision: (
            expected_boundary["candidate_tree"]
            if revision.endswith("^{tree}")
            else revision
        )
    )
    engine.save = lambda value: value
    invocation_identity = None
    if isinstance(job.get("repair_generation"), int):
        invocation_identity = lambda state, job: (
            f"run-repair:{state['run_id']}",
            int(job["repair_generation"]),
        )
    engine.contract = SimpleNamespace()
    engine.adapter = SimpleNamespace(
        save=lambda value: value,
        invocation_identity=(
            invocation_identity or ChangeDeliveryEngine._invocation_identity
        ),
        sync_attempts=lambda _state, _job: None,
        stop_after_review=lambda _state, _job: False,
        additional_agent_base_shas=lambda _state, _job: set(),
    )
    request = {"acceptance_scope": "test", "_callback": object()}

    semantic_attempt = {
        "attempt_id": "semantic-attempt",
        "role": "publication",
    }
    event = engine._invocation_events(
        state,
        job,
        request,
        phase="publication",
        semantic_attempt=semantic_attempt,
    )
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
        self.branch: str | None = None
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
                    "state": "FAILURE",
                    "repairability": "code_failure",
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
        expected_base_sha: str,
        expected_remote_sha: str,
        recovery_remote_sha: str,
    ) -> None:
        del (
            ticket_number, branch, base_branch, expected_base_sha,
            expected_remote_sha, recovery_remote_sha,
        )

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

    def verify_ticket_pr_before_publish(
        self,
        *,
        branch: str,
        base_branch: str,
        expected_head_sha: str,
        expected_base_sha: str,
    ) -> None:
        del branch, base_branch, expected_head_sha, expected_base_sha

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
        del expected_head_sha, expected_base_sha
        self.created_prs += 1
        self.branch = branch
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

    def required_checks_snapshot(
        self, pr_number: int, *, expected_head_sha: str
    ) -> dict[str, Any]:
        assert pr_number == self.pr_number
        assert self.live_pull_request(pr_number)["head_sha"] == expected_head_sha
        result = self.checks[min(self.check_position, len(self.checks) - 1)]
        self.check_position += 1
        configured = self.failed_check_evidence.get("checks")
        if result == "fail" and isinstance(configured, list):
            checks = [
                {
                    key: check[key]
                    for key in (
                        "name",
                        "workflow",
                        "bucket",
                        "state",
                        "description",
                        "link",
                    )
                    if key in check
                }
                for check in configured
                if isinstance(check, dict)
            ]
        elif result == "none":
            checks = []
        else:
            checks = [
                {
                    "name": "test",
                    "workflow": "ci",
                    "bucket": result,
                    "link": "https://example.invalid/checks/test",
                }
            ]
        return {
            "pr_number": pr_number,
            "head_sha": expected_head_sha,
            "result": result,
            "checks": checks,
        }

    def required_check_evidence(
        self, pr_number: int, *, expected_head_sha: str | None = None
    ) -> dict[str, Any]:
        del expected_head_sha
        assert pr_number == self.pr_number
        return self.failed_check_evidence

    def live_pull_request(self, pr_number: int) -> dict[str, Any]:
        result = {
            "head_branch": self.branch,
            "head_sha": self.live_head,
            "head_repository": "example/project",
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
            "base_repository": "example/project",
            "mergeable": True,
            "state": "OPEN",
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


class GitIntegrityRepairAgents(ScriptedAgents):
    def __init__(self, checkout: Path, *, commit_message: str = "agent-owned commit") -> None:
        super().__init__(checkout)
        self.agent_commit_done = False
        self.commit_message = commit_message

    def develop(self, request: dict[str, Any]) -> DevelopmentResult:
        self.development_requests.append(request)
        self.development_thread_ids.append(request.get("thread_id"))
        thread_id = str(request.get("thread_id") or "development-thread-1")
        target = self.checkout / "delivered.txt"
        if not self.agent_commit_done:
            target.write_text("agent commit must be repaired\n", encoding="utf-8")
            subprocess.run(["git", "add", "delivered.txt"], cwd=self.checkout, check=True)
            subprocess.run(
                ["git", "commit", "-m", self.commit_message],
                cwd=self.checkout,
                check=True,
                capture_output=True,
            )
            self.agent_commit_done = True
        else:
            target.write_text("managed candidate\n", encoding="utf-8")
        return DevelopmentResult(
            thread_id=thread_id,
            summary="Repaired the managed checkout boundary.",
        )

    def review(self, request: dict[str, Any]) -> ReviewResult:
        return PassAgents.review(self, request)


class CancelledAgents(ScriptedAgents):
    def develop(self, request: dict[str, Any]) -> DevelopmentResult:
        (self.checkout / "README.md").write_text(
            "tracked work before cancellation\n", encoding="utf-8"
        )
        (self.checkout / "untracked.txt").write_text(
            "untracked work before cancellation\n", encoding="utf-8"
        )
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
        self.review_requests.append(request)
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

    def required_checks_snapshot(
        self, pr_number: int, *, expected_head_sha: str
    ) -> dict[str, Any]:
        if self.check_read_failures:
            self.check_read_failures -= 1
            raise TimeoutError("simulated Required Checks timeout")
        return super().required_checks_snapshot(
            pr_number, expected_head_sha=expected_head_sha
        )


class CheckReadFailsNTimesPublisher(ScriptedPublisher):
    def __init__(self, repo: Path, failures: int) -> None:
        super().__init__(repo)
        self.check_read_failures = failures

    def required_checks_snapshot(
        self, pr_number: int, *, expected_head_sha: str
    ) -> dict[str, Any]:
        if self.check_read_failures:
            self.check_read_failures -= 1
            raise TimeoutError("simulated repeated Required Checks timeout")
        return super().required_checks_snapshot(
            pr_number, expected_head_sha=expected_head_sha
        )


class SnapshotReadFailsAfterPendingPublisher(ScriptedPublisher):
    def __init__(self, repo: Path) -> None:
        super().__init__(repo)
        self.checks = ["pending"]
        self.snapshot_calls = 0

    def required_checks_snapshot(
        self, pr_number: int, *, expected_head_sha: str
    ) -> dict[str, Any]:
        self.snapshot_calls += 1
        if self.snapshot_calls == 2:
            raise TimeoutError("simulated repeated snapshot timeout")
        return super().required_checks_snapshot(
            pr_number, expected_head_sha=expected_head_sha
        )


class SnapshotChangesAfterInitialPassPublisher(ScriptedPublisher):
    def required_checks_snapshot(
        self, pr_number: int, *, expected_head_sha: str
    ) -> dict[str, Any]:
        live = self.live_pull_request(pr_number)
        assert live["head_sha"] == expected_head_sha
        return {
            "pr_number": pr_number,
            "head_sha": expected_head_sha,
            "result": "pending",
            "checks": [
                {
                    "name": "tests",
                    "bucket": "pending",
                    "state": "IN_PROGRESS",
                }
            ],
        }


class ContradictorySnapshotPublisher(ScriptedPublisher):
    def __init__(
        self, repo: Path, *, result: str, checks: list[dict[str, str]]
    ) -> None:
        super().__init__(repo)
        self.snapshot_result = result
        self.snapshot_checks = checks

    def required_checks_snapshot(
        self, pr_number: int, *, expected_head_sha: str
    ) -> dict[str, Any]:
        return {
            "pr_number": pr_number,
            "head_sha": expected_head_sha,
            "result": self.snapshot_result,
            "checks": deepcopy(self.snapshot_checks),
        }


class LiveHeadDriftsBeforeFailedEvidencePublisher(ScriptedPublisher):
    def __init__(self, repo: Path) -> None:
        super().__init__(repo)
        self.failed_evidence_calls = 0
        self.drift_live_read = False

    def required_checks_snapshot(
        self, pr_number: int, *, expected_head_sha: str
    ) -> dict[str, Any]:
        result = super().required_checks_snapshot(
            pr_number, expected_head_sha=expected_head_sha
        )
        if result["result"] == "fail":
            self.drift_live_read = True
        return result

    def live_pull_request(self, pr_number: int) -> dict[str, Any]:
        live = super().live_pull_request(pr_number)
        if self.drift_live_read:
            live["head_sha"] = "drifted-head"
        return live

    def required_check_evidence(
        self, pr_number: int, *, expected_head_sha: str | None = None
    ) -> dict[str, Any]:
        self.failed_evidence_calls += 1
        return super().required_check_evidence(
            pr_number, expected_head_sha=expected_head_sha
        )


class CheckEvidenceFailsOncePublisher(ScriptedPublisher):
    def __init__(self, repo: Path, error: BaseException) -> None:
        super().__init__(repo)
        self.checks = ["fail", "fail", "pass"]
        self.evidence_error: BaseException | None = error

    def required_check_evidence(
        self, pr_number: int, *, expected_head_sha: str | None = None
    ) -> dict[str, Any]:
        if self.evidence_error is not None:
            error = self.evidence_error
            self.evidence_error = None
            raise error
        return super().required_check_evidence(
            pr_number, expected_head_sha=expected_head_sha
        )


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


class RequiredCheckThenAcceptanceFindingAgents(CheckRepairAgents):
    def review(self, request: dict[str, Any]) -> ReviewResult:
        result = super().review(request)
        if self.review_count == 2:
            result.artifact["checks"]["e2e"] = {
                "status": "fail",
                "evidence": "The repaired candidate still misses an acceptance requirement.",
                "findings": [
                    "问题：修复候选仍缺少验收标记；证据：独立复验未发现标记；必须修复：补齐验收标记；复验：重新检查候选文件。"
                ],
            }
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
        self,
        checkout: Path,
        *,
        ticket_number: int,
        attempt: int,
        expected_head: str | None = None,
        candidate_intent: dict[str, object] | None = None,
    ) -> str | None:
        candidate = super().commit_candidate(
            checkout,
            ticket_number=ticket_number,
            attempt=attempt,
            expected_head=expected_head,
            candidate_intent=candidate_intent,
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
        expected_head_sha: str,
        expected_base_sha: str,
    ) -> int:
        number = super().ensure_ticket_pr(
            branch=branch,
            base_branch=base_branch,
            title=title,
            body=body,
            primary_ticket=primary_ticket,
            expected_head_sha=expected_head_sha,
            expected_base_sha=expected_base_sha,
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
    assert agents.review_requests[0]["review_budget_context"] == {
        "current_review_attempt": 1,
        "remaining_review_attempts": 2,
    }
    assert agents.development_requests[1]["repair_source"] == "acceptance"
    assert agents.development_requests[1]["review_budget_context"] == {
        "completed_review_attempts": 1,
        "remaining_review_attempts": 2,
    }
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
    assert agents.review_requests[1]["review_budget_context"] == {
        "current_review_attempt": 2,
        "remaining_review_attempts": 1,
    }
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

    waiting = engine.deliver(state["run_id"])

    assert waiting["status"] == "waiting_external"
    assert waiting["active_ticket_job"]["phase"] == "merged"

    persisted = states.load_run(state["run_id"])
    assert persisted is not None
    assert persisted["active_ticket_job"]["phase"] == "merged"
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


def test_fresh_validation_human_resume_with_new_thread_replaces_blocker_artifact(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": issue(3)})
    states = StateStore(git_repo / ".agent-run")
    controller = Controller(
        FixtureGitHubReader(fixture), GitRepository(git_repo), states
    )
    state, _ = controller.start(1)
    checkout = git_repo / ".agent-run" / "worktrees" / state["run_id"] / "ticket-3"
    agents = HumanThenHistoricalReviewerAgents(checkout)
    engine = TicketDeliveryEngine(
        git=GitRepository(git_repo),
        states=states,
        github=ScriptedPublisher(git_repo),
        agents=agents,
    )

    blocked = engine.deliver(str(state["run_id"]))
    blocked_job = blocked["active_ticket_job"]
    assert blocked_job["review_budget"]["reviewer_invocations"] == 1
    assert len(blocked_job["review_budget"]["review_artifacts"]) == 1

    resumed, _ = controller.resume(
        str(state["run_id"]),
        resume_human_blocker=True,
    )
    resumed_job = resumed["ticket_jobs"]["3"]
    resumed_job["review_new_thread"] = True
    resumed["active_ticket_job"] = resumed_job
    states.save_run(str(state["run_id"]), resumed)
    engine.deliver(str(state["run_id"]))

    persisted = states.load_run(str(state["run_id"]))
    assert persisted is not None
    assert "previous_acceptance_artifact" not in agents.review_requests[1]
    assert "previous_review_identity" not in agents.review_requests[1]
    assert agents.review_requests[1]["review_budget_context"] == {
        "current_review_attempt": 1,
        "remaining_review_attempts": 2,
    }
    completed_job = persisted["ticket_jobs"]["3"]
    assert completed_job["review_budget"]["reviewer_invocations"] == 1
    assert len(completed_job["review_budget"]["review_artifacts"]) == 1
    assert completed_job["review_budget"]["review_artifacts"][0][
        "reviewer_thread_id"
    ] == "older-reviewer"
    assert completed_job["review_budget"]["review_artifacts"][0]["artifact"] == (
        completed_job["acceptance_artifact"]
    )


def test_ticket_review_budget_fallback_publishes_without_acceptance_record(
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
    github.checks = ["none"]
    agents = AlwaysRejectAgents(checkout)

    result = TicketDeliveryEngine(
        git=GitRepository(git_repo),
        states=states,
        github=github,
        agents=agents,
    ).deliver(state["run_id"])

    assert result["status"] == "ticket_completed"
    job = result["active_ticket_job"]
    assert job["modification_attempts"] == 4
    assert job["review_budget"]["development_attempts"] == 4
    assert job["review_budget"]["reviewer_invocations"] == 3
    assert job["publication_authority"] == "fallback"
    assert "acceptance_record" not in job
    assert job["fallback_publication_receipt"]["reviewer_invocations"] == 3
    fallback_publication = agents.publication_requests[0]
    assert "fallback_publication_context" in fallback_publication
    assert "fallback_receipt" not in fallback_publication
    assert "acceptance_artifact" not in fallback_publication
    assert "ci_evidence" not in fallback_publication
    assert job["deterministic_integration_record"]["source"] == "fallback"
    assert "required_checks" not in job
    assert "required_checks_mode" not in job
    assert "required_checks" not in job["deterministic_integration_record"]
    assert "required_checks_mode" not in job["deterministic_integration_record"]
    integration = job["deterministic_integration_record"]
    assert integration["integrated_sha"] == job["integrated_sha"]
    assert integration["integrated_publication_sha"] == job["publication_sha"]
    assert integration["integrated_tree"] == GitRepository(git_repo).resolve(
        f'{job["integrated_sha"]}^{{tree}}'
    )
    assert integration["integrated_parents"] == [job["base_sha"]]
    assert integration["pr"]["state"] == "MERGED"
    assert integration["pr"]["merge_commit_sha"] == job["integrated_sha"]
    assert github.escalated == []
    assert github.created_prs == 1
    assert github.closed_issues == [3]


def test_fallback_pending_observation_is_one_contract_valid_commit(
    git_repo: Path,
) -> None:
    class ObservationRecordingStateStore(StateStore):
        def __init__(self, root: Path) -> None:
            super().__init__(root)
            self.observation_commits: list[dict[str, Any]] = []

        def save_run(self, run_id: str, state: dict[str, Any]) -> None:
            job = state.get("active_ticket_job")
            if isinstance(job, dict) and isinstance(
                job.get("required_checks_evidence"), dict
            ):
                require_current_run_state(state)
                self.observation_commits.append(deepcopy(state))
            super().save_run(run_id, state)

    fixture = write_fixture(git_repo / "github.json", issues={"3": issue(3)})
    states = ObservationRecordingStateStore(git_repo / ".agent-run")
    state, _ = Controller(
        FixtureGitHubReader(fixture), GitRepository(git_repo), states
    ).start(1)
    checkout = git_repo / ".agent-run" / "worktrees" / state["run_id"] / "ticket-3"
    github = ScriptedPublisher(git_repo)
    github.checks = ["pending"]

    waiting = TicketDeliveryEngine(
        git=GitRepository(git_repo),
        states=states,
        github=github,
        agents=AlwaysRejectAgents(checkout),
    ).deliver(state["run_id"])

    assert waiting["status"] == "waiting_checks"
    assert len(states.observation_commits) == 1
    job = waiting["active_ticket_job"]
    receipt = job["fallback_publication_receipt"]
    assert receipt["required_checks_evidence"] == job["required_checks_evidence"]
    assert receipt["required_checks_evidence"] is not job["required_checks_evidence"]
    require_current_run_state(states.observation_commits[0])


def test_final_ci_fix_candidate_save_clears_receipt_observation_before_resume(
    git_repo: Path,
) -> None:
    class CrashAfterFinalCiFixCandidateSave(StateStore):
        crashed = False

        def save_run(self, run_id: str, state: dict[str, Any]) -> None:
            job = state.get("active_ticket_job")
            is_final_ci_fix_candidate = (
                not self.crashed
                and isinstance(job, dict)
                and job.get("phase") == "candidate"
                and job.get("attempt_kind") == "final_ci_fix"
            )
            super().save_run(run_id, state)
            if is_final_ci_fix_candidate:
                self.crashed = True
                raise SimulatedProcessCrash(
                    "simulated crash after Final CI-fix Candidate save"
                )

    fixture = write_fixture(git_repo / "github.json", issues={"3": issue(3)})
    states = CrashAfterFinalCiFixCandidateSave(git_repo / ".agent-run")
    state, _ = Controller(
        FixtureGitHubReader(fixture), GitRepository(git_repo), states
    ).start(1)
    checkout = git_repo / ".agent-run" / "worktrees" / state["run_id"] / "ticket-3"
    publisher = ScriptedPublisher(git_repo)
    publisher.checks = ["fail", "pass"]
    agents = AlwaysRejectAgents(checkout)
    engine = TicketDeliveryEngine(
        git=GitRepository(git_repo), states=states, github=publisher, agents=agents
    )

    with pytest.raises(SimulatedProcessCrash, match="Final CI-fix Candidate"):
        engine.deliver(state["run_id"])

    interrupted = states.load_current_run(state["run_id"])
    assert interrupted is not None
    interrupted_job = interrupted["active_ticket_job"]
    assert interrupted_job["phase"] == "candidate"
    assert interrupted_job["attempt_kind"] == "final_ci_fix"
    assert "required_checks_evidence" not in interrupted_job
    receipt = interrupted_job["fallback_publication_receipt"]
    assert "required_checks_evidence" not in receipt
    assert interrupted_job["ci_evidence"]["result"] == "fail"
    assert interrupted_job["final_ci_fix_failure_head"] == interrupted_job[
        "ci_evidence"
    ]["head_sha"]
    previous = receipt["previous_publication_authorization"]
    assert previous["publication_sha"] == interrupted_job["final_ci_fix_failure_head"]
    assert previous["fallback_receipt"]["required_checks_evidence"]["head_sha"] == (
        interrupted_job["final_ci_fix_failure_head"]
    )
    require_current_run_state(interrupted)

    completed = engine.deliver(state["run_id"])

    assert completed["status"] == "ticket_completed"
    completed_job = completed["active_ticket_job"]
    assert completed_job["required_checks_evidence"]["head_sha"] == completed_job[
        "publication_sha"
    ]
    assert completed_job["fallback_publication_receipt"][
        "required_checks_evidence"
    ]["head_sha"] == completed_job["publication_sha"]


@pytest.mark.parametrize("crash_timing", ["before", "after"])
def test_fallback_observation_commit_resumes_without_duplicate_work(
    git_repo: Path, crash_timing: str
) -> None:
    class CandidateRecordingGit(GitRepository):
        candidate_commit_calls = 0

        def commit_candidate(
            self,
            checkout: Path,
            *,
            ticket_number: int,
            attempt: int,
            expected_head: str | None = None,
            candidate_intent: dict[str, object] | None = None,
        ) -> str | None:
            self.candidate_commit_calls += 1
            return super().commit_candidate(
                checkout,
                ticket_number=ticket_number,
                attempt=attempt,
                expected_head=expected_head,
                candidate_intent=candidate_intent,
            )

    class PullRequestRecordingPublisher(ScriptedPublisher):
        def __init__(self, repo: Path) -> None:
            super().__init__(repo)
            self.external_pr_numbers: set[int] = set()

        def ensure_ticket_pr(self, **kwargs: Any) -> int:
            pr_number = super().ensure_ticket_pr(**kwargs)
            self.external_pr_numbers.add(pr_number)
            return pr_number

    class CrashAroundObservationStateStore(StateStore):
        crashed = False

        def save_run(self, run_id: str, state: dict[str, Any]) -> None:
            job = state.get("active_ticket_job")
            is_observation_commit = (
                not self.crashed
                and isinstance(job, dict)
                and isinstance(job.get("required_checks_evidence"), dict)
            )
            if is_observation_commit and crash_timing == "before":
                self.crashed = True
                raise SimulatedProcessCrash(
                    "simulated crash before atomic Observation commit"
                )
            super().save_run(run_id, state)
            if is_observation_commit:
                self.crashed = True
                raise SimulatedProcessCrash(
                    "simulated crash after atomic Observation commit"
                )

    fixture = write_fixture(git_repo / "github.json", issues={"3": issue(3)})
    states = CrashAroundObservationStateStore(git_repo / ".agent-run")
    state, _ = Controller(
        FixtureGitHubReader(fixture), GitRepository(git_repo), states
    ).start(1)
    checkout = git_repo / ".agent-run" / "worktrees" / state["run_id"] / "ticket-3"
    github = PullRequestRecordingPublisher(git_repo)
    github.checks = ["pending", "pass"]
    agents = AlwaysRejectAgents(checkout)
    git = CandidateRecordingGit(git_repo)
    engine = TicketDeliveryEngine(
        git=git, states=states, github=github, agents=agents
    )

    with pytest.raises(SimulatedProcessCrash):
        engine.deliver(state["run_id"])

    interrupted = states.load_current_run(state["run_id"])
    assert interrupted is not None
    require_current_run_state(interrupted)
    interrupted_job = interrupted["active_ticket_job"]
    interrupted_receipt = interrupted_job["fallback_publication_receipt"]
    if crash_timing == "before":
        assert interrupted["status"] == "active"
        assert interrupted_job["phase"] == "publishing"
        assert "required_checks_evidence" not in interrupted_job
        assert "required_checks_evidence" not in interrupted_receipt
    else:
        assert interrupted["status"] == "waiting_checks"
        assert interrupted_job["phase"] == "waiting_checks"
        assert interrupted_receipt["required_checks_evidence"] == (
            interrupted_job["required_checks_evidence"]
        )
    candidate_sha = interrupted_job["candidate_sha"]
    pr_number = interrupted_job["pr_number"]
    review_budget = deepcopy(interrupted_job["review_budget"])
    modification_attempts = interrupted_job["modification_attempts"]
    development_thread_ids = list(agents.development_thread_ids)
    review_count = agents.review_count
    reviewer_thread_ids = list(agents.reviewer_thread_ids)
    publication_requests = len(agents.publication_requests)
    candidate_commit_calls = git.candidate_commit_calls

    completed = engine.deliver(state["run_id"])

    job = completed["active_ticket_job"]
    assert completed["status"] == "ticket_completed"
    assert job["candidate_sha"] == candidate_sha
    assert job["pr_number"] == pr_number
    assert job["review_budget"] == review_budget
    assert job["modification_attempts"] == modification_attempts
    assert github.external_pr_numbers == {pr_number}
    assert git.candidate_commit_calls == candidate_commit_calls
    assert agents.development_thread_ids == development_thread_ids
    assert agents.review_count == review_count
    assert agents.reviewer_thread_ids == reviewer_thread_ids
    assert len(agents.publication_requests) == publication_requests


def test_ci_sourced_fallback_receipt_preserves_failure_source_and_authority() -> None:
    engine = object.__new__(ChangeDeliveryEngine)
    engine.review_budget_policy = lambda: TICKET_POLICY
    engine.save = lambda state: state
    engine.git = SimpleNamespace(
        verify_candidate_integrity=lambda _base, _candidate: {"ok": True},
        diff_name_status=lambda _base, _candidate: ["M delivered.txt"],
        resolve=lambda _revision: "candidate-tree",
    )
    ci_evidence = {"head_sha": "old-head", "checks": [{"name": "tests"}]}
    prior_artifact = {
        "checks": {
            lane: {
                "status": "pass",
                "evidence": evidence,
                "findings": [],
            }
            for lane, evidence in (
                ("e2e", E2E_PASS_EVIDENCE),
                ("standards", STANDARDS_PASS_EVIDENCE),
                ("spec", SPEC_PASS_EVIDENCE),
            )
        }
    }
    job: dict[str, Any] = {
        "base_sha": "base",
        "candidate_sha": "candidate",
        "effective_revision": "revision",
        "acceptance_artifact": prior_artifact,
        "acceptance_record": {
            "acceptance_scope": "change_job",
            "reviewed_base_sha": "base",
            "reviewed_candidate_sha": "prior-candidate",
            "reviewed_candidate_tree": "candidate-tree",
            "effective_revision": "revision",
            "reviewer_thread_id": "reviewer-1",
            "artifact": prior_artifact,
        },
        "publication_authority": "acceptance",
        "pr_number": 7,
        "publication_sha": "old-head",
        "ci_evidence": ci_evidence,
        "repair_source": "required_checks",
        "attempt_kind": "ordinary",
        "review_budget": {
            "window": 1,
            "development_attempts": 4,
            "reviewer_invocations": 3,
            "final_ci_fix_used": True,
            "review_artifacts": [],
            "checkpoint_reason": None,
        },
        "review_budget_history": [],
    }

    assert engine._prepare_ticket_fallback({}, job)

    receipt = job["fallback_publication_receipt"]
    assert receipt["failure_evidence"] == ci_evidence
    assert receipt["required_check_failure_evidence"] == ci_evidence
    assert receipt["failure_evidence_source"] == "required_checks"
    assert receipt["previous_publication_authority"] == "acceptance"
    assert receipt["required_check_failure_head"] == "old-head"
    assert receipt["repair_delta"] == ["M delivered.txt"]
    assert receipt["repair_delta_base_sha"] == "old-head"


def test_fallback_after_final_ci_fix_reuses_prior_acceptance_artifact() -> None:
    engine = object.__new__(ChangeDeliveryEngine)
    engine.review_budget_policy = lambda: TICKET_POLICY
    engine.save = lambda state: state
    engine.git = SimpleNamespace(
        verify_candidate_integrity=lambda _base, _candidate: {"ok": True},
        diff_name_status=lambda _base, _candidate: ["M delivered.txt"],
        resolve=lambda _revision: "candidate-tree",
    )
    prior_artifact = {"checks": {"spec": {"status": "fail"}}}
    job: dict[str, Any] = {
        "base_sha": "base",
        "candidate_sha": "candidate-2",
        "last_review_candidate_sha": "candidate-1",
        "effective_revision": "revision",
        "fallback_publication_receipt": {
            "candidate_sha": "candidate-1",
            "candidate_tree": "prior-tree",
            "last_acceptance_artifact": prior_artifact,
        },
        "publication_authority": "fallback",
        "pr_number": 7,
        "publication_sha": "old-head",
        "ci_evidence": {"head_sha": "candidate-2", "checks": [{"name": "tests"}]},
        "repair_source": "required_checks",
        "attempt_kind": "final_ci_fix",
        "review_budget": {
            "window": 1,
            "development_attempts": 4,
            "reviewer_invocations": 3,
            "final_ci_fix_used": True,
            "review_artifacts": [],
            "checkpoint_reason": None,
        },
        "review_budget_history": [],
    }

    assert engine._prepare_ticket_fallback({}, job)

    receipt = job["fallback_publication_receipt"]
    assert receipt["last_acceptance_artifact"] == prior_artifact
    assert receipt["candidate_sha"] == "candidate-2"
    assert receipt["failure_evidence_source"] == "required_checks"


def test_acceptance_repair_supersedes_stale_ci_fallback_evidence() -> None:
    engine = object.__new__(ChangeDeliveryEngine)
    engine.review_budget_policy = lambda: TICKET_POLICY
    engine.save = lambda state: state
    engine.git = SimpleNamespace(
        verify_candidate_integrity=lambda _base, _candidate: {"ok": True},
        diff_name_status=lambda _base, _candidate: ["M delivered.txt"],
        resolve=lambda _revision: "candidate-tree",
    )
    acceptance_artifact = {"checks": {"spec": {"status": "fail"}}}
    stale_ci = {"head_sha": "old-head", "checks": [{"name": "old-tests"}]}
    required_checks_origin = {
        "pr_number": 7,
        "head_sha": "old-head",
        "result": "fail",
        "checks": [{"name": "old-tests", "bucket": "fail"}],
    }
    job: dict[str, Any] = {
        "base_sha": "base",
        "candidate_sha": "candidate",
        "last_review_candidate_sha": "previous",
        "effective_revision": "revision",
        "acceptance_artifact": acceptance_artifact,
        "ci_evidence": stale_ci,
        "required_checks_origin": required_checks_origin,
        "repair_source": "acceptance",
        "review_budget": {
            "window": 1,
            "development_attempts": 4,
            "reviewer_invocations": 3,
            "final_ci_fix_used": False,
            "review_artifacts": [],
            "checkpoint_reason": None,
        },
        "review_budget_history": [],
    }

    assert engine._prepare_ticket_fallback({}, job)

    receipt = job["fallback_publication_receipt"]
    assert receipt["failure_evidence_source"] == "acceptance"
    assert receipt["failure_evidence"] == acceptance_artifact
    assert receipt["failure_evidence"] != stale_ci
    assert receipt["required_checks_origin"] == required_checks_origin


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
    assert resumed["status"] == "blocked"
    assert resumed["active_ticket_job"]["ticket_number"] == 3
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


@pytest.mark.parametrize(
    "agent_commit_message",
    ["agent-owned commit", "chore(ticket-3): candidate 1"],
)
def test_agent_owned_clean_commit_routes_to_same_development_thread(
    git_repo: Path, agent_commit_message: str
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": issue(3)})
    states = StateStore(git_repo / ".agent-run")
    state, _ = Controller(
        FixtureGitHubReader(fixture), GitRepository(git_repo), states
    ).start(1)
    checkout = git_repo / ".agent-run" / "worktrees" / state["run_id"] / "ticket-3"
    agents = GitIntegrityRepairAgents(checkout, commit_message=agent_commit_message)

    result = TicketDeliveryEngine(
        git=GitRepository(git_repo),
        states=states,
        github=ScriptedPublisher(git_repo),
        agents=agents,
    ).deliver(state["run_id"])

    assert result["status"] == "ticket_completed"
    job = result["active_ticket_job"]
    assert job["review_budget"]["development_attempts"] == 2
    assert job["modification_attempts"] == 2
    assert job.get("blocked_reason") is None
    assert len(agents.development_requests) == 2
    assert agents.development_requests[1]["thread_id"] == "development-thread-1"
    evidence = agents.development_requests[1]["git_integrity_evidence"]
    assert agents.development_requests[1]["repair_source"] == "git_integrity"
    assert evidence["actual_subject"] == agent_commit_message
    assert evidence["workspace_clean"] == "true"
    candidate_sha = str(job["candidate_sha"])
    git = GitRepository(git_repo)
    assert git.commit_parents(candidate_sha) == [str(job["base_sha"])]
    assert git.commit_subject(candidate_sha) != agent_commit_message


def test_cancelled_worker_preserves_dirty_stable_ticket_checkout(
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

    assert (checkout / "README.md").read_text(encoding="utf-8") == (
        "tracked work before cancellation\n"
    )
    assert (checkout / "untracked.txt").read_text(encoding="utf-8") == (
        "untracked work before cancellation\n"
    )


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
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": issue(3)})
    states = StateStore(git_repo / ".agent-run")
    state, _ = Controller(
        FixtureGitHubReader(fixture), GitRepository(git_repo), states
    ).start(1)
    checkout = git_repo / ".agent-run" / "worktrees" / state["run_id"] / "ticket-3"
    publisher = ScriptedPublisher(git_repo)
    publisher.checks = ["fail", "pass"]
    original_snapshot = publisher.required_checks_snapshot

    def snapshot_with_passing_sibling(
        pr_number: int, *, expected_head_sha: str
    ) -> dict[str, Any]:
        observation = original_snapshot(
            pr_number, expected_head_sha=expected_head_sha
        )
        if observation["result"] == "fail":
            observation["checks"].append(
                {
                    "name": "lint",
                    "workflow": "ci",
                    "bucket": "pass",
                    "link": "https://example.invalid/checks/lint",
                }
            )
        return observation

    monkeypatch.setattr(
        publisher, "required_checks_snapshot", snapshot_with_passing_sibling
    )
    agents = CheckRepairAgents(checkout)

    result = TicketDeliveryEngine(
        git=GitRepository(git_repo),
        states=states,
        github=publisher,
        agents=agents,
    ).deliver(state["run_id"])

    assert result["status"] == "ticket_completed"
    ci_evidence = agents.development_requests[1]["ci_evidence"]
    assert ci_evidence["checks"] == publisher.failed_check_evidence["checks"]
    assert ci_evidence["pr_number"] == publisher.pr_number
    assert ci_evidence["head_sha"]
    assert ci_evidence["result"] == "fail"
    assert result["active_ticket_job"]["required_checks_origin"] == ci_evidence
    assert result["active_ticket_job"]["deterministic_integration_record"][
        "required_checks_origin"
    ] == ci_evidence
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


def test_required_check_origin_survives_acceptance_repair_and_final_record(
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
    agents = RequiredCheckThenAcceptanceFindingAgents(checkout)

    completed = TicketDeliveryEngine(
        git=GitRepository(git_repo), states=states, github=publisher, agents=agents
    ).deliver(state["run_id"])

    assert completed["status"] == "ticket_completed"
    job = completed["active_ticket_job"]
    origin = job["required_checks_origin"]
    assert origin["pr_number"] == job["pr_number"]
    assert origin["head_sha"] != job["publication_sha"]
    assert origin["result"] == "fail"
    assert origin["checks"] == publisher.failed_check_evidence["checks"]
    assert "ci_evidence" not in job
    assert job["deterministic_integration_record"]["required_checks_origin"] == origin
    assert agents.review_count == 3


def test_ticket_required_check_failure_uses_exact_head_repair_evidence(
    git_repo: Path,
) -> None:
    (git_repo / "pyproject.toml").write_text(
        "[tool.agent-run.required-checks]\n"
        'code-failure-steps = ["ci::test::Run tests"]\n',
        encoding="utf-8",
    )
    fixture = write_fixture(git_repo / "github.json", issues={"3": issue(3)})
    states = StateStore(git_repo / ".agent-run")
    state, _ = Controller(
        FixtureGitHubReader(fixture), GitRepository(git_repo), states
    ).start(1)
    checkout = git_repo / ".agent-run" / "worktrees" / state["run_id"] / "ticket-3"

    class RealEvidencePublisher(ScriptedPublisher):
        def __init__(self, repo: Path) -> None:
            super().__init__(repo)
            self.git = GitRepository(repo)
            self.repository = "example/project"
            self.evidence_fields: list[str] = []
            self.actions_job_reads = 0

        def _checks(self, _pr_number: int, fields: str) -> list[object]:
            self.evidence_fields.append(fields)
            check = self.failed_check_evidence["checks"][0]
            return [
                {key: check[key] for key in fields.split(",") if key in check}
            ]

        def _json(self, *_arguments: str, **_kwargs: object) -> object:
            self.actions_job_reads += 1
            return {
                "id": 33,
                "head_sha": self.live_head,
                "name": "test",
                "workflow_name": "ci",
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
            }

        required_check_evidence = GhGitHubPublisher.required_check_evidence

    publisher = RealEvidencePublisher(git_repo)
    publisher.checks = ["fail", "pass"]
    publisher.failed_check_evidence["checks"][0].pop("repairability", None)
    publisher.failed_check_evidence["checks"][0]["link"] = (
        "https://github.com/example/project/actions/runs/22/job/33"
    )
    agents = CheckRepairAgents(checkout)

    completed = TicketDeliveryEngine(
        git=GitRepository(git_repo), states=states, github=publisher, agents=agents
    ).deliver(state["run_id"])

    assert completed["status"] == "ticket_completed"
    assert len(agents.development_requests) == 2
    assert agents.development_requests[1]["repair_source"] == "required_checks"
    assert publisher.evidence_fields == [
        "bucket,state,name,link,workflow,description"
    ]
    assert publisher.actions_job_reads == 1
    assert {
        "name",
        "workflow",
        "bucket",
        "description",
        "link",
    } <= set(agents.development_requests[1]["ci_evidence"]["checks"][0])


def test_ticket_required_check_head_drift_blocks_before_failed_evidence(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": issue(3)})
    states = StateStore(git_repo / ".agent-run")
    state, _ = Controller(
        FixtureGitHubReader(fixture), GitRepository(git_repo), states
    ).start(1)
    checkout = git_repo / ".agent-run" / "worktrees" / state["run_id"] / "ticket-3"
    publisher = LiveHeadDriftsBeforeFailedEvidencePublisher(git_repo)
    publisher.checks = ["fail"]
    agents = CheckRepairAgents(checkout)

    blocked = TicketDeliveryEngine(
        git=GitRepository(git_repo), states=states, github=publisher, agents=agents
    ).deliver(state["run_id"])

    assert blocked["status"] == "blocked"
    job = blocked["active_ticket_job"]
    assert job["phase"] == "blocked"
    assert job["blocked_reason"] == "published_head_mismatch"
    assert "required_checks_evidence" not in job
    assert "ci_evidence" not in job
    assert publisher.failed_evidence_calls == 0
    assert agents.development_thread_ids == [None]
    assert blocked["diagnostics"][0]["code"] == "published_head_mismatch"


@pytest.mark.parametrize(
    "checks",
    [
        [
            {
                "name": "cancelled-test",
                "workflow": "ci",
                "bucket": "cancel",
                "state": "CANCELLED",
                "description": "The job was cancelled.",
                "link": "https://example.invalid/checks/cancelled",
            }
        ],
        [
            {
                "name": "platform-test",
                "workflow": "ci",
                "bucket": "fail",
                "state": "FAILURE",
                "description": "The runner platform timed out.",
                "link": "https://example.invalid/checks/platform",
            }
        ],
        [
            {
                "name": "unknown-test",
                "workflow": "ci",
                "bucket": "fail",
                "description": "No authoritative conclusion was reported.",
                "link": "https://example.invalid/checks/unknown",
            }
        ],
        [
            {
                "name": "runner-test",
                "workflow": "CI",
                "bucket": "fail",
                "state": "FAILURE",
                "description": "The runner setup failed.",
                "link": "https://example.invalid/checks/runner",
                "job": {
                    "status": "completed",
                    "conclusion": "failure",
                    "steps": [
                        {
                            "name": "Set up job",
                            "status": "completed",
                            "conclusion": "failure",
                        }
                    ],
                },
            }
        ],
        [
            {
                "name": "lint-test",
                "workflow": "CI",
                "bucket": "fail",
                "state": "FAILURE",
                "description": "An unconfigured lint step failed.",
                "link": "https://example.invalid/checks/lint",
                "job": {
                    "status": "completed",
                    "conclusion": "failure",
                    "steps": [
                        {
                            "name": "Run lint",
                            "status": "completed",
                            "conclusion": "failure",
                        }
                    ],
                },
            }
        ],
        [
            {
                "name": "test",
                "workflow": "ci",
                "bucket": "fail",
                "state": "FAILURE",
                "repairability": "code_failure",
                "description": "The configured test step failed.",
                "link": "https://example.invalid/checks/test",
            },
            {
                "name": "runner-test",
                "workflow": "CI",
                "bucket": "fail",
                "state": "FAILURE",
                "description": "The runner setup also failed.",
                "link": "https://example.invalid/checks/runner",
            },
        ],
        [
            {
                "name": "incomplete-test",
                "workflow": "CI",
                "bucket": "fail",
                "state": "FAILURE",
                "description": "The Actions job detail is incomplete.",
                "link": "https://example.invalid/checks/incomplete",
                "job": {
                    "status": "in_progress",
                    "conclusion": None,
                },
            }
        ],
    ],
    ids=(
        "cancelled",
        "platform",
        "unknown",
        "runner",
        "unconfigured-step",
        "mixed",
        "incomplete",
    ),
)
def test_ticket_non_repairable_required_check_failure_is_supervised(
    git_repo: Path, checks: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(change_delivery_module, "MAX_MODIFICATION_ATTEMPTS", 1)
    fixture = write_fixture(git_repo / "github.json", issues={"3": issue(3)})
    states = StateStore(git_repo / ".agent-run")
    state, _ = Controller(
        FixtureGitHubReader(fixture), GitRepository(git_repo), states
    ).start(1)
    checkout = git_repo / ".agent-run" / "worktrees" / state["run_id"] / "ticket-3"
    publisher = ScriptedPublisher(git_repo)
    publisher.checks = ["fail"]
    publisher.failed_check_evidence = {
        "pr_number": publisher.pr_number,
        "checks": checks,
    }
    agents = CheckRepairAgents(checkout)

    completed = TicketDeliveryEngine(
        git=GitRepository(git_repo), states=states, github=publisher, agents=agents
    ).deliver(state["run_id"])

    assert completed["status"] == "waiting_external"
    job = completed["active_ticket_job"]
    assert completed["supervision_window"]["kind"] == "github_convergence"
    assert job["phase"] == "waiting_checks"
    assert job["modification_attempts"] == 1
    assert job["review_budget"]["development_attempts"] == 1
    assert job["review_budget"]["final_ci_fix_used"] is False
    assert len(agents.development_requests) == 1
    assert "next_attempt_kind" not in job
    assert completed["diagnostics"][0]["code"] == (
        "github_check_failure_not_repairable"
    )


def test_ticket_mixed_pending_required_checks_are_supervised(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(change_delivery_module, "MAX_MODIFICATION_ATTEMPTS", 1)
    fixture = write_fixture(git_repo / "github.json", issues={"3": issue(3)})
    states = StateStore(git_repo / ".agent-run")
    state, _ = Controller(
        FixtureGitHubReader(fixture), GitRepository(git_repo), states
    ).start(1)
    checkout = git_repo / ".agent-run" / "worktrees" / state["run_id"] / "ticket-3"
    publisher = ScriptedPublisher(git_repo)
    publisher.checks = ["fail"]
    original_snapshot = publisher.required_checks_snapshot

    def mixed_snapshot(pr_number: int, *, expected_head_sha: str) -> dict[str, Any]:
        observation = original_snapshot(
            pr_number, expected_head_sha=expected_head_sha
        )
        observation["checks"].append(
            {"name": "integration", "bucket": "pending", "state": "IN_PROGRESS"}
        )
        return observation

    monkeypatch.setattr(publisher, "required_checks_snapshot", mixed_snapshot)
    agents = CheckRepairAgents(checkout)

    completed = TicketDeliveryEngine(
        git=GitRepository(git_repo), states=states, github=publisher, agents=agents
    ).deliver(state["run_id"])

    job = completed["active_ticket_job"]
    assert completed["status"] == "waiting_external"
    assert job["phase"] == "waiting_checks"
    assert job["review_budget"]["development_attempts"] == 1
    assert job["review_budget"]["final_ci_fix_used"] is False
    assert len(agents.development_requests) == 1
    assert "next_attempt_kind" not in job
    assert completed["diagnostics"][0]["code"] == (
        "github_check_failure_not_repairable"
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("pr_number", 999),
        ("head_sha", "stale-head"),
        ("result", "pass"),
        (
            "checks",
            [
                {
                    "name": "test",
                    "workflow": "other-ci",
                    "bucket": "fail",
                    "state": "FAILURE",
                    "repairability": "code_failure",
                    "description": "The test job failed.",
                    "link": "https://example.invalid/checks/test",
                }
            ],
        ),
    ],
)
def test_ticket_conflicting_failure_evidence_is_supervised(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: object,
) -> None:
    monkeypatch.setattr(change_delivery_module, "MAX_MODIFICATION_ATTEMPTS", 1)
    fixture = write_fixture(git_repo / "github.json", issues={"3": issue(3)})
    states = StateStore(git_repo / ".agent-run")
    state, _ = Controller(
        FixtureGitHubReader(fixture), GitRepository(git_repo), states
    ).start(1)
    checkout = git_repo / ".agent-run" / "worktrees" / state["run_id"] / "ticket-3"
    publisher = ScriptedPublisher(git_repo)
    publisher.checks = ["fail"]
    publisher.failed_check_evidence = {
        **publisher.failed_check_evidence,
        field: value,
    }
    if field == "checks":
        original_snapshot = publisher.required_checks_snapshot

        def mismatched_snapshot(
            pr_number: int, *, expected_head_sha: str
        ) -> dict[str, Any]:
            observation = original_snapshot(
                pr_number, expected_head_sha=expected_head_sha
            )
            observation["checks"][0]["workflow"] = "ci"
            return observation

        monkeypatch.setattr(
            publisher, "required_checks_snapshot", mismatched_snapshot
        )
    agents = CheckRepairAgents(checkout)

    completed = TicketDeliveryEngine(
        git=GitRepository(git_repo), states=states, github=publisher, agents=agents
    ).deliver(state["run_id"])

    job = completed["active_ticket_job"]
    assert completed["status"] == "waiting_external"
    assert job["phase"] == "waiting_checks"
    assert job["review_budget"]["final_ci_fix_used"] is False
    assert len(agents.development_requests) == 1
    assert "next_attempt_kind" not in job
    assert completed["diagnostics"][0]["code"] == (
        "github_check_failure_not_repairable"
    )


@pytest.mark.parametrize(
    "error",
    [
        GitHubReadError("github_timeout", "evidence read timed out"),
        OSError("evidence transport unavailable"),
        TimeoutError("evidence read timed out"),
    ],
)
def test_ticket_failed_required_check_evidence_read_is_supervised(
    git_repo: Path, error: BaseException
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": issue(3)})
    states = StateStore(git_repo / ".agent-run")
    state, _ = Controller(
        FixtureGitHubReader(fixture), GitRepository(git_repo), states
    ).start(1)
    checkout = git_repo / ".agent-run" / "worktrees" / state["run_id"] / "ticket-3"
    publisher = CheckEvidenceFailsOncePublisher(git_repo, error)
    agents = CheckRepairAgents(checkout)
    engine = TicketDeliveryEngine(
        git=GitRepository(git_repo), states=states, github=publisher, agents=agents
    )

    waiting = engine.deliver(state["run_id"])
    assert waiting["status"] == "waiting_external"

    interrupted = states.load_current_run(str(state["run_id"]))
    assert interrupted is not None
    job = interrupted["active_ticket_job"]
    assert job["modification_attempts"] == 1
    assert job["required_checks_evidence"]["result"] == "fail"
    assert job["phase"] == "waiting_checks"
    assert "ci_evidence" not in job
    assert agents.development_thread_ids == [None]

    completed = engine.deliver(state["run_id"])

    assert completed["status"] == "ticket_completed"
    assert completed["active_ticket_job"]["modification_attempts"] == 2
    assert agents.development_thread_ids == [None, "development-thread-1"]


def test_parent_publication_reads_existing_pr_context_once(git_repo: Path) -> None:
    publisher = ScriptedPublisher(git_repo)
    publisher.pr_titles.append("feat(parent): deliver parent scope")
    loop = ParentDeliveryLoop(
        git=GitRepository(git_repo),
        states=StateStore(git_repo / ".agent-run"),
        github=publisher,
        agents=ScriptedAgents(git_repo),
    )

    request = loop.adapter.publication_request(
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


def test_parent_review_budget_exhaustion_persists_checkpoint() -> None:
    state: dict[str, Any] = {
        "status": "parent_delivery_pending",
        "diagnostics": [],
        "policy_snapshot": DeliveryPolicy().snapshot(),
    }
    job: dict[str, Any] = {
        "review_budget": {
            "window": 1,
            "development_attempts": 0,
            "reviewer_invocations": 5,
            "final_ci_fix_used": False,
            "review_artifacts": [],
            "checkpoint_reason": None,
        },
        "review_budget_history": [],
    }

    ParentDeliveryLoop._escalate(state, job, "review_budget_exhausted")

    assert job["blocked_reason"] == "review_budget_exhausted"
    assert job["review_budget"]["checkpoint_reason"] == "review_budget_exhausted"


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
    assert interrupted["active_ticket_job"]["review_budget"][
        "reviewer_invocations"
    ] == 0
    assert interrupted["active_ticket_job"]["review_budget"]["review_artifacts"] == []

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


@pytest.mark.parametrize("check_result", ["pass", "pending"])
def test_required_checks_reject_live_base_sha_drift_before_progress(
    git_repo: Path, check_result: str
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": issue(3)})
    states = StateStore(git_repo / ".agent-run")
    state, _ = Controller(
        FixtureGitHubReader(fixture), GitRepository(git_repo), states
    ).start(1)
    checkout = git_repo / ".agent-run" / "worktrees" / state["run_id"] / "ticket-3"
    publisher = LiveBaseDriftPublisher(git_repo)
    publisher.checks = [check_result]

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
    assert "required_checks_evidence" not in result["active_ticket_job"]


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
    assert resumed["status"] == "blocked"
    assert resumed["active_ticket_job"]["ticket_number"] == 3
    assert resumed["diagnostics"] == [
        {
            "code": "ticket_pr_closed_unmerged",
            "message": "Current Ticket PR was closed without merging",
            "ticket_number": 3,
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
    waiting["active_ticket_job"]["phase"] = "waiting_merge"
    states.save_run(state["run_id"], waiting)
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


def test_post_merge_revision_drift_requires_explicit_requeue(
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
    interrupted_job = interrupted["active_ticket_job"]
    old_effective_revision = str(interrupted_job["effective_revision"])
    old_generation = interrupted_job["ticket_branch_generation"]
    old_modification_attempts = interrupted_job["modification_attempts"]
    old_window = interrupted_job["review_budget"]["window"]
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
    run_head_before_stale = GitRepository(git_repo).resolve(str(state["run_branch"]))

    drifted = engine.deliver(state["run_id"])

    assert drifted["status"] == "requeue_required"
    assert drifted["diagnostics"][0]["code"] == "ticket_requirements_changed"
    assert drifted["active_ticket_job"]["ticket_branch_generation"] == old_generation
    assert drifted["active_ticket_job"]["modification_attempts"] == (
        old_modification_attempts
    )
    assert drifted["active_ticket_job"]["review_budget"]["window"] == old_window
    assert GitRepository(git_repo).resolve(str(state["run_branch"])) == run_head_before_stale
    assert drifted["active_ticket_job"]["pr_number"] == 11
    assert publisher.closed_issues == []

    current = states.load_run(state["run_id"])
    assert current is not None
    retired = requeue_change_job(current)
    states.save_run(state["run_id"], current)
    assert current["status"] == "active"
    assert current["active_ticket_job"] is None
    assert retired["pr_number"] == 11
    assert retired["effective_revision"] == old_effective_revision
    assert current["retired_job_generations"] == [retired]


def test_revision_drift_after_merged_save_waits_for_explicit_requeue(
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

    stale = engine.deliver(state["run_id"])

    expected_archive = [
        {
            "pr_number": old_pr_number,
            "integrated_sha": old_integrated_sha,
            "effective_revision": old_effective_revision,
        }
    ]
    assert stale["status"] == "requeue_required"
    assert stale["active_ticket_job"]["pr_number"] == old_pr_number
    assert stale["active_ticket_job"]["superseded_integrations"] == (
        expected_archive
    )
    assert publisher.created_prs == 1
    assert publisher.closed_issues == []
    assert publisher.prepared_ticket_closes == [
        {"pr_number": old_pr_number, "integrated_sha": old_integrated_sha}
    ]

    current = states.load_run(state["run_id"])
    assert current is not None
    retired = requeue_change_job(current)
    states.save_run(state["run_id"], current)

    assert retired["pr_number"] == old_pr_number
    assert current["status"] == "active"
    assert current["active_ticket_job"] is None
    assert publisher.created_prs == 1
    assert publisher.closed_issues == []


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
    class SnapshotRecordingStateStore(StateStore):
        def __init__(self, root: Path) -> None:
            super().__init__(root)
            self.snapshot_failure_commits: list[dict[str, Any]] = []

        def save_run(self, run_id: str, state: dict[str, Any]) -> None:
            job = state.get("active_ticket_job")
            if isinstance(job, dict) and state.get("status") == "waiting_external":
                self.snapshot_failure_commits.append(deepcopy(state))
            super().save_run(run_id, state)

    fixture = write_fixture(git_repo / "github.json", issues={"3": issue(3)})
    states = SnapshotRecordingStateStore(git_repo / ".agent-run")
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
    assert "required_checks_evidence" not in job
    assert len(states.snapshot_failure_commits) == 1
    assert states.snapshot_failure_commits[0]["status"] == "waiting_external"
    assert "publication_operation_retry" not in states.snapshot_failure_commits[0][
        "active_ticket_job"
    ]
    assert "last_publication_error" not in states.snapshot_failure_commits[0][
        "active_ticket_job"
    ]
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


def test_repeated_required_checks_reads_share_supervision_without_publication_retry(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": issue(3)})
    states = StateStore(git_repo / ".agent-run")
    state, _ = Controller(
        FixtureGitHubReader(fixture), GitRepository(git_repo), states
    ).start(1)
    checkout = git_repo / ".agent-run" / "worktrees" / state["run_id"] / "ticket-3"
    agents = PassAgents(checkout)
    publisher = CheckReadFailsNTimesPublisher(git_repo, failures=5)
    engine = TicketDeliveryEngine(
        git=GitRepository(git_repo), states=states, github=publisher, agents=agents
    )

    waiting_states = [engine.deliver(state["run_id"]) for _ in range(5)]

    assert all(item["status"] == "waiting_external" for item in waiting_states)
    assert all(
        item["active_ticket_job"]["phase"] == "publishing"
        and "publication_operation_retry" not in item["active_ticket_job"]
        and "last_publication_error" not in item["active_ticket_job"]
        for item in waiting_states
    )
    completed = engine.deliver(state["run_id"])

    assert completed["status"] == "ticket_completed"
    assert publisher.merged_sha is not None
    assert completed["active_ticket_job"]["pr_number"] == publisher.pr_number
    assert len(agents.publication_requests) == 1
    assert completed["active_ticket_job"]["publication_attempts"] == 1
    assert completed["active_ticket_job"]["review_budget"] == waiting_states[0][
        "active_ticket_job"
    ]["review_budget"]


def test_same_head_snapshot_timeout_preserves_observation_without_progress(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": issue(3)})
    states = StateStore(git_repo / ".agent-run")
    state, _ = Controller(
        FixtureGitHubReader(fixture), GitRepository(git_repo), states
    ).start(1)
    checkout = git_repo / ".agent-run" / "worktrees" / state["run_id"] / "ticket-3"
    agents = PassAgents(checkout)
    publisher = SnapshotReadFailsAfterPendingPublisher(git_repo)
    engine = TicketDeliveryEngine(
        git=GitRepository(git_repo),
        states=states,
        github=publisher,
        agents=agents,
    )

    waiting = engine.deliver(state["run_id"])
    previous = deepcopy(waiting["active_ticket_job"]["required_checks_evidence"])
    development_requests = len(agents.development_requests)
    review_count = agents.review_count

    unavailable = engine.deliver(state["run_id"])

    job = unavailable["active_ticket_job"]
    assert unavailable["status"] == "waiting_external"
    assert unavailable["diagnostics"][0]["code"] == (
        "github_checks_observation_pending"
    )
    assert job["required_checks_evidence"] == previous
    assert job["phase"] == "waiting_checks"
    assert len(agents.development_requests) == development_requests
    assert agents.review_count == review_count
    assert publisher.merged_sha is None


def test_required_checks_snapshot_state_change_cannot_publish_as_pass(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": issue(3)})
    states = StateStore(git_repo / ".agent-run")
    state, _ = Controller(
        FixtureGitHubReader(fixture), GitRepository(git_repo), states
    ).start(1)
    checkout = git_repo / ".agent-run" / "worktrees" / state["run_id"] / "ticket-3"
    agents = PassAgents(checkout)
    publisher = SnapshotChangesAfterInitialPassPublisher(git_repo)
    engine = TicketDeliveryEngine(
        git=GitRepository(git_repo),
        states=states,
        github=publisher,
        agents=agents,
    )

    waiting = engine.deliver(state["run_id"])

    job = waiting["active_ticket_job"]
    assert waiting["status"] == "waiting_checks"
    assert job["phase"] == "waiting_checks"
    assert "required_checks" not in job
    assert job["required_checks_evidence"]["result"] == "pending"
    assert "deterministic_integration_record" not in job
    assert publisher.merged_sha is None


@pytest.mark.parametrize(
    ("result", "checks"),
    [
        ("pass", []),
        ("pass", [{"name": "quality", "bucket": "fail"}]),
        ("none", [{"name": "quality", "bucket": "pass"}]),
        ("pending", [{"name": "quality", "bucket": "pass"}]),
        ("fail", [{"name": "quality", "bucket": "pending"}]),
        ("unknown", []),
    ],
)
def test_published_head_gate_fails_closed_on_required_checks_contradictions(
    git_repo: Path, result: str, checks: list[dict[str, str]]
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": issue(3)})
    states = StateStore(git_repo / ".agent-run")
    state, _ = Controller(
        FixtureGitHubReader(fixture), GitRepository(git_repo), states
    ).start(1)
    checkout = git_repo / ".agent-run" / "worktrees" / state["run_id"] / "ticket-3"
    publisher = ContradictorySnapshotPublisher(
        git_repo, result=result, checks=checks
    )

    blocked = TicketDeliveryEngine(
        git=GitRepository(git_repo),
        states=states,
        github=publisher,
        agents=PassAgents(checkout),
    ).deliver(state["run_id"])

    assert blocked["status"] == "blocked"
    assert blocked["diagnostics"][0]["code"] == "published_head_mismatch"
    assert blocked["active_ticket_job"]["phase"] == "blocked"
    assert publisher.merged_sha is None


def test_active_ticket_authorization_is_checked_before_resume_mutation(
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
    job = waiting["active_ticket_job"]
    fail_artifact = deepcopy(job["acceptance_record"]["artifact"])
    fail_artifact["checks"]["e2e"]["status"] = "fail"
    fail_artifact["checks"]["e2e"]["findings"] = [
        "问题：复验失败；证据：测试失败；必须修复：修复失败；复验：重新运行测试。"
    ]
    job["acceptance_record"]["artifact"] = fail_artifact
    job["acceptance_artifact"] = fail_artifact
    states.save_run(state["run_id"], waiting)

    with pytest.raises(IncompatibleRunStateError, match="artifact must be pass"):
        engine.deliver(state["run_id"])

    assert publisher.merged_sha is None


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


def test_exhausted_publication_operation_retry_is_not_reopened_by_base_drift(
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

    still_pending = engine.deliver(state["run_id"])

    assert still_pending["status"] == "publication_pending"
    assert still_pending["active_ticket_job"]["pr_number"] == publisher.pr_number
    assert publisher.live_head == old_published_sha


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


def test_ticket_fallback_avoids_unbounded_escalation_response(
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

    result = engine.deliver(state["run_id"])
    assert result["status"] == "ticket_completed"
    assert result["active_ticket_job"]["publication_authority"] == "fallback"
    assert publisher.escalated == []


def test_resume_preserves_frontier_while_an_operator_gate_is_current(
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
            "review_budget": {
                "window": 1,
                "development_attempts": 0,
                "reviewer_invocations": 0,
                "final_ci_fix_used": False,
                "review_artifacts": [],
                "checkpoint_reason": None,
            },
            "review_budget_history": [],
        }
    )
    state.update(
        {
            "status": "ready_for_human",
            "terminal_kind": "waiting_human",
        }
    )
    state["ticket_jobs"]["4"] = {
        "ticket_number": 4,
        "phase": "completed",
        "development_thread_id": "developer-4",
        "pr_number": 44,
        "integrated_sha": "b" * 40,
        "deterministic_integration_record": {
            "source": "accepted",
            "base_sha": "a" * 40,
            "candidate_sha": "b" * 40,
            "candidate_tree": "tree-4",
            "publication_sha": "b" * 40,
            "integrated_sha": "b" * 40,
            "integrated_publication_sha": "b" * 40,
            "integrated_tree": "tree-4",
            "integrated_message": "feat: integrated ticket",
            "integrated_parents": ["a" * 40],
            "effective_revision": "ticket-4-revision",
            "pr_number": 44,
            "window": 1,
            "final_ci_fix_used": False,
            "required_checks_mode": "configured",
            "required_checks": "pass",
            "required_checks_evidence": {
                "pr_number": 44,
                "head_sha": "b" * 40,
                "result": "pass",
                "checks": [{"name": "fixture", "bucket": "pass"}],
            },
            "pr": {
                "number": 44,
                "state": "MERGED",
                "head_sha": "b" * 40,
                "base_sha": "a" * 40,
                "merge_commit_sha": "b" * 40,
            },
            "acceptance_record": {
                "acceptance_scope": "change_job",
                "reviewed_base_sha": "a" * 40,
                "reviewed_candidate_sha": "b" * 40,
                "reviewed_candidate_tree": "tree-4",
                "effective_revision": "ticket-4-revision",
                "reviewer_thread_id": "ticket-reviewer-4",
                "artifact": {
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
                    }
                },
            },
        },
        "review_budget": {
            "window": 1,
            "development_attempts": 0,
            "reviewer_invocations": 0,
            "final_ci_fix_used": False,
            "review_artifacts": [],
            "checkpoint_reason": None,
        },
        "review_budget_history": [],
    }
    completed_ticket = state["ticket_jobs"]["4"]
    completed_record = completed_ticket["deterministic_integration_record"]
    completed_acceptance = completed_record["acceptance_record"]
    completed_review_artifact = {
        "reviewer_thread_id": completed_acceptance["reviewer_thread_id"],
        "candidate_sha": completed_acceptance["reviewed_candidate_sha"],
        "reviewed_base_sha": completed_acceptance["reviewed_base_sha"],
        "review_identity": {
            "reviewed_base_sha": completed_acceptance["reviewed_base_sha"],
            "reviewed_candidate_sha": completed_acceptance[
                "reviewed_candidate_sha"
            ],
            "reviewed_candidate_tree": completed_acceptance[
                "reviewed_candidate_tree"
            ],
        },
        "artifact": completed_acceptance["artifact"],
    }
    completed_ticket["review_budget"] = {
        "window": 1,
        "development_attempts": 0,
        "reviewer_invocations": 1,
        "final_ci_fix_used": False,
        "review_artifacts": [completed_review_artifact],
        "checkpoint_reason": None,
    }
    completed_record["review_budget"] = deepcopy(completed_ticket["review_budget"])
    states.save_run(state["run_id"], state)
    fixture_data = json.loads(fixture.read_text(encoding="utf-8"))
    fixture_data["issues"]["3"]["labels"].append("ready-for-human")
    fixture.write_text(json.dumps(fixture_data), encoding="utf-8")

    resumed, _ = Controller(
        FixtureGitHubReader(fixture), GitRepository(git_repo), states
    ).resume(state["run_id"])

    assert resumed["active_ticket_job"]["ticket_number"] == 3
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
    completed_ticket = resumed["ticket_jobs"]["4"]
    assert completed_ticket["phase"] == "completed"
    integration = completed_ticket["deterministic_integration_record"]
    assert integration["effective_revision"] == "ticket-4-revision"
    assert integration["acceptance_record"]["reviewed_candidate_sha"] == "b" * 40
    assert integration["acceptance_record"]["artifact"]["checks"]["e2e"][
        "status"
    ] == "pass"
