from __future__ import annotations

import json
import subprocess
from pathlib import Path

from conftest import write_fixture
from test_cli import (
    git_fetch_failure_wrapper,
    load_only_run_state,
    run_cli,
    stdout_json,
)
from test_cli_delivery import passing_acceptance, publication, ticket


def _run_agents(path: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "developments": [
                    {
                        "expected_thread_id": None,
                        "thread_id": "ticket-developer-3",
                        "summary": "Delivered the Ticket.",
                        "write_files": {"feature.txt": "done\n"},
                    }
                ],
                "publications": [publication()],
                "reviews": [
                    passing_acceptance("ticket-reviewer-3", "The Ticket flow passed.")
                ],
                "run_reviews": [
                    passing_acceptance("run-reviewer-1", "The complete Run passed.")
                ],
                "run_publications": [
                    {
                        "commit_message": "feat(run): publish the delivery",
                        "pr_title": "feat(run): publish the delivery",
                        "pr_body_markdown": (
                            "## What Problem This Solves\n\nThe completed Ticket needs a final review boundary.\n\n"
                            "## Why This Change Was Made\n\nThe Run branch keeps the delivery isolated.\n\n"
                            "## User Impact\n\nMaintainers can explicitly approve the complete delivery.\n\n"
                            "## Evidence\n\nThe public CLI Run passed."
                        ),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def test_run_reaches_explicit_approval_with_status_and_history(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": ticket()})
    agents = _run_agents(git_repo / "agents.json")

    advanced = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))

    assert advanced.returncode == 0, advanced.stderr
    output = stdout_json(advanced)
    assert output["result"] == "started"
    assert output["status"] == "run_approval_pending"
    run_id = output["run_id"]
    state = load_only_run_state(git_repo)
    assert state["run_publication"]["phase"] == "ready_for_approval"
    assert state["parent"]["number"] == 1

    status = run_cli(git_repo, fixture, "status", run_id)
    assert status.returncode == 0, status.stderr
    assert "运行状态: 等待人工批准" in status.stdout
    assert "下一步: agent-run approve" in status.stdout
    status_json = stdout_json(run_cli(git_repo, fixture, "status", run_id, "--json"))
    assert status_json["active_ticket"] is None
    assert status_json["phase"] == "ready_for_approval"

    history = run_cli(git_repo, fixture, "history", run_id, "--json")
    assert history.returncode == 0, history.stderr
    timeline = stdout_json(history)["timeline"]
    assert any(
        entry.get("ticket") == 3
        and entry.get("worker") == "开发工作代理"
        and entry.get("attempt") == 1
        and entry.get("thread_id") == "ticket-developer-3"
        for entry in timeline
    )
    assert any(
        entry.get("ticket") == 3
        and entry.get("worker") == "独立验收工作代理"
        and entry.get("attempt") == 1
        and entry.get("thread_id") == "ticket-reviewer-3"
        for entry in timeline
    )
    assert any(entry.get("pr_number") == 2 for entry in timeline)
    assert all("summary" not in entry for entry in timeline)
    history_count = len(timeline)

    replayed = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))
    assert replayed.returncode == 0, replayed.stderr
    assert stdout_json(replayed)["result"] == "resumed"
    assert stdout_json(replayed)["run_id"] == run_id
    replay_timeline = stdout_json(
        run_cli(git_repo, fixture, "history", run_id, "--json")
    )["timeline"]
    assert len(replay_timeline) == history_count
    data = json.loads(fixture.read_text(encoding="utf-8"))
    assert len(data["delivery"]["pull_requests"]) == 2


def test_run_supervises_pending_ticket_checks_in_one_call(git_repo: Path) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"3": ticket()},
        delivery={"required_checks": ["pending", "pass", "none"]},
    )
    agents = _run_agents(git_repo / "agents.json")

    completed = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))

    assert completed.returncode == 0, completed.stderr
    assert stdout_json(completed)["status"] == "run_approval_pending"
    state = load_only_run_state(git_repo)
    assert state["ticket_jobs"]["3"]["phase"] == "completed"
    assert state["run_publication"]["phase"] == "ready_for_approval"


def test_run_supervises_initial_repository_read_lag_in_one_call(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"3": ticket()},
        repository_read_failures=[
            {"code": "github_timeout", "message": "repository still converging"},
            {"code": "github_timeout", "message": "repository still converging"},
            {"code": "github_timeout", "message": "repository still converging"},
        ],
    )
    agents = _run_agents(git_repo / "agents.json")

    completed = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))

    assert completed.returncode == 0, completed.stderr
    assert stdout_json(completed)["status"] == "run_approval_pending"
    state = load_only_run_state(git_repo)
    assert state.get("repository_binding_pending") is None
    assert state["base"]["branch"] == "main"


def test_deliver_routes_a_structured_graph_contradiction_to_human(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": ticket()})
    run_id = stdout_json(run_cli(git_repo, fixture, "start", "1"))["run_id"]
    data = json.loads(fixture.read_text(encoding="utf-8"))
    data["delivery_graph_read_failures"] = [
        {"code": "invalid_parent", "message": "parent graph contradicts itself"}
    ]
    fixture.write_text(json.dumps(data), encoding="utf-8")

    blocked = run_cli(git_repo, fixture, "deliver", run_id)

    assert blocked.returncode == 2
    assert stdout_json(blocked)["status"] == "blocked"
    state = load_only_run_state(git_repo)
    assert state["terminal_kind"] == "waiting_human"
    assert state["diagnostics"][0]["code"] == "invalid_parent"


def test_run_reconciles_ticket_close_visibility_in_one_call(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"3": ticket()},
        delivery={"ticket_close_event_lag_reads": 1},
    )
    agents = _run_agents(git_repo / "agents.json")

    completed = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))

    assert completed.returncode == 0, completed.stderr
    assert stdout_json(completed)["status"] == "run_approval_pending"
    state = load_only_run_state(git_repo)
    assert state["ticket_jobs"]["3"]["ticket_closed_by_run"] is True
    assert state["run_publication"]["phase"] == "ready_for_approval"


def test_run_supervises_unparseable_ticket_close_ownership_in_one_call(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"3": ticket()},
        delivery={
            "ticket_close_ownership_read_failures": [
                {
                    "code": "github_invalid_response",
                    "message": "Ticket timeline JSON is incomplete",
                }
            ]
        },
    )
    agents = _run_agents(git_repo / "agents.json")

    completed = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))

    assert completed.returncode == 0, completed.stderr
    assert stdout_json(completed)["status"] == "run_approval_pending"
    state = load_only_run_state(git_repo)
    assert state["ticket_jobs"]["3"]["ticket_closed_by_run"] is True
    mutations = json.loads(fixture.read_text(encoding="utf-8"))["delivery"]["mutations"]
    assert [entry["action"] for entry in mutations].count("close_issue") == 1


def test_run_supervises_unobserved_ticket_close_intent_in_one_call(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"3": ticket()},
        delivery={"ticket_close_intent_missing_once": True},
    )
    agents = _run_agents(git_repo / "agents.json")

    completed = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))

    assert completed.returncode == 0, completed.stderr
    assert stdout_json(completed)["status"] == "run_approval_pending"
    state = load_only_run_state(git_repo)
    assert state["ticket_jobs"]["3"]["ticket_closed_by_run"] is True
    mutations = json.loads(fixture.read_text(encoding="utf-8"))["delivery"]["mutations"]
    assert [entry["action"] for entry in mutations].count("close_issue") == 1


def test_run_reconciles_unknown_ticket_merge_outcomes_in_one_call(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"3": ticket()},
        delivery={"squash_merge_outcomes": ["unknown", "unknown", "merge"]},
    )
    agents = _run_agents(git_repo / "agents.json")

    completed = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))

    assert completed.returncode == 0, completed.stderr
    assert stdout_json(completed)["status"] == "run_approval_pending"
    state = load_only_run_state(git_repo)
    job = state["ticket_jobs"]["3"]
    assert job["merge_intent"]["attempts"] == 3
    assert [entry["attempt"] for entry in job["merge_reconciliation_history"]] == [1, 2]


def test_run_retries_final_pr_read_lag_in_one_call(git_repo: Path) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"3": ticket()},
        delivery={
            "run_required_checks_read_failures": [
                {"code": "github_timeout", "message": "final checks still converging"}
            ]
        },
    )
    agents = _run_agents(git_repo / "agents.json")

    completed = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))

    assert completed.returncode == 0, completed.stderr
    assert stdout_json(completed)["status"] == "run_approval_pending"
    state = load_only_run_state(git_repo)
    assert state["run_publication"]["phase"] == "ready_for_approval"


def test_publish_run_waits_for_repository_binding_in_one_call(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": ticket()})
    agents = _run_agents(git_repo / "agents.json")
    started = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))
    run_id = stdout_json(started)["run_id"]
    data = json.loads(fixture.read_text(encoding="utf-8"))
    data["repository_read_failures"] = [
        {"code": "github_timeout", "message": "repository still converging"}
    ]
    fixture.write_text(json.dumps(data), encoding="utf-8")

    waiting = run_cli(
        git_repo,
        fixture,
        "publish-run",
        run_id,
        "--agent-fixture",
        str(agents),
    )

    assert waiting.returncode == 0, waiting.stderr
    assert stdout_json(waiting)["status"] == "waiting_external"
    state = load_only_run_state(git_repo)
    assert state["run_publication"]["phase"] == "ready_for_approval"


def test_run_advances_multiple_tickets_without_final_merge(git_repo: Path) -> None:
    first = ticket()
    first.update({"number": 2, "title": "Complete ticket 2"})
    second = ticket()
    second.update({"number": 3, "title": "Complete ticket 3"})
    fixture = write_fixture(git_repo / "github.json", issues={"2": first, "3": second})
    agents = _run_agents(git_repo / "agents.json")
    data = json.loads(agents.read_text(encoding="utf-8"))
    data["developments"].append(
        {
            "expected_thread_id": None,
            "thread_id": "ticket-developer-2",
            "summary": "Delivered the second Ticket.",
            "write_files": {"second-feature.txt": "done\n"},
        }
    )
    data["publications"].append(publication())
    data["reviews"].append(
        passing_acceptance("ticket-reviewer-2", "The second Ticket passed.")
    )
    agents.write_text(json.dumps(data), encoding="utf-8")

    advanced = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))

    assert advanced.returncode == 0, advanced.stderr
    assert stdout_json(advanced)["status"] == "run_approval_pending"
    state = load_only_run_state(git_repo)
    assert set(state["ticket_jobs"]) == {"2", "3"}
    fixture_data = json.loads(fixture.read_text(encoding="utf-8"))
    pulls = fixture_data["delivery"]["pull_requests"]
    assert len(pulls) == 3
    assert fixture_data["delivery"]["closed_issues"] == [2, 3]


def test_status_and_history_show_started_development_attempt(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": ticket()})
    agents = _run_agents(git_repo / "agents.json")
    run_id = stdout_json(run_cli(git_repo, fixture, "start", "1"))["run_id"]

    interrupted = run_cli(
        git_repo,
        fixture,
        "deliver",
        run_id,
        "--agent-fixture",
        str(agents),
        "--crash-after-save",
        "5",
    )

    assert interrupted.returncode == 2
    status = stdout_json(run_cli(git_repo, fixture, "status", run_id, "--json"))
    assert status["worker"] == {
        "attempt": 1,
        "phase": "developing",
        "role": "开发工作代理",
        "thread_id": None,
    }
    timeline = stdout_json(run_cli(git_repo, fixture, "history", run_id, "--json"))[
        "timeline"
    ]
    assert any(
        entry.get("worker") == "开发工作代理"
        and entry.get("attempt") == 1
        and entry.get("phase") == "developing"
        for entry in timeline
    )


def test_status_offers_run_for_automatic_recovery_states(git_repo: Path) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": ticket()})
    run_id = stdout_json(run_cli(git_repo, fixture, "start", "1"))["run_id"]
    state = load_only_run_state(git_repo)
    state_path = next((git_repo / ".agent-run" / "runs").glob("*.json"))

    for status in ("waiting_merge", "parent_closeout_pending"):
        state["status"] = status
        state_path.write_text(json.dumps(state), encoding="utf-8")
        rendered = run_cli(git_repo, fixture, "status", run_id)
        assert rendered.returncode == 0, rendered.stderr
        assert "下一步: agent-run run 1" in rendered.stdout


def test_premature_approve_does_not_mark_run_as_execution_failed(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": ticket()})
    run_id = stdout_json(run_cli(git_repo, fixture, "start", "1"))["run_id"]
    fixture_data = json.loads(fixture.read_text(encoding="utf-8"))
    fixture_data["error"] = {
        "code": "github_timeout",
        "message": "simulated transient read timeout",
    }
    fixture.write_text(json.dumps(fixture_data), encoding="utf-8")

    premature = run_cli(git_repo, fixture, "approve", run_id)

    assert premature.returncode == 2
    assert stdout_json(premature)["diagnostics"][-1]["code"] == "command_precondition"
    assert load_only_run_state(git_repo)["status"] == "active"

    status = stdout_json(run_cli(git_repo, fixture, "status", run_id, "--json"))
    assert status["repository"] == "example/project"
    assert status["parent"]["number"] == 1
    assert status["worker"] is None
    assert isinstance(status["elapsed_seconds"], int)


def test_run_supervises_initial_graph_read_lag_in_one_call(git_repo: Path) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": ticket()})
    agents = _run_agents(git_repo / "agents.json")
    started = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))
    assert started.returncode == 0, started.stderr
    fixture_data = json.loads(fixture.read_text(encoding="utf-8"))
    fixture_data["delivery_graph_read_failures"] = [
        {
            "code": "github_timeout",
            "message": "simulated exhausted read retry budget",
        }
        for _ in range(3)
    ]
    fixture.write_text(json.dumps(fixture_data), encoding="utf-8")

    completed = run_cli(
        git_repo, fixture, "run", "1", "--agent-fixture", str(agents)
    )

    assert completed.returncode == 0, completed.stderr
    assert stdout_json(completed)["status"] == "run_approval_pending"
    consumed = json.loads(fixture.read_text(encoding="utf-8"))
    assert consumed["delivery_graph_read_failures"] == []
    pulls_before_recovery = list(consumed["delivery"]["pull_requests"])
    assert len(pulls_before_recovery) == 2


def test_run_restarts_a_paused_supervision_window(git_repo: Path) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": ticket()})
    agents = _run_agents(git_repo / "agents.json")
    started = run_cli(git_repo, fixture, "start", "1")
    run_id = stdout_json(started)["run_id"]
    run_file = next((git_repo / ".agent-run" / "runs").glob("*.json"))
    state = load_only_run_state(git_repo)
    state.update(
        {
            "status": "supervision_timeout",
            "terminal_kind": "supervision_timeout",
            "diagnostics": [{"code": "supervision_timeout"}],
            "supervision_wait": {"resume_status": "waiting_external"},
        }
    )
    run_file.write_text(json.dumps(state), encoding="utf-8")
    fixture_data = json.loads(fixture.read_text(encoding="utf-8"))
    fixture_data["delivery_graph_read_failures"] = [
        {"code": "github_read_failed", "message": "temporary GitHub lag"}
        for _ in range(2)
    ]
    fixture.write_text(json.dumps(fixture_data), encoding="utf-8")

    resumed = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))

    assert resumed.returncode == 0, resumed.stderr
    assert stdout_json(resumed)["run_id"] == run_id
    assert stdout_json(resumed)["status"] == "run_approval_pending"
    persisted = load_only_run_state(git_repo)
    assert persisted["status"] == "run_approval_pending"
    assert "supervision_wait" not in persisted


def test_publish_run_retries_final_pr_after_external_wait(git_repo: Path) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": ticket()})
    agents = _run_agents(git_repo / "agents.json")
    started = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))
    run_id = stdout_json(started)["run_id"]
    run_file = next((git_repo / ".agent-run" / "runs").glob("*.json"))
    state = load_only_run_state(git_repo)
    state.update({"status": "waiting_external", "terminal_kind": "waiting_external"})
    state["run_publication"]["phase"] = "waiting_checks"
    run_file.write_text(json.dumps(state), encoding="utf-8")

    retried = run_cli(
        git_repo, fixture, "publish-run", run_id, "--agent-fixture", str(agents)
    )

    assert retried.returncode == 0, retried.stderr
    assert stdout_json(retried)["status"] == "run_approval_pending"


def test_resume_supervises_read_failures_after_supervision_timeout(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": ticket()})
    agents = _run_agents(git_repo / "agents.json")
    started = run_cli(git_repo, fixture, "start", "1")
    run_id = stdout_json(started)["run_id"]
    run_file = next((git_repo / ".agent-run" / "runs").glob("*.json"))
    state = load_only_run_state(git_repo)
    state.update(
        {
            "status": "supervision_timeout",
            "terminal_kind": "supervision_timeout",
            "diagnostics": [{"code": "supervision_timeout"}],
            "supervision_wait": {"resume_status": "waiting_external"},
        }
    )
    run_file.write_text(json.dumps(state), encoding="utf-8")
    fixture_data = json.loads(fixture.read_text(encoding="utf-8"))
    fixture_data["delivery_graph_read_failures"] = [
        {"code": "github_read_failed", "message": "temporary GitHub lag"}
        for _ in range(2)
    ]
    fixture.write_text(json.dumps(fixture_data), encoding="utf-8")

    resumed = run_cli(
        git_repo, fixture, "resume", run_id, "--agent-fixture", str(agents)
    )

    assert resumed.returncode == 0, resumed.stderr
    assert stdout_json(resumed)["status"] == "run_approval_pending"
    assert load_only_run_state(git_repo)["status"] == "run_approval_pending"


def test_resume_repauses_after_a_fresh_external_wait_window(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": ticket()})
    agents = _run_agents(git_repo / "agents.json")
    run_id = stdout_json(run_cli(git_repo, fixture, "start", "1"))["run_id"]
    run_file = next((git_repo / ".agent-run" / "runs").glob("*.json"))
    state = load_only_run_state(git_repo)
    state.update(
        {
            "status": "supervision_timeout",
            "terminal_kind": "supervision_timeout",
            "diagnostics": [{"code": "supervision_timeout"}],
            "supervision_wait": {"resume_status": "waiting_external"},
        }
    )
    run_file.write_text(json.dumps(state), encoding="utf-8")
    fixture_data = json.loads(fixture.read_text(encoding="utf-8"))
    fixture_data["delivery_graph_read_failures"] = [
        {"code": "github_read_failed", "message": "persistent GitHub lag"}
        for _ in range(130)
    ]
    fixture.write_text(json.dumps(fixture_data), encoding="utf-8")

    resumed = run_cli(
        git_repo, fixture, "resume", run_id, "--agent-fixture", str(agents)
    )

    assert resumed.returncode == 2
    assert stdout_json(resumed)["status"] == "supervision_timeout"
    paused = load_only_run_state(git_repo)
    assert paused["status"] == "supervision_timeout"
    assert paused["supervision_wait"]["resume_status"] == "waiting_external"
    assert paused["supervision_wait"]["budget_seconds"] == 10 * 60


def test_public_run_and_resume_reuse_initial_fetch_failure_run(
    git_repo: Path, tmp_path: Path
) -> None:
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "clone", "--bare", str(git_repo), str(remote)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "remote", "add", "origin", str(remote)],
        cwd=git_repo,
        check=True,
    )
    updater = tmp_path / "updater"
    subprocess.run(["git", "clone", str(remote), str(updater)], check=True)
    subprocess.run(["git", "config", "user.name", "Updater"], cwd=updater, check=True)
    subprocess.run(
        ["git", "config", "user.email", "updater@example.invalid"],
        cwd=updater,
        check=True,
    )
    (updater / "remote.txt").write_text("remote\n", encoding="utf-8")
    subprocess.run(["git", "add", "remote.txt"], cwd=updater, check=True)
    subprocess.run(
        ["git", "commit", "-m", "remote base"],
        cwd=updater,
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "push", "origin", "main"], cwd=updater, check=True)
    remote_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=updater,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    closed_ticket = ticket()
    closed_ticket["state"] = "CLOSED"
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"3": closed_ticket},
        default_head_sha=remote_head,
    )
    counter, environment = git_fetch_failure_wrapper(tmp_path, failures=3)

    failed = run_cli(
        git_repo,
        fixture,
        "run",
        "1",
        extra_env=environment,
    )

    assert failed.returncode == 2
    failed_output = stdout_json(failed)
    run_id = failed_output["run_id"]
    assert failed_output["status"] == "execution_failed"
    assert counter.read_text(encoding="utf-8") == "0\n"
    failed_state = load_only_run_state(git_repo)
    assert failed_state["base_resolution_pending"] is True
    assert not list((git_repo / ".git" / "refs" / "heads" / "agent-run").rglob("*"))

    recovered = run_cli(
        git_repo,
        fixture,
        "run",
        "1",
        extra_env=environment,
    )
    assert recovered.returncode == 2
    assert stdout_json(recovered)["run_id"] == run_id
    assert stdout_json(recovered)["status"] == "progress_exhausted"
    resumed = run_cli(git_repo, fixture, "resume", run_id, extra_env=environment)
    assert resumed.returncode == 2
    assert stdout_json(resumed)["run_id"] == run_id
    state = load_only_run_state(git_repo)
    assert state["status"] == "progress_exhausted"
    assert len(list((git_repo / ".agent-run" / "runs").glob("*.json"))) == 1


def test_run_reconciles_an_already_created_ticket_pr_after_response_loss(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"3": ticket()},
        delivery={"crash_after_ensure_ticket_pr_once": True},
    )
    agents = _run_agents(git_repo / "agents.json")

    interrupted = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))

    assert interrupted.returncode == 2
    fixture_data = json.loads(fixture.read_text(encoding="utf-8"))
    assert len(fixture_data["delivery"]["pull_requests"]) == 1

    recovered = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))

    assert recovered.returncode == 0, recovered.stdout
    assert stdout_json(recovered)["status"] == "run_approval_pending"
    fixture_data = json.loads(fixture.read_text(encoding="utf-8"))
    assert len(fixture_data["delivery"]["pull_requests"]) == 2
    assert fixture_data["delivery"]["closed_issues"] == [3]


def test_completed_run_is_not_reopened_by_run_command(git_repo: Path) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": ticket()})
    agents = _run_agents(git_repo / "agents.json")
    run_id = stdout_json(
        run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))
    )["run_id"]
    assert (
        stdout_json(run_cli(git_repo, fixture, "approve", run_id))["status"]
        == "completed"
    )

    replayed = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))

    assert replayed.returncode == 2
    assert stdout_json(replayed)["run_id"] != run_id
    assert stdout_json(replayed)["status"] == "progress_exhausted"
    states = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in (git_repo / ".agent-run" / "runs").glob("*.json")
    ]
    assert any(state["status"] == "completed" for state in states)
    assert any(state["status"] == "progress_exhausted" for state in states)
