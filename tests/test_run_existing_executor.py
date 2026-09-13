from __future__ import annotations

import json
import os
import select
import subprocess
import sys
from pathlib import Path

import pytest

from agent_run.state import StateStore
from agent_run.task_control import TaskControlStore, TaskKey
from cli_run_supervision_support import _parent_only_agents
from conftest import write_fixture
from test_cli import PROJECT_ROOT, run_cli, stdout_json
from test_run_lifecycle import _file_snapshot, _isolated_environment
from test_ticket_191_resume import _fail_parent_run, _resumed_parent_agents


_GATED_ACTION = """
import os, sys
from agent_run.cli import main
from agent_run.run_driver import RunDriver
from agent_run.task_control import TaskControlStore
ready, release = int(sys.argv[1]), int(sys.argv[2])
def barrier():
    os.write(ready, b'1')
    os.close(ready)
    assert os.read(release, 1) == b'1'
    os.close(release)
if sys.argv[4] == 'approve':
    original = TaskControlStore.complete_action
    def complete(self, *args, **kwargs):
        if sys.argv[3] == 'unresolved':
            barrier()
        result = original(self, *args, **kwargs)
        if sys.argv[3] != 'unresolved':
            barrier()
        return result
    TaskControlStore.complete_action = complete
elif sys.argv[3] == 'unresolved':
    original = TaskControlStore.complete_action
    def complete(self, *args, **kwargs):
        barrier()
        return original(self, *args, **kwargs)
    TaskControlStore.complete_action = complete
else:
    original = RunDriver.advance
    def advance(self, *args, **kwargs):
        barrier()
        return original(self, *args, **kwargs)
    RunDriver.advance = advance
raise SystemExit(main(sys.argv[4:]))
"""

_OBSERVE_RUN = """
import json, os, select, sys
from pathlib import Path
from agent_run.cli import main
from agent_run.executor_host import FakeExecutorHost, HostObservation
from agent_run.run_lifecycle import RunLifecycle
from agent_run.state import StateStore
def forbidden(*args, **kwargs):
    raise AssertionError('observing run must not prepare environment or start Executor')
original = RunLifecycle.__init__
def initialize(self, *args, **kwargs):
    kwargs['prepare_executor_session'] = forbidden
    kwargs['startup_timeout'] = 0
    original(self, *args, **kwargs)
RunLifecycle.__init__ = initialize
FakeExecutorHost.ensure = forbidden
if sys.argv[1] == 'unknown':
    FakeExecutorHost.inspect = lambda self, spec, control: HostObservation(
        'unknown', spec.generation, None, False, 'fixture ownership unknown'
    )
elif sys.argv[1] == 'completed_during_inspect':
    original_inspect = FakeExecutorHost.inspect
    def inspect(self, spec, control):
        FakeExecutorHost.inspect = original_inspect
        os.write(int(sys.argv[2]), b'1')
        executor_fd = int(sys.argv[3])
        assert select.select([executor_fd], [], [], 10)[0] == [executor_fd]
        Path(sys.argv[4]).write_text(json.dumps({
            'control': control.load(spec.task),
            'state': StateStore(Path.cwd() / '.agent-run').load_run(spec.run_id),
            'fixture': json.loads(Path(sys.argv[-1]).read_text()),
        }))
        return original_inspect(self, spec, control)
    FakeExecutorHost.inspect = inspect
raise SystemExit(main(sys.argv[5:]))
"""


@pytest.mark.skipif(not hasattr(os, "pidfd_open"), reason="requires Linux pidfd")
@pytest.mark.parametrize("action", ["resume", "approve"])
@pytest.mark.parametrize(
    "boundary", ["completed", "unresolved", "unknown", "completed_during_inspect"]
)
def test_run_observes_existing_executor_after_other_lifecycle_actions(
    git_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
    boundary: str,
) -> None:
    environment = _isolated_environment(tmp_path / "environment")
    environment["XDG_RUNTIME_DIR"] = str(tmp_path / "runtime")
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(PROJECT_ROOT / "src")
    fixture = write_fixture(git_repo / "github.json", issues={})
    if action == "resume":
        _fail_parent_run(git_repo, fixture)
        agents = _resumed_parent_agents(git_repo)
    else:
        agents = _parent_only_agents(git_repo / "agents.json")
        initial = run_cli(
            git_repo, fixture, "run", "1", "--agent-fixture", str(agents)
        )
        assert initial.returncode == 0, initial.stdout
        assert stdout_json(initial)["status"] == "parent_approval_pending"

    ready_read, ready_write = os.pipe()
    release_read, release_write = os.pipe()
    first = subprocess.Popen(
        [
            sys.executable, "-c", _GATED_ACTION,
            str(ready_write), str(release_read), boundary,
            action, "1", "--json", "--agent-fixture", str(agents),
            "--github-fixture", str(fixture),
        ],
        cwd=git_repo, env=environment, pass_fds=(ready_write, release_read),
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    os.close(ready_write)
    os.close(release_read)
    executor_fd: int | None = None
    try:
        ready, _, _ = select.select([ready_read], [], [], 5)
        assert ready == [ready_read]
        assert os.read(ready_read, 1) == b"1"
        control = TaskControlStore(git_repo / ".agent-run")
        task = TaskKey(git_repo, "example/project", 1)
        record = control.load(task)
        assert record is not None
        assert record["action"]["kind"] == action
        assert record["action"]["status"] == (
            "applying" if boundary == "unresolved" else "completed"
        )
        assert record["executor"]["status"] == "running"
        executor_fd = os.pidfd_open(record["executor"]["pid"])
        state = StateStore(git_repo / ".agent-run").load_run(record["run_id"])
        assert state is not None
        roots = (
            git_repo / ".agent-run", tmp_path / "environment", tmp_path / "runtime"
        )
        before = {str(root): _file_snapshot(root) for root in roots}
        fixture_before = fixture.read_bytes()
        agents_before = agents.read_bytes()
        inspection_snapshot = tmp_path / "inspection-snapshot.json"

        observed = subprocess.run(
            [
                sys.executable, "-c", _OBSERVE_RUN, boundary,
                str(release_write), str(executor_fd), str(inspection_snapshot),
                "run", "1", "--json", "--github-fixture", str(fixture),
            ],
            cwd=git_repo, env=environment, text=True, capture_output=True,
            timeout=15, check=False, pass_fds=(release_write, executor_fd),
        )
        output = json.loads(observed.stdout)
        completed = boundary in {"completed", "completed_during_inspect"}
        assert observed.returncode == (0 if completed else 2), (
            observed.stdout, observed.stderr
        )
        if completed:
            assert output["run_id"] == state["run_id"]
            assert output["action"]["submission"] == "attached"
            audit = output["action_audit"]
            assert audit["action_id"] == record["action"]["action_id"]
            assert audit["executor_generation"] == record["executor"]["generation"]
            assert output["action_audit"]["handshake"] is True
        else:
            assert output["diagnostics"][0]["code"] == (
                "action_busy" if boundary == "unresolved" else "executor_start_unknown"
            )
        if boundary == "completed_during_inspect":
            finished = json.loads(inspection_snapshot.read_text())
            assert output["status"] == (
                "parent_approval_pending" if action == "resume" else "completed"
            )
            assert control.load(task) == finished["control"]
            final_state = StateStore(git_repo / ".agent-run").load_run(
                record["run_id"]
            )
            assert final_state == finished["state"]
            assert json.loads(fixture.read_text()) == finished["fixture"]
            assert not any(
                item.get("code") == "session_interrupted"
                for item in finished["state"].get("diagnostics", [])
            )
        else:
            if completed:
                assert output["status"] == state["status"]
            assert {str(root): _file_snapshot(root) for root in roots} == before
            assert fixture.read_bytes() == fixture_before
            assert select.select([executor_fd], [], [], 0)[0] == []
        assert agents.read_bytes() == agents_before
    finally:
        os.close(ready_read)
        try:
            os.write(release_write, b"1")
        except BrokenPipeError:
            pass
        os.close(release_write)
        stdout, stderr = first.communicate(timeout=10)
        if executor_fd is not None:
            try:
                assert select.select([executor_fd], [], [], 10)[0] == [executor_fd]
            finally:
                os.close(executor_fd)
    assert first.returncode == 0, (stdout, stderr)
