"""Real Ticket orchestration fixtures with controllable Agent boundaries."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from agent_run.agent_fixture import FixtureAgentBackend
from agent_run.controller import Controller
from agent_run.delivery import TicketDeliveryEngine
from agent_run.delivery_policy import DeliveryPolicy
from agent_run.git import GitRepository
from agent_run.github_fixture import FixtureGitHubPublisher, FixtureGitHubReader
from agent_run.state import StateStore
from support.cli_payloads import passing_acceptance, publication
from support.seeding import write_fixture

class TicketAgents:
    def __init__(
        self, root: Path, numbers: tuple[int, ...],
        before_develop: Callable[[int], None],
        before_publication: Callable[[int], None] | None = None,
        before_review: Callable[[int], None] | None = None,
    ) -> None:
        self.before_develop = before_develop
        self.before_publication = before_publication
        self.before_review = before_review
        self.backends: dict[int, FixtureAgentBackend] = {}
        for number in numbers:
            path = root / f"ticket-{number}-agents.json"
            path.write_text(json.dumps({
                "developments": [{
                    "thread_id": f"developer-{number}",
                    "summary": f"Ticket {number}",
                    "write_files": {f"ticket-{number}.txt": "done\n"},
                }],
                "reviews": [passing_acceptance(
                    f"reviewer-{number}", f"Ticket {number} passed."
                )],
                "publications": [{**publication(), "thread_id": f"developer-{number}"}],
            }), encoding="utf-8")
            self.backends[number] = FixtureAgentBackend(path)

    @staticmethod
    def _number(request: dict[str, Any]) -> int:
        return int(request["ticket"]["number"])

    def develop(self, request: dict[str, Any]) -> Any:
        number = self._number(request)
        self.before_develop(number)
        return self.backends[number].develop(request)

    def review(self, request: dict[str, Any]) -> Any:
        number = self._number(request)
        if self.before_review is not None:
            self.before_review(number)
        return self.backends[number].review(request)

    def publication(self, request: dict[str, Any]) -> Any:
        number = self._number(request)
        if self.before_publication is not None:
            self.before_publication(number)
        return self.backends[number].publication(request)


def ticket_run(
    git_repo: Path, numbers: tuple[int, ...],
    *, blocked_by: dict[int, list[int]] | None = None,
    workers: int = 3,
    ci_repairs_per_round: int = 3,
) -> tuple[dict[str, Any], StateStore, GitRepository, Path]:
    fixture = write_fixture(git_repo / "github.json", issues={
        str(number): {
            "number": number,
            "title": f"Deliver ticket {number}",
            "body": f"Implement ticket {number}.",
            "state": "OPEN",
            "labels": ["ready-for-agent"],
            "blocked_by": [
                {"number": blocker, "state": "OPEN"}
                for blocker in (blocked_by or {}).get(number, [])
            ],
        }
        for number in numbers
    })
    states = StateStore(git_repo / ".agent-run")
    git = GitRepository(git_repo)
    controller = Controller(
        FixtureGitHubReader(fixture), git, states,
        delivery_policy=DeliveryPolicy(
            ticket_development_workers=workers,
            ci_repairs_per_round=ci_repairs_per_round,
        ),
    )
    state, _ = controller.start(1)
    return state, states, git, fixture


def ticket_engine(
    git: GitRepository, states: StateStore, fixture: Path,
    agents: TicketAgents,
    *, development_only: bool = False,
) -> TicketDeliveryEngine:
    return TicketDeliveryEngine(
        git=git, states=states,
        github=FixtureGitHubPublisher(fixture, git),
        agents=agents,
        development_only=development_only,
    )


