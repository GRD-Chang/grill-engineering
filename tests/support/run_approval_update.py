"""Bounded process barriers and Agent scripts for public Run approval updates."""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from typing import Any, Iterator

from support.cli import PROJECT_ROOT, load_only_run_state, run_cli, stdout_json
from support.cli_fixtures import run_agents
from support.cli_payloads import passing_acceptance, publication, ticket
from support.cli_supervision import _interrupt_run
from support.run_publication import RunPublicationAgents
from support.seeding import write_fixture


def ready_two_ticket_run(repo: Path) -> tuple[Path, dict[str, Any]]:
    """Keep one representative public run with two genuinely completed Tickets."""
    fixture = write_fixture(repo / "github.json", issues={
        "3": ticket(), "4": {**ticket(), "number": 4, "title": "Second independent Ticket"},
    })
    agents = run_agents(repo / "initial-agents.json")
    script = json.loads(agents.read_text())
    script["developments"].append({
        "expected_thread_id": None, "thread_id": "ticket-developer-4",
        "summary": "Delivered the second Ticket.", "write_files": {"second.txt": "done\n"},
    })
    script["reviews"].append(passing_acceptance("ticket-reviewer-4", "Second Ticket passed"))
    script["publications"].append(publication())
    agents.write_text(json.dumps(script))
    result = run_cli(
        repo, fixture, "run", "1", "--agent-fixture", str(agents),
        "--ticket-development-workers", "1",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert stdout_json(result)["status"] == "run_approval_pending"
    return fixture, load_only_run_state(repo)


def update_agents(repo: Path, *, conflict: bool = False, resumed: bool = False) -> Path:
    path = repo / ("resumed-run-update-agents.json" if resumed else "run-update-agents.json")
    path.write_text(json.dumps({
        "developments": ([{
            "expected_thread_id": "run-update-conflict" if resumed else None,
            "thread_id": "run-update-conflict",
            "summary": "Resolved the original Run integration conflict.",
            "expected_files": {"partial.txt": "preserved Run conflict work\n"},
            "write_files": {"README.md": "Run contribution\nDefault contribution\n"},
        }] if conflict else []),
        "run_reviews": [passing_acceptance("updated-run-reviewer", "Updated Run passed")],
        "run_publications": [{**RunPublicationAgents().run_publication({}),
                              "thread_id": "updated-run-publication"}],
    }))
    return path


_GATED_APPROVAL = """
import socket, sys
from pathlib import Path
from agent_run.agent_fixture import FixtureAgentBackend
from agent_run.cli import main
from agent_run.external_supervision import ExternalSupervisor
from agent_run.git import GitRepository

barrier = socket.socket(fileno=int(sys.argv[1]))
barrier.settimeout(20)
boundary = sys.argv[2]

def hold():
    barrier.sendall(b'1')
    assert barrier.recv(1) == b'1', 'Run update barrier was not released'

original_integrate = GitRepository.integrate_remote_heads
def integrate(self, *args, **kwargs):
    result = original_integrate(self, *args, **kwargs)
    if boundary == 'synced':
        hold()
    return result
GitRepository.integrate_remote_heads = integrate

original_gate = FixtureAgentBackend._wait_for_invocation_gate
def invocation_gate(self, role, request, **kwargs):
    if (boundary == 'conflict' and role == 'developments') or (boundary == 'review' and role == 'run_reviews'):
        request['_invocation_event']('thread_started', reported_thread_id=(
            'run-update-conflict' if role == 'developments' else 'updated-run-reviewer'
        ), attempt_count=1)
        if role == 'developments':
            assert request['repair_source'] == 'integration'
            Path(request['checkout'], 'partial.txt').write_text('preserved Run conflict work\\n')
        hold()
    return original_gate(self, role, request, **kwargs)
FixtureAgentBackend._wait_for_invocation_gate = invocation_gate

original_retry = ExternalSupervisor.before_retry
def before_retry(self, state, **kwargs):
    if boundary == 'checks' and state.get('status') == 'waiting_checks':
        hold()
    return original_retry(self, state, **kwargs)
ExternalSupervisor.before_retry = before_retry
raise SystemExit(main(sys.argv[3:]))
"""


@contextmanager
def paused_update(
    repo: Path, fixture: Path, agents: Path, run_id: str, boundary: str,
) -> Iterator[subprocess.Popen[str]]:
    """Wait for a proven update boundary and always reap the CLI and Executor."""
    observer, executor = socket.socketpair()
    with observer, executor:
        process = subprocess.Popen(
            [sys.executable, "-c", _GATED_APPROVAL, str(executor.fileno()), boundary,
             "approve", run_id, "--github-fixture", str(fixture),
             "--agent-fixture", str(agents), "--json"],
            cwd=repo, env={**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")},
            pass_fds=(executor.fileno(),), text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        executor.close()
        try:
            observer.settimeout(20)
            assert observer.recv(1) == b"1", "Run approval did not reach its update boundary"
            yield process
        finally:
            _interrupt_run(process, repo)
