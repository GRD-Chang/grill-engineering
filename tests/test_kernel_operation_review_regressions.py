"""Independent public-operation review regressions; never edit a prepared DB.

The parent is edbbeb62d27d7a9899cd8ba0ffb0aea68e19524d (exact package
composition recorded in data/kernel/operation-review-20261022.json).
The stop/resume test also runs unchanged before bae9d20, on a55c4e60.
All work decisions go through run_once; fixture Agents supply files/raw only.
The native push cases execute an actual local command and inspect its actual
exit status. They never replace a CompletedProcess or forge write_returncode.
"""
from __future__ import annotations

from collections.abc import Callable
import json
import os
from pathlib import Path
import shutil
import sys

import pytest

from agent_run_kernel import DeliveryKind, Effect, EffectResult, Requirements
from agent_run_kernel.model import Issue, ParentIssue
from support.offline import GraphScenario, LostResponse, Scenario
from agent_run_kernel.operation_runner import ReadUnavailable
from agent_run_kernel.query import read
from agent_run_kernel.remote_effects import RemoteEffects
from support.core_operations import scripted_return


def _outage(parent_number: int) -> None:
    raise ReadUnavailable("github_read_failed", "Independent synthetic source outage")


def _window(scenario: Scenario) -> dict[str, float]:
    with scenario.kernel.storage.transaction(write=False) as tx:
        row = tx.one("SELECT started_at,deadline FROM waits WHERE subject=?", (scenario.delivery.key,))
        return {"started_at": row["started_at"], "deadline": row["deadline"]}


def _failures(scenario: Scenario) -> list[dict]:
    return read(scenario.kernel.storage.path, "history").data["publication_operation_retries"]


def test_stop_resume_preserves_the_original_source_wait_deadline(tmp_path: Path) -> None:
    scenario = Scenario(tmp_path / "scenario", DeliveryKind.PARENT_ONLY)
    kernel = scenario.kernel
    now = [100.25]
    kernel.clock = lambda: now[0]
    runner = scenario.runner()
    runner.ports.source = _outage
    assert runner.run_once().retry_at == 105.25
    original = _window(scenario)
    assert original == {"started_at": 100.25, "deadline": 700.25}

    # No Worker was allocated, so the fixture can prove its own Host exit.
    # Stop/resume uses the public control seam, never a row/epoch edit.
    kernel.control("stop", "review-stop")
    kernel.confirm_executor_exit(scenario.execution)
    kernel.control("resume", "review-resume")
    scenario.execution = kernel.claim_executor("replacement-review-executor")
    now[0] = 200.25
    runner = scenario.runner()
    runner.ports.source = _outage
    assert runner.run_once().kind == "waiting"
    assert _window(scenario) == original


@pytest.mark.parametrize("stage", ["source", "delivery"])
def test_a_read_finishing_after_the_absolute_wait_deadline_allocates_no_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str,
) -> None:
    scenario = Scenario(tmp_path / "scenario", DeliveryKind.PARENT_ONLY)
    kernel = scenario.kernel
    now = [100.25]
    kernel.clock = lambda: now[0]
    runner = scenario.runner()
    for _ in range(12):
        state = kernel.status(scenario.delivery.key)
        if state["phase"] == "development" and state["pending_attempt"] is None:
            break
        assert runner.run_once().call is None
    else:
        raise AssertionError("Normal workspace operations never reached Development")
    with monkeypatch.context() as outage:
        outage.setattr(runner.ports, "source", _outage)
        assert runner.run_once().retry_at == 105.25
    assert _window(scenario)["deadline"] == 700.25
    before = kernel.status(scenario.delivery.key)
    physical_read = getattr(runner.ports, stage)

    def finishes_late(request):
        facts = physical_read(request)
        now[0] = 701.25
        return facts

    monkeypatch.setattr(runner.ports, stage, finishes_late)
    now[0] = 105.25
    step = runner.run_once()
    after = kernel.status(scenario.delivery.key)
    # Independently check absence of allocation before the returned category.
    assert step.call is None
    assert after["pending_attempt"] is None
    assert after["budgets"] == before["budgets"]
    assert (step.kind, step.reason) == ("paused", "supervision_timeout")


@pytest.mark.parametrize("command_exit", [0, 7], ids=["actual-zero", "actual-nonzero"])
@pytest.mark.parametrize("readback_fails", [False, True], ids=["readback-target", "readback-outage"])
def test_actual_native_push_status_is_audited_once_independently_of_its_readback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command_exit: int, readback_fails: bool,
) -> None:
    scenario = Scenario(tmp_path / "scenario", DeliveryKind.PARENT_ONLY)
    now = [100.25]
    scenario.kernel.clock = lambda: now[0]
    bare = tmp_path / "actual-origin.git"
    scenario.port.git.run("init", "--bare", str(bare))
    scenario.port.git.run("remote", "add", "review-origin", str(bare))
    scenario.port.git.run("push", "review-origin", scenario.initial.sha + ":refs/heads/main")
    real_git = shutil.which("git")
    assert real_git is not None
    binary = tmp_path / "bin"
    binary.mkdir()
    marker, trace = tmp_path / "readback-outage", tmp_path / "commands.jsonl"
    # The native adapter needs only its actor read for this one push. A failing
    # assertion on every other gh request prevents accidental external traffic.
    actor = binary / "gh"
    actor.write_text(f"#!{sys.executable}\nimport json,sys\nassert sys.argv[1:]==['api','user']\n"
                     "print(json.dumps({'login':'offline-publisher'}))\n")
    actor.chmod(0o755)
    command = binary / "git"
    command.write_text(f"""#!{sys.executable}
import json,subprocess,sys
from pathlib import Path
marker,trace=Path({str(marker)!r}),Path({str(trace)!r})
arguments=sys.argv[1:]
if arguments and arguments[0]=='ls-remote' and 'review-origin' in arguments and marker.exists():
    print('independent local readback outage',file=sys.stderr)
    raise SystemExit(9)
response=subprocess.run([{real_git!r},*arguments])
if arguments[:2]==['push','review-origin']:
    actual_exit={command_exit} if response.returncode==0 else response.returncode
    with trace.open('a') as output:
        output.write(json.dumps({{'arguments':arguments,'inner_git_returncode':response.returncode,
                                 'actual_command_returncode':actual_exit}})+'\\n')
    if {readback_fails!r} and response.returncode==0:
        marker.write_text('The original push has returned; its readback is unavailable.')
    if actual_exit:
        print('actual transport command exited nonzero after the local push',file=sys.stderr)
    raise SystemExit(actual_exit)
raise SystemExit(response.returncode)
""")
    command.chmod(0o755)
    monkeypatch.setenv("PATH", str(binary) + os.pathsep + os.environ["PATH"])
    for variable in ("GH_TOKEN", "GITHUB_TOKEN"):
        monkeypatch.delenv(variable, raising=False)
    native = RemoteEffects(scenario.port.git.root, scenario.spec.repository,
                           runner_identity={"source": "host"}, remote="review-origin")
    runner = scenario.runner()
    execute, reconcile = runner.ports.execute, runner.ports.reconcile
    observed: list[tuple[Effect, EffectResult | ReadUnavailable]] = []
    grants: list[str] = []

    def execute_original(effect: Effect, authorize: Callable[[], None]) -> EffectResult:
        if effect.operation != "push":
            return execute(effect, authorize)

        def current_grant() -> None:
            authorize()
            grants.append(effect.effect_id)

        try:
            result = native.execute(effect, current_grant)
        except ReadUnavailable as error:
            observed.append((effect, error))
            raise
        observed.append((effect, result))
        return result

    monkeypatch.setattr(runner.ports, "execute", execute_original)
    monkeypatch.setattr(runner.ports, "reconcile", lambda effect:
                        native.reconcile(effect) if effect.operation == "push" else reconcile(effect))
    for _ in range(48):
        step = runner.run_once()
        if step.call is not None:
            scripted_return(scenario.kernel, runner, step.call, scenario.agent)
        if observed:
            break
    else:
        raise AssertionError("Normal public operations never reached the original push")
    effect, physical = observed[0]
    assert grants == [effect.effect_id]
    commands = [json.loads(line) for line in trace.read_text().splitlines()]
    assert len(commands) == 1
    assert commands[0]["inner_git_returncode"] == 0
    assert commands[0]["actual_command_returncode"] == command_exit
    # The command really mutated an independent bare Git repository.
    assert scenario.port.git.run("--git-dir", str(bare), "rev-parse", effect.source_ref).stdout.strip() == effect.after_sha
    if readback_fails:
        assert isinstance(physical, ReadUnavailable)
        assert physical.write_returncode == (command_exit or None)
        assert step.kind == "waiting"
        pending = scenario.kernel.status(effect.subject)["pending_write"]
        assert pending["id"] == effect.effect_id and pending["status"] == "sent"
        assert len(_failures(scenario)) == (1 if command_exit else 0)
        marker.unlink()
        now[0] = 106.25
        runner.run_once(agent_capacity=0)  # Only read back this original intent.
    else:
        assert isinstance(physical, EffectResult)
        assert physical.disposition == "target"
        assert physical.raw["write_returncode"] == command_exit
    # Target settlement never erases the command fact; zero never invents one.
    failures = _failures(scenario)
    assert len(failures) == (1 if command_exit else 0)
    if command_exit:
        assert failures[0]["operation"] == "push"
        assert failures[0]["dispatch_serial"] == 1
        assert failures[0]["evidence"]["returncode"] == command_exit
    assert len(trace.read_text().splitlines()) == 1


def test_offline_lost_response_is_unknown_without_an_observed_command_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = Scenario(tmp_path / "scenario", DeliveryKind.PARENT_ONLY)
    scenario.kernel.clock = lambda: 100.25
    scenario.port.lose_response.add("pr_write")
    execute = scenario.port.execute
    physical_exceptions: list[tuple[str, str, int | None]] = []

    def capture(effect, authorize):
        try:
            return execute(effect, authorize)
        except LostResponse as error:
            physical_exceptions.append((type(error).__name__, str(error), getattr(error, "write_returncode", None)))
            raise

    monkeypatch.setattr(scenario.port, "execute", capture)
    step = scenario.run()
    assert physical_exceptions == [("LostResponse", "pr_write took effect; response lost", None)]
    assert (step.kind, step.reason) == ("waiting", "github_convergence")
    pending = scenario.kernel.status(scenario.delivery.key)["pending_write"]
    assert pending["operation"] == "pr_write" and pending["status"] == "sent"
    assert sum(row["operation"] == "pr_write" for row in scenario.port.writes()) == 1
    assert _failures(scenario) == []
    with scenario.kernel.storage.transaction(write=False) as tx:
        row = tx.one("SELECT evidence FROM waits WHERE subject=?", (scenario.delivery.key,))
        diagnostic = json.loads(tx.get(row["evidence"], scenario.delivery.key))
    assert diagnostic == {"code": "write_response_unavailable", "message": "pr_write took effect; response lost"}
    scenario.kernel.clock = lambda: 106.25
    assert scenario.run().kind == "approval"
    assert _failures(scenario) == []
    assert sum(row["operation"] == "pr_write" for row in scenario.port.writes()) == 1
    assert any(event["kind"] == "effect_readback" and event["identity"] == pending["id"]
               for event in scenario.kernel.history(scenario.delivery.key))
    with scenario.kernel.storage.transaction(write=False) as tx:
        diagnostics = [json.loads(row[0]) for row in tx.rows(
            "SELECT body FROM material WHERE subject=? AND purpose='github_read_observation'", (scenario.delivery.key,))]
    assert diagnostic in diagnostics  # Settling target does not erase the original uncertainty.


def test_formal_graph_completion_final_still_retires_the_remote_run_branch(tmp_path: Path) -> None:
    """Same existing normal contract as test_kernel_cleanup_lifecycle[final_run]."""
    source = Requirements(ParentIssue(1, "Parent", "Deliver the declared behavior", (2,)), (
        Issue(2, "Ticket", "Implement the declared behavior", "OPEN", frozenset({"ready-for-agent"})),
    ))
    scenario = GraphScenario(tmp_path / "graph", source)
    assert scenario.run().kind == "complete"
    assert len(scenario.kernel.source_status()["completions"]) == 1
    runner = scenario.runner()
    for _ in range(12):
        if scenario.kernel.source_status()["final_subject"] is not None:
            break
        assert runner.run_once().call is None
    else:
        raise AssertionError("The real Completion never admitted Final")
    scenario.select(scenario.kernel.source_status()["final_subject"])
    assert scenario.port.remote_commit("refs/heads/run") is not None
    assert scenario.run(approve=True).kind == "complete"
    assert set(scenario.port.remote_refs()) == {"refs/heads/main"}
    final_deletes = [row for row in scenario.port.writes()
                     if row["subject"] == scenario.delivery.key and row["operation"] == "cleanup_remote_ref"]
    assert len(final_deletes) == 1
    assert scenario.kernel.status(scenario.delivery.key)["resource_cleanup"]["state"] == "complete"

