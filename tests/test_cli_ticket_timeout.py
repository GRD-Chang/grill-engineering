"""Public recovery commands identify the paused Ticket in a multi-Ticket Run."""
from __future__ import annotations

import json
import shlex
from pathlib import Path

from support.cli import load_only_run_state, run_cli, stdout_json
from support.cli_payloads import passing_acceptance, publication, ticket
from support.inprocess_cli import invoke_cli_inprocess
from support.seeding import write_fixture


def test_status_and_history_commands_resume_only_the_timed_out_ticket(git_repo: Path) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={str(number): {**ticket(), "number": number} for number in (2, 3)},
        delivery={"required_checks": ["pending"]},
        supervision_clock_multiplier=540,
    )
    agents = git_repo / "agents.json"
    agents.write_text(json.dumps({
        "developments": [
            {"thread_id": f"developer-{number}", "summary": "Delivered ticket",
             "write_files": {f"ticket-{number}.txt": "done\n"}}
            for number in (2, 3)
        ],
        "reviews": [passing_acceptance(f"reviewer-{number}", "Passed") for number in (2, 3)],
        "publications": [publication(), publication()],
    }))
    paused = run_cli(
        git_repo, fixture, "run", "1", "--ticket-development-workers", "1",
        "--agent-fixture", str(agents),
    )
    assert paused.returncode == 2, paused.stdout + paused.stderr
    before = load_only_run_state(git_repo)
    run_id = str(before["run_id"])
    assert all(job["blocked_reason"] == "ticket_supervision_timeout" for job in before["ticket_jobs"].values())
    commands = {}
    for verb in ("status", "history"):
        view = stdout_json(invoke_cli_inprocess(git_repo, fixture, verb, run_id, "--json"))
        progress = view["progress"] if verb == "status" else view
        for item in progress["multi_ticket"]["tickets"]:
            number = item["ticket_number"]
            action = item["supervision"]["timeout_resume_action"]
            assert action == f"agent-run resume {run_id} --ticket {number}"
            commands[number] = action
            assert item["recovery_action"].endswith(f"--ticket {number}")
    ambiguous = run_cli(git_repo, fixture, "resume", run_id, "--agent-fixture", str(agents))
    assert ambiguous.returncode == 2
    assert load_only_run_state(git_repo) == before
    data = json.loads(fixture.read_text())
    data["delivery"]["required_checks"] = ["pass"]
    data["delivery"]["check_position"] = 0
    fixture.write_text(json.dumps(data))

    resumed = run_cli(git_repo, fixture, *shlex.split(commands[2])[1:], "--agent-fixture", str(agents))
    assert stdout_json(resumed).get("result") not in {"rejected", "error"}, resumed.stdout + resumed.stderr
    after = load_only_run_state(git_repo)
    assert after["ticket_jobs"]["2"]["phase"] == "completed"
    assert after["ticket_jobs"]["2"]["publication_sha"] == before["ticket_jobs"]["2"]["publication_sha"]
    assert after["ticket_jobs"]["3"] == before["ticket_jobs"]["3"]
    assert after["resume_audit"]["history"][-1]["work_subject"] == "ticket:2"
    assert len(after["agent_invocation_history"]) == len(before["agent_invocation_history"])
