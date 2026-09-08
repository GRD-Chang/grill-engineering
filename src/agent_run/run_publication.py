from __future__ import annotations

from collections.abc import Callable
from typing import Any

from agent_run.agents import AgentBackend
from agent_run.delivery_protocol import GitHubPublisher
from agent_run.git import GitRepository
from agent_run.run_publication_approval import RunPublicationApproval
from agent_run.run_publication_flow import RunPublicationFlow
from agent_run.run_currentness import RunCurrentnessReader
from agent_run.state import StateStore


class RunPublicationEngine:
    """Public facade for the Final Run publication and approval lifecycle."""

    def __init__(
        self,
        *,
        git: GitRepository,
        states: StateStore,
        agents: AgentBackend,
        github: GitHubPublisher,
        default_branch: str,
        default_head_sha: str,
        currentness_reader: RunCurrentnessReader | None = None,
    ) -> None:
        self.git = git
        self.states = states
        self.agents = agents
        self.github = github
        self.default_branch = default_branch
        self.default_head_sha = default_head_sha
        self.currentness_reader = currentness_reader

    def publish(self, run_id: str) -> dict[str, Any]:
        return self._flow().publish(run_id)

    def approve(
        self,
        run_id: str,
        *,
        prepare_state: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        return self._approval().approve(run_id, prepare_state=prepare_state)

    def has_current_approval_grant(self, run_id: str) -> bool:
        return self._approval().has_current_approval_grant(run_id)

    def recover_closeout(
        self,
        run_id: str,
        *,
        prepare_state: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        return self._approval().recover_closeout(
            run_id, prepare_state=prepare_state
        )

    def revise(
        self,
        run_id: str,
        feedback: str,
        *,
        prepare_state: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        return self._approval().revise(
            run_id, feedback, prepare_state=prepare_state
        )

    def abandon(self, run_id: str, *, discard_worktree: bool = False) -> dict[str, Any]:
        return self._approval().abandon(run_id, discard_worktree=discard_worktree)

    def _flow(self) -> RunPublicationFlow:
        return RunPublicationFlow(
            git=self.git,
            states=self.states,
            agents=self.agents,
            github=self.github,
            default_branch=self.default_branch,
            default_head_sha=self.default_head_sha,
            currentness_reader=self.currentness_reader,
        )

    def _approval(self) -> RunPublicationApproval:
        return RunPublicationApproval(
            git=self.git,
            states=self.states,
            agents=self.agents,
            github=self.github,
            default_branch=self.default_branch,
            default_head_sha=self.default_head_sha,
            currentness_reader=self.currentness_reader,
        )
