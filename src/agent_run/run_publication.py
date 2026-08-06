from __future__ import annotations

from typing import Any

from agent_run.agents import AgentBackend
from agent_run.delivery_protocol import GitHubPublisher
from agent_run.git import GitRepository
from agent_run.run_publication_approval import RunPublicationApproval
from agent_run.run_publication_flow import RunPublicationFlow
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
    ) -> None:
        self.git = git
        self.states = states
        self.agents = agents
        self.github = github
        self.default_branch = default_branch
        self.default_head_sha = default_head_sha

    def publish(self, run_id: str) -> dict[str, Any]:
        return self._flow().publish(run_id)

    def approve(self, run_id: str) -> dict[str, Any]:
        return self._approval().approve(run_id)

    def revise(self, run_id: str, feedback: str) -> dict[str, Any]:
        return self._approval().revise(run_id, feedback)

    def abandon(self, run_id: str) -> dict[str, Any]:
        return self._approval().abandon(run_id)

    def _flow(self) -> RunPublicationFlow:
        return RunPublicationFlow(
            git=self.git,
            states=self.states,
            agents=self.agents,
            github=self.github,
            default_branch=self.default_branch,
            default_head_sha=self.default_head_sha,
        )

    def _approval(self) -> RunPublicationApproval:
        return RunPublicationApproval(
            git=self.git,
            states=self.states,
            agents=self.agents,
            github=self.github,
            default_branch=self.default_branch,
            default_head_sha=self.default_head_sha,
        )
