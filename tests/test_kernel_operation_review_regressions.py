"""Independent public-operation review regressions; never edit a prepared DB.

The parent is edbbeb62d27d7a9899cd8ba0ffb0aea68e19524d (exact package
composition recorded in data/kernel/operation-review-20261022.json).
The stop/resume test also runs unchanged before bae9d20, on a55c4e60.
All work decisions go through run_once; fixture Agents supply files/raw only.
The native push cases execute an actual local command and inspect its actual
exit status. They never replace a CompletedProcess or forge write_returncode.
"""
from __future__ import annotations
from support.core_operations import _window
from support.core_operations import prepare_scripted_agent

from agent_run.contracts.write_authority import WriteAuthority

from collections.abc import Callable
import json
import os
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace

import pytest

from agent_run.model import DeliveryKind, Effect, EffectResult
from support.offline import Scenario
from support.offline_prefixes import _at_phase, _prepared_stage
from support.run_scoped_kernel import domain_kernel
from agent_run.core.operation_contract import ReadUnavailable
from agent_run.queries.run import read
from agent_run.adapters import github_commands
from agent_run.adapters.remote import RemoteEffects


def _outage(parent_number: int) -> None:
    raise ReadUnavailable("github_read_failed", "Independent synthetic source outage")




def _failures(scenario: Scenario) -> list[dict]:
    return read(scenario.kernel.storage.path, 'history', storage=scenario.kernel.storage).data["history"]["effect_failures"]


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
        assert prepare_scripted_agent(kernel, runner.run_once()).call is None
    else:
        raise AssertionError("Normal workspace operations never reached Development")
    with monkeypatch.context() as outage:
        outage.setattr(runner.ports, "source", _outage)
        assert prepare_scripted_agent(kernel, runner.run_once()).retry_at == 105.25
    assert _window(scenario)["deadline"] == 700.25
    before = kernel.status(scenario.delivery.key)
    physical_read = getattr(runner.ports, stage)

    def finishes_late(request):
        facts = physical_read(request)
        now[0] = 701.25
        return facts

    monkeypatch.setattr(runner.ports, stage, finishes_late)
    now[0] = 105.25
    step = prepare_scripted_agent(kernel, runner.run_once())
    after = kernel.status(scenario.delivery.key)
    # Independently check absence of allocation before the returned category.
    assert step.call is None
    assert after["pending_attempt"] is None
    assert after["budgets"] == before["budgets"]
    assert (step.kind, step.reason) == ("paused", "supervision_timeout")


@pytest.fixture(scope="module")
def _parent_push_stage(tmp_path_factory):
    with _prepared_stage(tmp_path_factory, "parent-push",
                         lambda root: _at_phase(root, DeliveryKind.PARENT_ONLY, "push")) as checkout:
        yield checkout


@pytest.fixture
def parent_before_push(_parent_push_stage, isolated_user_environment):
    with _parent_push_stage() as scenario, domain_kernel(scenario.kernel) as kernel:
        scenario.kernel = kernel
        yield scenario


@pytest.mark.parametrize("command_exit", [0, 7], ids=["actual-zero", "actual-nonzero"])
@pytest.mark.parametrize("readback_fails", [False, True], ids=["readback-target", "readback-outage"])
def test_actual_native_push_status_is_audited_once_independently_of_its_readback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command_exit: int, readback_fails: bool, parent_before_push,
) -> None:
    scenario = parent_before_push
    now = [100.25]
    scenario.kernel.clock = lambda: now[0]
    bare = scenario.port.physical.remote
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
if arguments and arguments[0]=='ls-remote' and {str(bare)!r} in arguments and marker.exists():
    print('independent local readback outage',file=sys.stderr)
    raise SystemExit(9)
response=subprocess.run([{real_git!r},*arguments])
if arguments[:2]==['push',{str(bare)!r}]:
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
                           runner_identity={"source": "host"}, remote=str(bare))
    runner = scenario.runner()
    execute, reconcile = runner.ports.execute, runner.ports.reconcile
    monkeypatch.setattr(github_commands, "time", SimpleNamespace(sleep=lambda _: None))
    observed: list[tuple[Effect, EffectResult | ReadUnavailable]] = []
    grants: list[str] = []

    def execute_original(effect: Effect, authorize: WriteAuthority) -> EffectResult:
        if effect.operation != "push":
            return execute(effect, authorize)

        def prepare_grant():
            final_admission = authorize.prepare()
            def current_grant() -> None:
                final_admission()
                grants.append(effect.effect_id)
            return current_grant

        try:
            result = native.execute(effect, WriteAuthority(prepare_grant))
        except ReadUnavailable as error:
            observed.append((effect, error))
            raise
        observed.append((effect, result))
        return result

    monkeypatch.setattr(runner.ports, "execute", execute_original)
    monkeypatch.setattr(runner.ports, "reconcile", lambda effect:
                        native.reconcile(effect) if effect.operation == "push" else reconcile(effect))
    step = runner.run_once()
    assert len(observed) == 1
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
        prepare_scripted_agent(scenario.kernel, runner.run_once(agent_capacity=0))  # Only read back this original intent.
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
        assert "actual transport command exited nonzero" in failures[0]["evidence"]["message"]
    # Consuming this original result and advancing once cannot repeat the push
    # or discard the physical failure proved before successful readback.
    assert prepare_scripted_agent(scenario.kernel, runner.run_once(agent_capacity=0)).kind == "progress"
    with scenario.kernel.storage.transaction(write=False) as tx:
        assert tuple(tx.one("SELECT status,attempts FROM effects WHERE id=?", (effect.effect_id,))) == ("confirmed", 1)
        assert len(tx.rows("SELECT position FROM history WHERE kind='effect_failed' AND identity=?",
                           (effect.effect_id,))) == int(bool(command_exit))
    prepare_scripted_agent(scenario.kernel, runner.run_once(agent_capacity=0))
    assert _failures(scenario) == [{**failure, "status": "confirmed"} for failure in failures]
    assert len(trace.read_text().splitlines()) == 1


def test_offline_lost_response_is_unknown_without_an_observed_command_failure(
    monkeypatch: pytest.MonkeyPatch, parent_before_push,
) -> None:
    scenario = parent_before_push
    scenario.kernel.clock = lambda: 100.25
    scenario.port.lose_response.add("pr_write")
    make_ports = scenario.make_ports
    physical_exceptions: list[tuple[str, str, int | None]] = []

    def capture_ports(binding):
        ports = make_ports(binding)
        execute = ports.execute
        def capture(effect, authorize):
            try:
                return execute(effect, authorize)
            except ReadUnavailable as error:
                physical_exceptions.append((type(error).__name__, str(error), getattr(error, "write_returncode", None)))
                raise

        ports.execute = capture
        return ports
    monkeypatch.setattr(scenario, "make_ports", capture_ports)
    step = scenario.run()
    assert physical_exceptions == [("ReadUnavailable", "pr_write took effect; response lost", None)]
    assert (step.kind, step.reason) == ("waiting", "github_convergence")
    pending = scenario.kernel.status(scenario.delivery.key)["pending_write"]
    assert pending["operation"] == "pr_write" and pending["status"] == "sent"
    assert sum(row["operation"] == "pr_write" for row in scenario.port.writes()) == 1
    assert _failures(scenario) == []
    with scenario.kernel.storage.transaction(write=False) as tx:
        row = tx.one("SELECT evidence FROM waits WHERE subject=?", (scenario.delivery.key,))
        diagnostic = json.loads(tx.get(row["evidence"], scenario.delivery.key))
    assert diagnostic == {"code": "write_response_unavailable", "message": "pr_write took effect; response lost"}
    status = read(scenario.kernel.storage.path, 'status', storage=scenario.kernel.storage).data
    assert status["supervision_windows"][0]["latest_observation"] == diagnostic
    assert scenario.runner().run_once().kind == "waiting"
    scenario.kernel.clock = lambda: 106.25
    assert scenario.run().kind == "approval"
    assert _failures(scenario) == []
    assert sum(row["operation"] == "pr_write" for row in scenario.port.writes()) == 1
    assert any(event["kind"] == "effect_readback" and event["identity"] == pending["id"]
               for event in scenario.kernel.history(scenario.delivery.key))
    public = read(scenario.kernel.storage.path, 'history', storage=scenario.kernel.storage).data["history"]
    events = [event for event in public["events"] if event.get("evidence") == diagnostic]
    assert len(events) == 1 and pending["id"] in events[0]["identity"]
    assert public["effect_failures"] == []
    with scenario.kernel.storage.transaction(write=False) as tx:
        assert tuple(tx.one("SELECT attempts,status FROM effects WHERE id=?", (pending["id"],))) == (1, "confirmed")
        assert not tx.rows("SELECT subject FROM waits")
        diagnostics = [json.loads(row[0]) for row in tx.rows(
            "SELECT body FROM material WHERE subject=? AND purpose='github_read_observation'", (scenario.delivery.key,))]
    assert diagnostic in diagnostics  # Settling target does not erase the original uncertainty.
