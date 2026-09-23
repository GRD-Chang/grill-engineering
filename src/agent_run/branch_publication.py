"""Exact remote observations and outcomes for a managed source ref."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from agent_run.git_errors import GitError


@dataclass(frozen=True)
class RemoteBranchObservation:
    branch: str
    expected_sha: str
    observed_sha: str | None

    @property
    def matches_expected(self) -> bool:
        return self.observed_sha == self.expected_sha


@dataclass(frozen=True)
class BranchPublicationResult:
    observation: RemoteBranchObservation
    head_sha: str
    disposition: Literal["published", "already_published"]


class RemoteBranchChanged(GitError):
    def __init__(self, observation: RemoteBranchObservation) -> None:
        super().__init__(f"remote source branch {observation.branch!r} changed")
        self.observation = observation
