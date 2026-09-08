from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent_run.executor_host import ExecutorSpec
from agent_run.run_lifecycle import prepare_action_application_receipt
from agent_run.state import StateStore
from agent_run.systemd_executor_host import (
    FakeSystemdTransport,
    SystemdUnitObservation,
    SystemdUserExecutorHost,
)
from agent_run.task_control import TaskControlStore, TaskKey
from conftest import write_fixture
from test_cli import PROJECT_ROOT
from test_run_lifecycle import _file_snapshot, _isolated_environment
from test_ticket_191_resume import _fail_parent_run


_OBSERVE = """
import json, sys
from itertools import count
from pathlib import Path
from agent_run import cli
from agent_run.executor import DeliveryExecutor
from agent_run.github_fixture import FixtureGitHubReader, FixtureGitHubPublisher
from agent_run.run_lifecycle import RunLifecycle
from agent_run.systemd_executor_host import (
    FakeSystemdTransport, SystemdUnitObservation, SystemdUserExecutorHost,
)
fixture, proof, evidence = map(Path, sys.argv[1:4])
native = json.loads(proof.read_text())
class Transport(FakeSystemdTransport):
    inspections = 0
    def inspect(self, unit):
        self.inspections += 1
        return super().inspect(unit)
transport = Transport(unit=SystemdUnitObservation(**native))
def forbidden(*args, **kwargs):
    raise AssertionError('observation must not prepare environment or execute work')
def host(**options):
    value = SystemdUserExecutorHost(transport=transport, **options)
    value.prepare_environment = forbidden
    return value
original_init = RunLifecycle.__init__
def initialize(self, *args, **kwargs):
    kwargs.update(clock=count().__next__, startup_timeout=0, sleep=forbidden)
    original_init(self, *args, **kwargs)
RunLifecycle.__init__ = initialize
DeliveryExecutor.execute = forbidden
cli._running_active_runner = lambda: True
cli.SystemdUserExecutorHost = host
cli.GhGitHubReader = lambda repo, *, working_directory: FixtureGitHubReader(fixture)
cli.GhGitHubPublisher = lambda repo, git: FixtureGitHubPublisher(fixture, git)
try:
    result = cli.main(sys.argv[4:])
finally:
    evidence.write_text(json.dumps({
        'inspections': transport.inspections, 'starts': transport.start_count,
    }))
raise SystemExit(result)
"""


@pytest.mark.parametrize("invocation_status", ["running", "resuming"])
@pytest.mark.parametrize(
    ("host_status", "exit_proof_persisted", "source_action"),
    [
        pytest.param(status, False, "run", id=status)
        for status in ("exited", "absent", "running", "unknown", "conflict")
    ] + [
        pytest.param(status, True, "run", id=f"persisted-{status}")
        for status in ("exited", "absent")
    ] + [
        pytest.param(status, True, "revise", id=f"revise-persisted-{status}")
        for status in ("exited", "absent")
    ],
)
def test_repeated_public_run_checks_host_while_invocation_is_active(
    git_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invocation_status: str,
    host_status: str,
    exit_proof_persisted: bool,
    source_action: str,
) -> None:
    environment = _isolated_environment(tmp_path / "environment")
    environment["XDG_RUNTIME_DIR"] = str(tmp_path / "runtime")
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    fixture = write_fixture(git_repo / "github.json", issues={})
    _fail_parent_run(git_repo, fixture)
    states = StateStore(git_repo / ".agent-run")
    state = states.find_unfinished_runs("example/project", 1)[0]
    task = TaskKey(git_repo, "example/project", 1)
    control = TaskControlStore(states.root)
    initial_control = control.load(task)
    assert initial_control is not None
    # Build an already-applied origin Action at the canonical receipt seam;
    # the behavior under test is the subsequent independent public run command.
    payload = initial_control["action"]["payload"] if source_action == "run" else {
        "parent": 1, "run_id": state["run_id"], "message": "fixture revision",
    }
    claim = control.claim_action(task, kind=source_action, payload=payload)
    assert claim.action_id is not None
    control.bind_run(task, claim.action_id, state["run_id"])
    record = control.load(task)
    assert record is not None
    spec = ExecutorSpec(
        task=task, run_id=state["run_id"], action_id=claim.action_id,
        generation=record["action"]["executor_generation"],
        state_root=states.root, command=("run", "1"), cwd=git_repo,
    )
    transport = FakeSystemdTransport()
    host = SystemdUserExecutorHost(
        runtime_directory=tmp_path / "setup-runtime", environment=dict(os.environ),
        executor_python=Path(sys.executable), transport=transport,
    )
    assert host.ensure(spec, control).status == "starting"
    control.mark_handshake(
        task, action_id=spec.action_id, generation=spec.generation,
        pid=os.getpid(), process_start_token=None,
    )
    state["status"] = "parent_delivery_pending"
    state["terminal_kind"] = None
    state["diagnostics"] = []
    invocation = state["active_agent_invocation"]
    assert isinstance(invocation, dict)
    invocation.update(status=invocation_status, ended_at=None, error=None)
    state["agent_invocation_history"][-1] = copy.deepcopy(invocation)
    record = control.load(task)
    prepare_action_application_receipt(state, record["action"])
    states.save_run(state["run_id"], state)
    control.record_application(
        task, action_id=spec.action_id, run_id=spec.run_id, generation=spec.generation,
        payload_digest=record["action"]["payload_digest"],
    )
    control.complete_action(
        task, action_id=spec.action_id, generation=spec.generation,
        result_status=state["status"],
    )
    description = str(transport.launches[0]["description"])
    transport.unit = SystemdUnitObservation("running", description, os.getpid(), None)
    assert host.inspect(spec, control).status == "running"
    native = {
        "status": "exited" if host_status == "conflict" else host_status,
        "description": (
            None if host_status in {"absent", "unknown"}
            else "another Executor" if host_status == "conflict" else description
        ),
        "pid": os.getpid() if host_status == "running" else None,
        "reason": (
            "fixture signal exit"
            if host_status == "exited" and not exit_proof_persisted else None
        ),
    }
    if exit_proof_persisted:
        # Stop at the durable Host-exit boundary before lifecycle can record
        # session_interrupted; the next independent CLI must finish that work.
        transport.unit = SystemdUnitObservation(**native)
        assert host.inspect(spec, control).status == host_status
        exited = control.load(task)
        assert exited["executor"]["status"] == "exited"
        assert exited["executor"].get("failure") is None
    proof = tmp_path / "native.json"
    proof.write_text(json.dumps(native))
    process_environment = os.environ.copy()
    process_environment["PYTHONPATH"] = str(PROJECT_ROOT / "src")
    original_control = control.load(task)
    original_runs = _file_snapshot(states.root / "runs")
    original_worktrees = _file_snapshot(states.root / "worktrees")
    fixture_before = fixture.read_bytes()
    previous_control: bytes | None = None
    previous_runs: dict[str, bytes] | None = None
    for index in range(2):
        evidence = tmp_path / f"observation-{index}.json"
        result = subprocess.run(
            [
                sys.executable, "-c", _OBSERVE, str(fixture), str(proof), str(evidence),
                "run", "1", "--repo", "example/project", "--json",
                "--agent-fixture", str(git_repo / "failed-agents.json"),
            ],
            cwd=git_repo, env=process_environment, capture_output=True, text=True,
            check=False, timeout=5,
        )
        assert result.returncode == (0 if host_status == "running" else 2), (
            result.stdout, result.stderr
        )
        output = json.loads(result.stdout)
        calls = json.loads(evidence.read_text())
        assert calls["starts"] == 0
        if not exit_proof_persisted and (
            index == 0 or host_status in {"running", "unknown", "conflict"}
        ):
            assert calls["inspections"] > 0
        latest = states.load_current_run(spec.run_id)
        current_control = control.load(task)
        assert latest is not None and current_control is not None
        assert current_control["action"] == original_control["action"]
        assert current_control["executor"]["generation"] == spec.generation
        assert latest["action_application_receipt"] == state["action_application_receipt"]
        assert latest["parent_job"] == state["parent_job"]
        assert latest["active_agent_invocation"]["semantic_attempt"] == invocation["semantic_attempt"]
        assert _file_snapshot(states.root / "worktrees") == original_worktrees
        assert fixture.read_bytes() == fixture_before
        if host_status in {"exited", "absent"}:
            assert output["status"] == "execution_failed"
            assert latest["diagnostics"][0]["code"] == "session_interrupted"
            assert latest["active_agent_invocation"]["status"] == "failed"
            assert current_control["executor"]["failure"] == "session_interrupted"
            assert "resume" in output["next_action"]
        else:
            assert current_control == original_control
            assert _file_snapshot(states.root / "runs") == original_runs
            if host_status != "running":
                assert output["diagnostics"][0]["code"] == "executor_start_unknown"
        if previous_control is not None:
            assert control.path_for(task).read_bytes() == previous_control
            assert _file_snapshot(states.root / "runs") == previous_runs
        previous_control = control.path_for(task).read_bytes()
        previous_runs = _file_snapshot(states.root / "runs")
    assert not list((tmp_path / "runtime").rglob("*.json"))
