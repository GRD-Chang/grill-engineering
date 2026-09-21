from __future__ import annotations

from support.run_artifacts import _repair_artifact, _passing_artifact

import subprocess
from pathlib import Path
from typing import Any


from agent_run.agents import DevelopmentResult, ReviewResult


class ScriptedRunAgents:
    def __init__(self) -> None:
        self.development_requests: list[dict[str, Any]] = []
        self.review_requests: list[dict[str, Any]] = []
        # The initial Run rejection is followed by exactly one Candidate Run
        # Acceptance; promotion must not invoke a third whole-Run reviewer.
        self._reviews = [
            _repair_artifact(),
            _passing_artifact(),
        ]

    def develop(self, request: dict[str, Any]) -> DevelopmentResult:
        self.development_requests.append(request)
        checkout = Path(str(request["checkout"]))
        (checkout / "run-repair.txt").write_text("repaired\n", encoding="utf-8")
        return DevelopmentResult(
            thread_id="run-repair-developer",
            summary="Repaired the accumulated behavior.",
        )

    def review(self, request: dict[str, Any]) -> ReviewResult:
        self.review_requests.append(request)
        checkout = Path(str(request["checkout"]))
        assert subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=checkout,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        if request.get("candidate_acceptance") is True:
            assert (checkout / "run-repair.txt").read_text(encoding="utf-8") == "repaired\n"
            assert not {
                "run_id",
                "parent",
                "ticket_graph",
                "ticket_completion_records",
                "base_sha",
                "run_head_sha",
                "candidate_sha",
                "repair_base_run_head_sha",
                "expected_merge_result",
                "acceptance_artifact",
                "ci_evidence",
                "human_feedback",
                "merge_conflict_evidence",
            }.intersection(request)
        return ReviewResult(
            thread_id=f"run-reviewer-{len(self.review_requests)}",
            artifact=self._reviews.pop(0),
        )

    def publication(self, request: dict[str, Any]) -> dict[str, str]:
        return {
            "commit_message": "fix(run): repair accumulated delivery behavior",
            "pr_title": "fix(run): repair accumulated delivery behavior",
            "pr_body_markdown": (
                "## What Problem This Solves\n\nThe combined Run failed.\n\n"
                "## Why This Change Was Made\n\nThe repair restores the combined behavior.\n\n"
                "## User Impact\n\nThe complete delivery works together.\n\n"
                "## Evidence\n\nThe fresh Run validation will recheck it."
            ),
        }


class FreshCycleRunAgents(ScriptedRunAgents):
    """Use identities that cannot belong to an archived Repair Cycle."""

    def develop(self, request: dict[str, Any]) -> DevelopmentResult:
        result = super().develop(request)
        return DevelopmentResult(
            thread_id="fresh-run-repair-developer",
            summary=result.summary,
        )

    def review(self, request: dict[str, Any]) -> ReviewResult:
        result = super().review(request)
        return ReviewResult(
            thread_id=f"fresh-run-reviewer-{len(self.review_requests)}",
            artifact=result.artifact,
        )


