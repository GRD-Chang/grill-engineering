from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from agent_run.state import SimulatedProcessCrash, StateStore
from conftest import seed_run, write_fixture
from test_cli import run_internal_stage, load_only_run_state, run_cli, stdout_json
from test_cli_delivery import final_run_publication, passing_acceptance


def _ticket() -> dict[str, Any]:
    return {
        "number": 2,
        "title": "Guard publish revisions",
        "body": "Do not publish stale evidence.",
        "state": "OPEN",
        "labels": ["ready-for-agent"],
        "blocked_by": [],
    }


def _publication(label: str) -> dict[str, str]:
    return {
        "commit_message": f"fix(run): publish {label} revision",
        "pr_title": f"fix(run): publish {label} revision",
        "pr_body_markdown": f"""
## What Problem This Solves

Stale publication evidence must not advance.

## Why This Change Was Made

This is the {label} candidate.

## User Impact

Only the current revision can merge.

## Evidence

The public CLI publish-race scenario passed.
""".strip(),
    }


def _write_agents(path: Path, *, two_revisions: bool) -> Path:
    developments = [
        {
            "expected_thread_id": None,
            "thread_id": "developer-2",
            "summary": "Implemented the original revision.",
            "write_files": {"revision.txt": "original\n"},
        }
    ]
    publications = [_publication("original")]
    reviews = [
        passing_acceptance(
            "reviewer-original", "The original candidate passed."
        )
    ]
    if two_revisions:
        developments.append(
            {
                "expected_thread_id": "developer-2",
                "thread_id": "developer-2",
                "summary": "Rebuilt against the changed revision.",
                "write_files": {"revision.txt": "current\n"},
            }
        )
        publications.append(_publication("current"))
        reviews.append(
            passing_acceptance(
                "reviewer-current", "The current candidate passed."
            )
        )
    path.write_text(
        json.dumps(
            {
                "developments": developments,
                "publications": publications,
                "reviews": reviews,
            }
        ),
        encoding="utf-8",
    )
    return path


def _write_replacement_agents(path: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "developments": [
                    {
                        "expected_thread_id": None,
                        "thread_id": "developer-current",
                        "summary": "Rebuilt in a replacement Generation.",
                        "write_files": {"revision.txt": "current\n"},
                    }
                ],
                "publications": [_publication("current")],
                "reviews": [
                    passing_acceptance(
                        "reviewer-current",
                        "The current candidate passed.",
                    )
                ],
                "run_reviews": [
                    passing_acceptance(
                        "run-reviewer-current",
                        "The rebuilt Run passed.",
                    )
                ],
                "run_publications": [final_run_publication()],
            }
        ),
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize(
    "action", ["publish_branch", "ensure_ticket_pr", "required_checks"]
)
def test_content_change_at_publish_boundary_requires_explicit_requeue(
    git_repo: Path, action: str
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"2": _ticket()},
        delivery={
            "drift_after": {
                "action": action,
                "kind": "ticket_content",
            },
            "required_checks": (
                ["pending", "none"]
                if action == "required_checks"
                else ["none"]
            ),
        },
    )
    agents = _write_agents(git_repo / "agents.json", two_revisions=False)
    replacement_agents = _write_replacement_agents(
        git_repo / "replacement-agents.json"
    )
    run_id = stdout_json(
        seed_run(git_repo, fixture, "1", idle_control=True)
    )["run_id"]

    delivered = run_internal_stage(
        git_repo,
        fixture,
        "deliver",
        run_id,
        "--agent-fixture",
        str(agents),
    )

    assert delivered.returncode == 2, delivered.stdout
    state = load_only_run_state(git_repo)
    assert state["status"] == "requeue_required"
    job = state["ticket_jobs"]["2"]
    assert job["ticket_branch_generation"] == 1
    assert job["development_thread_id"] == "developer-2"

    requeued = run_cli(
        git_repo,
        fixture,
        "requeue",
        run_id,
        "--agent-fixture",
        str(replacement_agents),
    )

    assert requeued.returncode == 0, requeued.stdout
    state = load_only_run_state(git_repo)
    job = state["ticket_jobs"]["2"]
    assert state["status"] == "run_approval_pending"
    assert job["phase"] == "completed"
    assert job["ticket_branch_generation"] == 2
    assert job["development_thread_id"] == "developer-current"
    assert job["reviewer_thread_ids"] == ["reviewer-current"]
    assert job["acceptance_record"]["reviewer_thread_id"] == "reviewer-current"
    live = json.loads(fixture.read_text(encoding="utf-8"))
    pull_states = [pull["state"] for pull in live["delivery"]["pull_requests"]]
    assert pull_states[-2:] == ["MERGED", "OPEN"]
    assert "OPEN" not in pull_states[:-1]
    if action == "publish_branch":
        assert pull_states == ["MERGED", "OPEN"]
    else:
        assert pull_states == ["CLOSED", "MERGED", "OPEN"]
    assert live["delivery"]["acceptance_records"] == []
    statuses = live["delivery"]["agent_run_status"]
    if action == "publish_branch":
        assert len(statuses) == 2
    else:
        assert statuses[0]["scope"] == "superseded_generation"
        assert statuses[0]["generation"] == 1
        assert len(statuses) == 3
    assert live["delivery"]["closed_issues"] == [2]


@pytest.mark.parametrize(
    "action", ["publish_branch", "ensure_ticket_pr", "required_checks"]
)
def test_ticket_removal_at_publish_boundary_pauses_same_command(
    git_repo: Path, action: str
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"2": _ticket()},
        delivery={
            "drift_after": {
                "action": action,
                "kind": "ticket_removal",
            },
            "required_checks": ["pending"],
        },
    )
    agents = _write_agents(
        git_repo / "agents.json", two_revisions=False
    )
    run_id = stdout_json(
        seed_run(git_repo, fixture, "1")
    )["run_id"]

    delivered = run_internal_stage(
        git_repo,
        fixture,
        "deliver",
        run_id,
        "--agent-fixture",
        str(agents),
    )

    assert delivered.returncode == 2
    state = load_only_run_state(git_repo)
    assert state["status"] == "unsupported_scope_change"
    assert state["active_ticket_job"] is None
    assert state["unsupported_scope_change"]["graph_change_summary"][
        "removed_tickets"
    ] == [2]
    live = json.loads(fixture.read_text(encoding="utf-8"))
    delivery = live["delivery"]
    assert delivery.get("acceptance_records", []) == []
    assert delivery.get("closed_issues", []) == []
    assert delivery.get("mutations", []) == []
    assert not any(
        pr.get("state") == "MERGED"
        for pr in delivery.get("pull_requests", [])
        if isinstance(pr, dict)
    )


def test_resume_freezes_completed_ticket_assets_after_graph_drift(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"2": _ticket()},
        delivery={"required_checks": ["none"]},
    )
    agents = _write_agents(git_repo / "agents.json", two_revisions=False)
    run_id = stdout_json(seed_run(git_repo, fixture, "1", idle_control=True))["run_id"]
    delivered = run_internal_stage(
        git_repo,
        fixture,
        "deliver",
        run_id,
        "--agent-fixture",
        str(agents),
    )
    assert delivered.returncode == 0, delivered.stderr

    before_state = load_only_run_state(git_repo)
    before_fixture = json.loads(fixture.read_text(encoding="utf-8"))
    job = before_state["ticket_jobs"]["2"]
    run_branch = before_state["run_branch"]
    ticket_completion_record = {
        "ticket_number": 2,
        "integrated_sha": job["integrated_sha"],
        "effective_revision": job["effective_revision"],
        "acceptance_record": job["acceptance_record"],
    }
    frozen = {
        "candidate_sha": job["candidate_sha"],
        "acceptance_record": job["acceptance_record"],
        "ticket_completion_record": ticket_completion_record,
        "local_run_branch_sha": subprocess.run(
            ["git", "rev-parse", run_branch],
            cwd=git_repo,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip(),
        "remote_run_branch_sha": before_fixture["delivery"][
            "published_branches"
        ][run_branch],
        "pull_requests": before_fixture["delivery"]["pull_requests"],
        "mutations": before_fixture["delivery"]["mutations"],
    }

    added = _ticket()
    added.update({"number": 4, "title": "Unexpected added ticket"})
    before_fixture["parent"]["sub_issues"] = [2, 4]
    before_fixture["issues"]["4"] = added
    fixture.write_text(json.dumps(before_fixture), encoding="utf-8")

    resumed = run_internal_stage(git_repo, fixture, "deliver", run_id)

    assert resumed.returncode == 2
    assert stdout_json(resumed)["status"] == "unsupported_scope_change"
    after_state = load_only_run_state(git_repo)
    after_fixture = json.loads(fixture.read_text(encoding="utf-8"))
    after_job = after_state["ticket_jobs"]["2"]
    assert after_job["candidate_sha"] == frozen["candidate_sha"]
    assert after_job["acceptance_record"] == frozen["acceptance_record"]
    assert {
        "ticket_number": 2,
        "integrated_sha": after_job["integrated_sha"],
        "effective_revision": after_job["effective_revision"],
        "acceptance_record": after_job["acceptance_record"],
    } == frozen["ticket_completion_record"]
    assert subprocess.run(
        ["git", "rev-parse", run_branch],
        cwd=git_repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip() == frozen["local_run_branch_sha"]
    assert after_fixture["delivery"]["published_branches"][run_branch] == (
        frozen["remote_run_branch_sha"]
    )
    assert after_fixture["delivery"]["pull_requests"] == frozen[
        "pull_requests"
    ]
    assert after_fixture["delivery"]["mutations"] == frozen["mutations"]

    after_fixture["delivery"]["crash_after_recover_abandoned_ticket_once"] = True
    fixture.write_text(json.dumps(after_fixture), encoding="utf-8")
    interrupted_abandon = run_cli(git_repo, fixture, "abandon", run_id)

    assert interrupted_abandon.returncode == 2
    assert stdout_json(interrupted_abandon)["status"] == "abandonment_pending"
    assert load_only_run_state(git_repo)["run_abandonment"]["tickets"][0][
        "eligible"
    ] is True
    interrupted_fixture = json.loads(fixture.read_text(encoding="utf-8"))
    frozen_pending_mutations = interrupted_fixture["delivery"]["mutations"]
    for command in ("resume", "deliver", "approve"):
        blocked = (
            run_internal_stage(git_repo, fixture, command, run_id, "--agent-fixture", str(agents))
            if command == "deliver"
            else run_cli(git_repo, fixture, command, run_id)
        )
        assert blocked.returncode == 2
        assert stdout_json(blocked)["status"] == "abandonment_pending"
        blocked_fixture = json.loads(fixture.read_text(encoding="utf-8"))
        assert blocked_fixture["delivery"]["mutations"] == frozen_pending_mutations
    run_replay = run_cli(
        git_repo,
        fixture,
        "run",
        "1",
        "--agent-fixture",
        str(agents),
    )
    assert run_replay.returncode == 2
    assert stdout_json(run_replay)["status"] == "abandonment_pending"
    assert stdout_json(run_replay)["next_action"] == f"agent-run abandon {run_id}"

    abandoned = run_cli(git_repo, fixture, "abandon", run_id)

    assert abandoned.returncode == 0, abandoned.stderr
    assert stdout_json(abandoned)["status"] == "abandoned"
    abandoned_fixture = json.loads(fixture.read_text(encoding="utf-8"))
    assert abandoned_fixture["issues"]["2"]["state"] == "OPEN"
    assert abandoned_fixture["delivery"]["closed_issues"] == []
    assert [
        mutation["action"]
        for mutation in abandoned_fixture["delivery"]["mutations"]
        if mutation["action"].startswith("abandonment_")
    ] == ["abandonment_reopen_issue", "abandonment_recovery_comment"]
    frozen_after_abandon = abandoned_fixture["delivery"]["mutations"]

    for command in ("resume", "deliver"):
        replayed = (
            run_internal_stage(git_repo, fixture, command, run_id, "--agent-fixture", str(agents))
            if command == "deliver"
            else run_cli(git_repo, fixture, command, run_id)
        )
        assert replayed.returncode == (2 if command == "resume" else 0), (
            replayed.stderr
        )
        assert stdout_json(replayed)["status"] == "abandoned"
    replayed_fixture = json.loads(fixture.read_text(encoding="utf-8"))
    assert replayed_fixture["delivery"]["mutations"] == frozen_after_abandon


def test_abandon_closes_active_ticket_pr_after_graph_drift(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"2": _ticket()},
        delivery={"required_checks": ["pending"]},
    )
    agents = _write_agents(git_repo / "agents.json", two_revisions=False)
    run_id = stdout_json(seed_run(git_repo, fixture, "1", idle_control=True))["run_id"]
    waiting = run_internal_stage(
        git_repo,
        fixture,
        "deliver",
        run_id,
        "--agent-fixture",
        str(agents),
    )
    assert stdout_json(waiting)["status"] == "waiting_checks"
    before_drift = json.loads(fixture.read_text(encoding="utf-8"))
    assert before_drift["delivery"]["pull_requests"][0]["state"] == "OPEN"

    added = _ticket()
    added.update({"number": 4, "title": "Unexpected added ticket"})
    before_drift["parent"]["sub_issues"] = [2, 4]
    before_drift["issues"]["4"] = added
    fixture.write_text(json.dumps(before_drift), encoding="utf-8")
    blocked = run_internal_stage(git_repo, fixture, "deliver", run_id)
    assert blocked.returncode == 2

    abandoned = run_cli(git_repo, fixture, "abandon", run_id)

    assert abandoned.returncode == 0, abandoned.stderr
    assert stdout_json(abandoned)["status"] == "abandoned"
    after = json.loads(fixture.read_text(encoding="utf-8"))
    assert after["delivery"]["pull_requests"][0]["state"] == "CLOSED"
    assert {"action": "close_change_pr", "pr_number": 1} in after["delivery"][
        "mutations"
    ]
    assert str(git_repo / ".agent-run" / "worktrees" / run_id) not in subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=git_repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout


def test_abandon_recovers_lost_change_pr_close_response(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"2": _ticket()},
        delivery={
            "required_checks": ["pending"],
            "crash_after_abandon_change_pr_once": True,
        },
    )
    agents = _write_agents(git_repo / "agents.json", two_revisions=False)
    run_id = stdout_json(seed_run(git_repo, fixture, "1", idle_control=True))["run_id"]
    waiting = run_internal_stage(
        git_repo,
        fixture,
        "deliver",
        run_id,
        "--agent-fixture",
        str(agents),
    )
    assert stdout_json(waiting)["status"] == "waiting_checks"

    interrupted = run_cli(git_repo, fixture, "abandon", run_id)

    assert interrupted.returncode == 2
    assert stdout_json(interrupted)["status"] == "abandonment_pending"
    for command in ("status", "history"):
        json_view = stdout_json(
            run_cli(git_repo, fixture, command, run_id, "--json")
        )
        action = json_view["operator_action"]
        assert action["type"] == "Abandonment Recovery"
        assert action["object"] == "Ticket #2"
        assert action["phase"] == "waiting_checks"
        assert action["reasons"] == [
            "Run abandonment recovery is incomplete."
        ]
        assert action["next_action"] == f"agent-run abandon {run_id}"
        assert json_view["next_action"] == action["next_action"]
        text_view = run_cli(git_repo, fixture, command, run_id)
        assert "类型: Abandonment Recovery" in text_view.stdout
        assert "对象: Ticket #2" in text_view.stdout
        assert "阶段: waiting_checks" in text_view.stdout
        assert "原因: Run abandonment recovery is incomplete." in text_view.stdout
        assert "已保留成果:" in text_view.stdout
        assert "整个 Delivery Run 已暂停；其他 Ticket 不会推进" in text_view.stdout
        assert (
            "唯一下一步: agent-run abandon 1 --repo example/project"
            in text_view.stdout
        )
        assert "<run-id>" not in text_view.stdout
        assert run_id not in text_view.stdout
    after_interruption = json.loads(fixture.read_text(encoding="utf-8"))
    assert after_interruption["delivery"]["pull_requests"][0]["state"] == "CLOSED"
    close_mutations = [
        mutation
        for mutation in after_interruption["delivery"]["mutations"]
        if mutation["action"] == "close_change_pr"
    ]
    assert close_mutations == [{"action": "close_change_pr", "pr_number": 1}]

    recovered = run_cli(git_repo, fixture, "abandon", run_id)

    assert recovered.returncode == 0, recovered.stderr
    assert stdout_json(recovered)["status"] == "abandoned"
    after_recovery = json.loads(fixture.read_text(encoding="utf-8"))
    assert [
        mutation
        for mutation in after_recovery["delivery"]["mutations"]
        if mutation["action"] == "close_change_pr"
    ] == close_mutations


def test_abandon_does_not_reopen_ticket_closed_outside_publisher(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"2": _ticket()},
        delivery={
            "required_checks": ["none"],
            "external_close_before_primary_ticket": True,
        },
    )
    agents = _write_agents(git_repo / "agents.json", two_revisions=False)
    run_id = stdout_json(seed_run(git_repo, fixture, "1", idle_control=True))["run_id"]
    recovered = run_internal_stage(
        git_repo,
        fixture,
        "deliver",
        run_id,
        "--agent-fixture",
        str(agents),
    )
    assert recovered.returncode == 2, recovered.stderr
    interrupted_job = load_only_run_state(git_repo)["ticket_jobs"]["2"]
    assert interrupted_job["phase"] == "blocked"
    assert "ticket_closed_by_run" not in interrupted_job

    drifted = json.loads(fixture.read_text(encoding="utf-8"))
    added = _ticket()
    added.update({"number": 4, "title": "Unexpected added ticket"})
    drifted["parent"]["sub_issues"] = [2, 4]
    drifted["issues"]["4"] = added
    fixture.write_text(json.dumps(drifted), encoding="utf-8")
    assert run_internal_stage(git_repo, fixture, "deliver", run_id).returncode == 2

    before_abandonment = json.loads(fixture.read_text(encoding="utf-8"))
    abandoned = run_cli(git_repo, fixture, "abandon", run_id)

    assert abandoned.returncode == 2, abandoned.stderr
    assert stdout_json(abandoned)["status"] == "incompatible_run_state"
    after = json.loads(fixture.read_text(encoding="utf-8"))
    assert after == before_abandonment
    assert after["issues"]["2"]["state"] == "CLOSED"
    assert not any(
        mutation["action"].startswith("abandonment_")
        for mutation in after["delivery"]["mutations"]
    )


@pytest.mark.parametrize(
    "action", ["publish_branch", "ensure_ticket_pr", "required_checks"]
)
def test_aba_revision_after_crash_requires_explicit_requeue(
    git_repo: Path, action: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_body = _ticket()["body"]
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"2": _ticket()},
        delivery={
            "drift_after": {
                "action": action,
                "kind": "ticket_content",
            },
            "required_checks": (
                ["pending", "none"]
                if action == "required_checks"
                else ["none"]
            ),
        },
    )
    first_agents = _write_agents(
        git_repo / "agents-first.json", two_revisions=False
    )
    run_id = stdout_json(
        seed_run(git_repo, fixture, "1", idle_control=True)
    )["run_id"]

    save_run = StateStore.save_run
    crashed = False

    def crash_after_revision_mismatch_save(
        store: StateStore, saved_run_id: str, state: dict[str, Any]
    ) -> None:
        nonlocal crashed
        save_run(store, saved_run_id, state)
        job = state.get("ticket_jobs", {}).get("2", {})
        if (
            not crashed
            and saved_run_id == run_id
            and job.get("phase") == "blocked"
            and job.get("blocked_reason") == "effective_revision_mismatch"
        ):
            crashed = True
            raise SimulatedProcessCrash("after durable revision mismatch")

    with monkeypatch.context() as crash_patch:
        crash_patch.setattr(StateStore, "save_run", crash_after_revision_mismatch_save)
        interrupted = run_internal_stage(
            git_repo,
            fixture,
            "deliver",
            run_id,
            "--agent-fixture",
            str(first_agents),
        )

    assert crashed, "delivery must durably record the revision mismatch before crashing"
    assert interrupted.returncode == 2
    interrupted_state = load_only_run_state(git_repo)
    assert interrupted_state["ticket_jobs"]["2"]["phase"] == "blocked"
    assert interrupted_state["ticket_jobs"]["2"]["blocked_reason"] == (
        "effective_revision_mismatch"
    )
    live = json.loads(fixture.read_text(encoding="utf-8"))
    live["issues"]["2"]["body"] = original_body
    fixture.write_text(json.dumps(live), encoding="utf-8")
    replacement_agents = _write_replacement_agents(
        git_repo / "agents-replacement.json"
    )

    stale = run_internal_stage(
        git_repo,
        fixture,
        "deliver",
        run_id,
        "--agent-fixture",
        str(replacement_agents),
    )

    assert stale.returncode == 2, stale.stdout
    stale_state = load_only_run_state(git_repo)
    assert stale_state["status"] == "requeue_required"
    stale_job = stale_state["ticket_jobs"]["2"]
    assert stale_job["ticket_branch_generation"] == 1
    assert stale_job["development_thread_id"] == "developer-2"
    assert stale_job["phase"] == "blocked"
    assert stale_job["blocked_reason"] == "effective_revision_mismatch"

    requeued = run_cli(
        git_repo,
        fixture,
        "requeue",
        run_id,
        "--agent-fixture",
        str(replacement_agents),
    )

    assert requeued.returncode == 0, requeued.stdout
    state = load_only_run_state(git_repo)
    job = state["ticket_jobs"]["2"]
    assert state["status"] == "run_approval_pending"
    assert job["phase"] == "completed"
    assert job["ticket_branch_generation"] == 2
    assert job["development_thread_id"] == "developer-current"
    assert job["reviewer_thread_ids"] == ["reviewer-current"]
    assert job["acceptance_record"]["reviewer_thread_id"] == "reviewer-current"
    assert job["acceptance_record"]["effective_revision"] == (
        job["effective_revision"]
    )
    live = json.loads(fixture.read_text(encoding="utf-8"))
    delivery = live["delivery"]
    pull_states = [pull["state"] for pull in delivery["pull_requests"]]
    assert pull_states[-2:] == ["MERGED", "OPEN"]
    if action == "publish_branch":
        assert pull_states == ["MERGED", "OPEN"]
    else:
        assert pull_states == ["CLOSED", "MERGED", "OPEN"]
    assert delivery["acceptance_records"] == []
    statuses = delivery["agent_run_status"]
    if action == "publish_branch":
        assert len(statuses) == 2
    else:
        assert statuses[0]["scope"] == "superseded_generation"
        assert statuses[0]["generation"] == 1
        assert len(statuses) == 3
    assert delivery["closed_issues"] == [2]
    expected_mutations = [
        "completion_comment",
        "close_issue",
        "delete_managed_branch",
    ]
    if action != "publish_branch":
        expected_mutations.insert(0, "close_change_pr")
    assert [mutation["action"] for mutation in delivery["mutations"]] == (
        expected_mutations
    )
