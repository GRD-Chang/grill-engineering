"""One real-process recovery flow; GitHub and Agent responses stay local."""
from __future__ import annotations

from copy import deepcopy
import json
import multiprocessing
from multiprocessing.synchronize import Event as ProcessEvent
import os
from pathlib import Path
import signal
import subprocess
import sys
from typing import Any

from agent_run.controller import Controller
from agent_run.delivery import TicketDeliveryEngine
from agent_run.git import GitRepository
from agent_run.executor_host import _process_start_token
from agent_run.github_fixture import FixtureGitHubPublisher, FixtureGitHubReader
from agent_run.run_lifecycle import prepare_action_application_receipt
from agent_run.run_orchestration import DeliveryRunEngine
from agent_run.state import StateStore
from agent_run.state_contract import require_current_run_state
from agent_run.task_control import TaskControlStore, TaskKey
from support.cli import issue, run_cli, stdout_json
from support.cli_payloads import final_run_agents, passing_acceptance, publication
from support.seeding import seed_run, write_fixture
from support.ticket_parallel import TicketAgents, ticket_engine
from support.workspace import managed_repo, managed_state


def _bind_current_process(repo: Path, run_id: str) -> None:
    """Bind the actual child as Executor, with no unrelated sleeping Worker."""
    states = StateStore(managed_state(repo))
    task = TaskKey(managed_repo(repo), "example/project", 1)
    control = TaskControlStore(managed_state(repo))
    claim = control.claim_action(task, kind="run", payload={"parent": 1})
    reservation = control.begin_executor(
        task, action_id=claim.action_id, run_id=None, runner_binding="b" * 16,
    )
    token = Path(f"/proc/{os.getpid()}/stat").read_text().rsplit(")", 1)[1].split()[19]
    binding = dict(action_id=claim.action_id, generation=reservation.generation)
    control.mark_process_started(task, **binding, pid=os.getpid(), process_start_token=token)
    control.mark_handshake(task, **binding, pid=os.getpid(), process_start_token=token)
    control.bind_run(
        task, claim.action_id, run_id,
        generation=reservation.generation, state_dir=states.root,
    )
    control.record_application(
        task, **binding, run_id=run_id,
        payload_digest=str(claim.action["payload_digest"]),
    )
    state = states.load_current_run(run_id)
    record = control.load(task)
    assert state is not None and record is not None
    prepare_action_application_receipt(state, record["action"])
    states.save_run(run_id, state)
    control.complete_action(task, **binding, result_status=str(state["status"]))


def _execute_until_interrupted(
    git_repo: Path, run_id: str, fixture: Path,
    running: ProcessEvent, green: ProcessEvent, completed: ProcessEvent,
    release: ProcessEvent, stopped_gate: ProcessEvent,
) -> None:
    import agent_run.delivery as delivery

    delivery.SUPERVISION_POLL_SECONDS = 0.02
    git = GitRepository(managed_repo(git_repo))
    numbers = (2, 3, 4, 5, 6)
    os.setsid()
    _bind_current_process(git_repo, run_id)
    child_states = StateStore(managed_state(git_repo))

    class InterruptedAgents(TicketAgents):
        def develop(self, request: dict[str, Any]) -> Any:
            if self._number(request) != 4:
                return super().develop(request)
            recorder = request["_invocation_event"]

            def event(kind: str, **facts: Any) -> None:
                recorder(kind, **facts)
                if kind == "thread_started":
                    running.set()
                    assert release.wait(20), "Development gate timed out"

            return super().develop({**request, "_invocation_event": event})

    def observe(current: dict[str, Any]) -> None:
        if current["ticket_jobs"]["3"].get("blocked_reason") == "ticket_paused":
            stopped_gate.set()
        if current["ticket_jobs"]["2"]["phase"] == "completed":
            completed.set()
            assert release.wait(20), "crash boundary gate timed out"

    class ChecksPublisher(FixtureGitHubPublisher):
        def _next_required_checks_result(self, pr_number: int) -> str:
            return "pass" if green.is_set() else "pending"

    child_states.run_saved_observer = observe
    agents = InterruptedAgents(git_repo, numbers, lambda _: None)
    engine = DeliveryRunEngine(
        controller=Controller(FixtureGitHubReader(fixture), git, child_states),
        tickets=TicketDeliveryEngine(
            git=git, states=child_states,
            github=ChecksPublisher(fixture, git), agents=agents,
        ),
    )
    engine.deliver(run_id)


def test_public_recovery_preserves_local_stop_and_finishes_parent(
    git_repo: Path,
) -> None:
    """CI, queued Resume/Stop and an interrupted sibling meet in one Run."""
    numbers = (2, 3, 4, 5, 6)
    fixture = write_fixture(git_repo / "github.json", issues={
        str(number): issue(
            number,
            blocked_by=[{"number": 2, "state": "OPEN"}] if number == 6 else [],
        )
        for number in numbers
    }, delivery={"required_checks": ["pending"]})
    seeded = seed_run(git_repo, fixture, "1", "--ticket-development-workers", "1")
    assert seeded.returncode == 0, seeded.stderr
    run_id = stdout_json(seeded)["run_id"]
    states = StateStore(managed_state(git_repo))
    git = GitRepository(managed_repo(git_repo))

    def fail_review(number: int) -> None:
        if number == 3:
            raise RuntimeError("controlled Reviewer failure before Executor crash")

    initial_agents = TicketAgents(git_repo, numbers, lambda _: None, before_review=fail_review)
    paused = ticket_engine(
        git, states, fixture, initial_agents, development_only=True,
    ).deliver_ticket(run_id, 3)
    assert paused["ticket_jobs"]["3"]["blocked_reason"] == "ticket_execution_failed"
    waiting = ticket_engine(git, states, fixture, initial_agents).deliver_ticket(run_id, 2)
    assert waiting["ticket_jobs"]["2"]["phase"] == "waiting_checks"
    before_ci = deepcopy(waiting["ticket_jobs"]["2"])

    context = multiprocessing.get_context("spawn")
    running, green, completed, release, stopped_gate = (context.Event() for _ in range(5))

    executor = context.Process(
        target=_execute_until_interrupted,
        args=(git_repo, run_id, fixture, running, green, completed, release, stopped_gate),
        name="multi-ticket-recovery-executor",
    )
    executor.start()
    try:
        assert running.wait(10), f"Executor exited: {executor.exitcode}"
        active = states.load_current_run(run_id)
        assert active is not None
        assert active["ticket_jobs"]["4"]["phase"] == "developing"
        assert active["ticket_integration_holder"] == 2
        assert any(item["ticket_number"] == 5 for item in active["ticket_work_queue"])
        assert "6" not in active["ticket_jobs"] or active["ticket_jobs"]["6"]["phase"] == "pending"

        resumed = run_cli(git_repo, fixture, "resume", run_id, "--ticket", "3")
        assert resumed.returncode == 0, resumed.stdout + resumed.stderr
        assert stdout_json(resumed)["resume_accepted"] is True
        assert stdout_json(resumed)["work_queue_position"] == 2
        stopped = run_cli(git_repo, fixture, "stop", run_id, "--ticket", "3")
        assert stopped.returncode == 0, stopped.stdout + stopped.stderr
        assert stopped_gate.wait(10), "queued Stop was not consumed"
        stopped_state = states.load_current_run(run_id)
        assert stopped_state is not None
        local_stop = deepcopy(stopped_state["ticket_jobs"]["3"])
        assert local_stop["blocked_reason"] == "ticket_paused"
        assert all(item["ticket_number"] != 3 for item in stopped_state["ticket_work_queue"])
        assert executor.is_alive()

        green.set()
        assert completed.wait(10), "CI did not complete while sibling Development remained blocked"
        executor.kill()
        executor.join(5)
        assert executor.exitcode == -signal.SIGKILL
    finally:
        if executor.is_alive():
            executor.kill()
            executor.join(5)
        executor.close()

    interrupted = states.load_current_run(run_id)
    assert interrupted is not None
    require_current_run_state(interrupted)
    assert interrupted["ticket_jobs"]["2"]["phase"] == "completed"
    assert interrupted["ticket_jobs"]["4"]["phase"] == "developing"
    assert interrupted["ticket_jobs"]["3"]["candidate_sha"] == local_stop["candidate_sha"]
    interrupted_invocation = interrupted["ticket_agent_invocations"]["4"]
    assert interrupted_invocation["status"] == "running"
    assert interrupted_invocation["reported_thread_id"] == "developer-4"
    completed_ticket = deepcopy(interrupted["ticket_jobs"]["2"])
    queued = deepcopy(interrupted["ticket_work_queue"])
    remote_before = fixture.read_text()
    denied = run_cli(git_repo, fixture, "run", "1")
    assert denied.returncode == 2, denied.stdout
    assert fixture.read_text() == remote_before
    assert states.load_current_run(run_id) == interrupted

    # The Executor is dead, but an exactly bound Worker still owns live work.
    # Register the surviving process against the original Action/Generation;
    # public recovery must refuse it until public Stop proves it has exited.
    control = TaskControlStore(states.root)
    task = TaskKey(managed_repo(git_repo), "example/project", 1)
    ownership = control.load(task)
    assert ownership is not None
    old_executor = ownership["executor"]
    orphan = subprocess.Popen(
        [sys.executable, "-c", "import signal; signal.pause()"],
        start_new_session=True,
    )
    try:
        token = _process_start_token(orphan.pid)
        assert token is not None
        control.mark_worker_started(
            task, action_id=old_executor["action_id"],
            generation=old_executor["generation"],
            pid=orphan.pid, process_start_token=token,
        )
        ownership_with_orphan = control.path_for(task).read_bytes()
        refused = run_cli(git_repo, fixture, "resume", run_id)
        assert refused.returncode == 2, refused.stdout + refused.stderr
        assert "stop" in refused.stdout
        assert orphan.poll() is None, "Resume must not terminate an outstanding Worker"
        assert states.load_current_run(run_id) == interrupted
        assert control.path_for(task).read_bytes() == ownership_with_orphan
        assert fixture.read_text() == remote_before

        stopped = run_cli(git_repo, fixture, "stop", run_id)
        assert stopped.returncode == 0, stopped.stdout + stopped.stderr
        orphan.wait(timeout=3)
        assert orphan.returncode is not None, "Stop must terminate the Worker before fallback cleanup"
        stopped_run = states.load_current_run(run_id)
        assert stopped_run is not None
        assert stopped_run["status"] == "operator_stopped"
        assert stopped_run["ticket_work_queue"] == [
            {**item, "lease_id": None} for item in queued
        ]
        assert stopped_run["ticket_jobs"]["3"] == local_stop
        assert stopped_run["ticket_jobs"]["2"] == completed_ticket
        for number in (3, 4, 5):
            before = interrupted["ticket_jobs"].get(str(number), {})
            after = stopped_run["ticket_jobs"].get(str(number), {})
            for key in ("review_budget", "review_budget_history", "pending_semantic_attempt"):
                assert after.get(key) == before.get(key), (number, key)
        assert fixture.read_text() == remote_before
    finally:
        if orphan.poll() is None:
            orphan.kill()
        orphan.wait(timeout=3)

    # Once both the Executor and its Worker have exited, authorize continuation.
    data = json.loads(fixture.read_text())
    data["delivery"]["required_checks"] = ["pass"]
    fixture.write_text(json.dumps(data))
    recovery_agents = git_repo / "recovery-agents.json"
    recovery_agents.write_text(json.dumps({
        "developments": [{
            "thread_id": "developer-4" if number == 4 else f"recovered-{number}",
            "expected_thread_id": "developer-4" if number == 4 else None,
            "summary": f"Ticket {number}",
            "write_files": {f"recovered-{number}.txt": "done\n"},
        } for number in (4, 5, 6)],
        "reviews": [
            passing_acceptance(f"recovered-review-{number}", "Recovered Ticket passed.")
            for number in (4, 5, 6)
        ],
        "publications": [publication() for _ in range(3)],
    }))
    recovered = run_cli(
        git_repo, fixture, "resume", run_id, "--agent-fixture", str(recovery_agents),
    )
    assert recovered.returncode == 2, recovered.stdout + recovered.stderr
    assert stdout_json(recovered)["status"] == "progress_exhausted"
    recovered_state = states.load_current_run(run_id)
    assert recovered_state is not None
    assert recovered_state["ticket_jobs"]["3"]["blocked_reason"] == "ticket_paused"
    assert recovered_state["ticket_jobs"]["3"]["phase"] == "blocked"
    assert [
        item for item in recovered_state["agent_invocation_history"]
        if item["work_subject"] == "ticket:3"
    ] == [
        item for item in interrupted["agent_invocation_history"]
        if item["work_subject"] == "ticket:3"
    ]
    for key in ("candidate_sha", "review_budget", "review_budget_history", "pending_semantic_attempt"):
        assert recovered_state["ticket_jobs"]["3"].get(key) == local_stop.get(key), key
    assert recovered_state["ticket_jobs"]["2"] == completed_ticket
    assert recovered_state["ticket_jobs"]["4"]["development_thread_id"] == "developer-4"
    completed_attempts = recovered_state["ticket_jobs"]["4"]["semantic_attempt_history"]
    original_attempt_id = interrupted_invocation["semantic_attempt"]["attempt_id"]
    assert sum(item["attempt_id"] == original_attempt_id for item in completed_attempts) == 1
    for number in (2, 4, 5, 6):
        job = recovered_state["ticket_jobs"][str(number)]
        assert job["phase"] == "completed"
        assert job["review_budget"]["development_attempts"] == 1
    assert any(item["ticket_number"] == 5 for item in queued)
    assert recovered_state["ticket_work_queue"] == []
    assert recovered_state["ticket_integration_holder"] is None

    final_agents = git_repo / "final-agents.json"
    final_agents.write_text(json.dumps({
        **final_run_agents(), "developments": [],
        "reviews": [passing_acceptance("recovered-review-3", "Stopped Ticket passed.")],
    }))
    final = run_cli(
        git_repo, fixture, "resume", run_id, "--ticket", "3",
        "--agent-fixture", str(final_agents),
    )
    assert final.returncode == 0, final.stdout + final.stderr
    approved_state = states.load_current_run(run_id)
    assert approved_state is not None
    assert approved_state["status"] == "run_approval_pending"
    assert all(job["phase"] == "completed" for job in approved_state["ticket_jobs"].values())
    acceptance = approved_state["run_acceptance"]["acceptance_record"]
    assert acceptance["acceptance_scope"] == "run"
    completion_records = acceptance["ticket_completion_records"]
    assert [record["ticket_number"] for record in completion_records] == list(numbers)
    for record in completion_records:
        job = approved_state["ticket_jobs"][str(record["ticket_number"])]
        assert record["integrated_sha"] == job["integrated_sha"]
    approved = run_cli(git_repo, fixture, "approve", run_id)
    assert approved.returncode == 0, approved.stdout + approved.stderr
    final_state = states.load_current_run(run_id)
    assert final_state is not None and final_state["status"] == "completed"
    live = json.loads(fixture.read_text())["delivery"]
    assert len(live["pull_requests"]) == len(numbers) + 1
    assert sorted(live["closed_issues"]) == [1, *numbers]
    assert len(set(live["closed_issues"])) == len(live["closed_issues"])
    assert before_ci["candidate_sha"] == completed_ticket["candidate_sha"]
    assert final_state["ticket_work_queue"] == []
    assert final_state["ticket_integration_queue"] == []
    assert final_state["ticket_integration_holder"] is None
    assert final_state["delivery_cleanup"]["status"] == "completed"
    assert not (states.root / "worktrees" / run_id).exists()
    assert all(pull["state"] == "MERGED" for pull in live["pull_requests"])
    grant = final_state["run_publication"]["approval_grant"]
    assert grant["head_sha"] == acceptance["reviewed_head_sha"]
    final_head = final_state["run_publication"]["integrated_sha"]
    for name in (
        "ticket-2.txt", "ticket-3.txt", "recovered-4.txt",
        "recovered-5.txt", "recovered-6.txt",
    ):
        content = subprocess.run(
            ["git", "show", f"{final_head}:{name}"], cwd=git.root,
            check=True, capture_output=True, text=True,
        )
        assert content.stdout == "done\n"
