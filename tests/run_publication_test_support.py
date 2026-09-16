from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any


from agent_run.agents import ReviewResult
from agent_run.github_fixture import FixtureGitHubPublisher
from agent_run.github import GitHubReadError
from agent_run.run_acceptance import RunAcceptanceEngine

from run_acceptance_test_support import _completed_run, _passing_artifact

class RunPublicationAgents:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    def run_publication(self, request: dict[str, Any]) -> dict[str, Any]:
        self.requests.append(request)
        return {
            "result_kind": "publication",
            "commit_message": "feat(run): publish the completed delivery",
            "pr_title": "feat(run): publish the completed delivery",
            "pr_body_markdown": (
                "## What Problem This Solves\n\nThe accepted Run needs a human merge boundary.\n\n"
                "## Why This Change Was Made\n\nIt makes the completed delivery reviewable.\n\n"
                "## User Impact\n\nMaintainers can inspect and approve one final PR.\n\n"
                "## Evidence\n\nFresh Run Acceptance passed."
            ),
            "human_blockers": None,
        }

class InvocationRunPublicationAgents(RunPublicationAgents):
    def run_publication(self, request: dict[str, Any]) -> dict[str, Any]:
        result = super().run_publication(request)
        event = request["_invocation_event"]
        event("started", requested_thread_id=None, attempt_count=0)
        event(
            "thread_started",
            reported_thread_id="final-publication-thread",
            attempt_count=1,
        )
        event(
            "completed",
            reported_thread_id="final-publication-thread",
            attempt_count=1,
        )
        result["_thread_id"] = "final-publication-thread"
        return result

class PassingRunReviewer:
    def review(self, request: dict[str, Any]) -> ReviewResult:
        del request
        return ReviewResult("run-reviewer", _passing_artifact())

class InterruptedRunPublisher(FixtureGitHubPublisher):
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
        del branch, base_branch, expected_head_sha, expected_base_sha, title, body
        raise OSError("simulated Publisher interruption")

class DelayedChecksRunPublisher(FixtureGitHubPublisher):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.read_failures = 1

    def required_checks_snapshot(
        self, pr_number: int, *, expected_head_sha: str
    ) -> dict[str, Any]:
        if self.read_failures:
            self.read_failures -= 1
            raise GitHubReadError(
                "github_timeout", "final PR checks have not converged"
            )
        return super().required_checks_snapshot(
            pr_number, expected_head_sha=expected_head_sha
        )


class WaitingThenInterruptedChecksPublisher(FixtureGitHubPublisher):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.required_check_calls = 0
        self.interrupted_live_reads = 0
        self.interrupt_live_reads = False

    def live_pull_request(self, pr_number: int) -> dict[str, Any]:
        if self.interrupt_live_reads:
            self.interrupted_live_reads += 1
            raise OSError("final PR live read process interrupted")
        return super().live_pull_request(pr_number)

    def required_checks_snapshot(
        self, pr_number: int, *, expected_head_sha: str
    ) -> dict[str, Any]:
        self.required_check_calls += 1
        if self.required_check_calls == 1:
            self.interrupt_live_reads = True
            raise GitHubReadError(
                "github_timeout", "final PR checks have not converged"
            )
        return super().required_checks_snapshot(
            pr_number, expected_head_sha=expected_head_sha
        )


class InterruptedFinalRefPublisher(FixtureGitHubPublisher):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.ref_calls = 0

    def ensure_final_run_ref(self, *, branch: str, expected_head_sha: str) -> None:
        del branch, expected_head_sha
        self.ref_calls += 1
        raise OSError("final ref write response was lost")

    def final_run_ref_matches(self, *, branch: str, expected_head_sha: str) -> bool:
        del branch, expected_head_sha
        self.ref_calls += 1
        raise OSError("final ref readback was interrupted")

class CountingNarrativeRefreshPublisher(FixtureGitHubPublisher):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.refresh_attempts = 0

    def refresh_run_pr_narrative(self, **kwargs: Any) -> None:
        self.refresh_attempts += 1
        super().refresh_run_pr_narrative(**kwargs)

class UnknownNarrativeWritePublisher(FixtureGitHubPublisher):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.refresh_attempts = 0

    def refresh_run_pr_narrative(self, **kwargs: Any) -> None:
        self.refresh_attempts += 1
        super().refresh_run_pr_narrative(**kwargs)
        raise GitHubReadError(
            "github_write_failed", "final PR narrative write outcome is unknown"
        )

class HumanThenRunPublicationAgents:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    def run_publication(self, request: dict[str, Any]) -> dict[str, Any]:
        self.requests.append(request)
        event = request["_invocation_event"]
        requested_thread = request.get("thread_id")
        event(
            "started",
            requested_thread_id=requested_thread,
            attempt_count=0,
            model="publication-model",
            reasoning_effort="high",
        )
        event(
            "thread_started",
            reported_thread_id="blocked-publication-thread",
            attempt_count=1,
        )
        event(
            "completed",
            reported_thread_id="blocked-publication-thread",
            attempt_count=1,
        )
        if len(self.requests) == 1:
            return {
                "result_kind": "human_blocker",
                "commit_message": None,
                "pr_title": None,
                "pr_body_markdown": None,
                "human_blockers": [
                    "GitHub denied access; tried gh issue view; grant Issue read access."
                ],
                "_thread_id": "blocked-publication-thread",
            }
        assert request["thread_id"] == "blocked-publication-thread"
        assert request["prior_human_blockers"] == [
            "GitHub denied access; tried gh issue view; grant Issue read access."
        ]
        assert request["human_response_history"] == [
            {
                "generation": 1,
                "human_blockers": [
                    "GitHub denied access; tried gh issue view; grant Issue read access."
                ],
                "response": "Issue read access is now available.",
            }
        ]
        return {
            "result_kind": "publication",
            "commit_message": "feat(run): publish the completed delivery",
            "pr_title": "feat(run): publish the completed delivery",
            "pr_body_markdown": (
                "## What Problem This Solves\n\nThe accepted Run needs publication.\n\n"
                "## Why This Change Was Made\n\nAccess has been restored.\n\n"
                "## User Impact\n\nMaintainers can approve the Run.\n\n"
                "## Evidence\n\nThe original thread rechecked GitHub."
            ),
            "human_blockers": None,
            "_thread_id": "blocked-publication-thread",
        }

def _accepted_run(
    git_repo: Path, *, ticket_number: int = 2, state_root: Path | None = None,
) -> tuple[dict[str, Any], Any, Any, FixtureGitHubPublisher]:
    state, states, git = _completed_run(
        git_repo, ticket_number=ticket_number, state_root=state_root,
    )
    tree = git.resolve(f"{state['run_branch']}^{{tree}}")
    integrated = subprocess.run(
        [
            "git",
            "commit-tree",
            tree,
            "-p",
            str(state["run_branch"]),
            "-m",
            "feat(ticket): integrated accepted ticket",
        ],
        cwd=git_repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    subprocess.run(
        ["git", "update-ref", f"refs/heads/{state['run_branch']}", integrated],
        cwd=git_repo,
        check=True,
    )
    state["ticket_jobs"][str(ticket_number)]["integrated_sha"] = integrated
    integration = state["ticket_jobs"][str(ticket_number)]["deterministic_integration_record"]
    integration.update(
        {
            "integrated_sha": integrated,
            "integrated_tree": tree,
            "integrated_message": git.commit_subject(integrated),
            "integrated_parents": git.commit_parents(integrated),
        }
    )
    integration["pr"].update(
        {"state": "MERGED", "merge_commit_sha": integrated}
    )
    states.save_run(str(state["run_id"]), state)
    publisher = FixtureGitHubPublisher(git_repo / "github.json", git)
    accepted = RunAcceptanceEngine(
        git=git,
        states=states,
        agents=PassingRunReviewer(),
        github=publisher,
        default_head_sha=git.resolve("main"),
    ).accept(str(state["run_id"]))
    return accepted, states, git, publisher
