"""Opt-in real user-systemd journeys; invoke this file explicitly with pytest.

GitHub and Codex are local executable fixtures. Host launch, credentials,
processes, fd-store, cgroups and public CLI are the production implementations.
An unavailable manager is a failed precondition, never a simulated lifecycle.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from agent_run.adapters.systemd import SystemdHost
from agent_run.adapters.workspace import ManagedWorkspace
from agent_run.core.kernel import Kernel
from agent_run.runtime.package_lease import default_runner_lock_path, runner_management_lease
from agent_run.storage.routing import RepositoryRoute
from support.systemd_manager import _CODEX, _control_environment


# Capture before the ordinary per-test HOME/XDG isolation replaces user paths.
_BUS = os.environ.get("DBUS_SESSION_BUS_ADDRESS", f"unix:path=/run/user/{os.geteuid()}/bus")


def _wait(read, *, timeout=90):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = read()
        if last:
            return last
        time.sleep(.1)
    raise AssertionError(f"Real systemd journey did not settle: {last!r}")


def _backend(*, hold_development):
    classification = """schema = json.loads(Path(sys.argv[sys.argv.index('--output-schema') + 1]).read_text())
role = 'review' if 'checks' in schema['properties'] else 'publication' if 'commit_message' in schema['properties'] else 'development'
"""
    program = _CODEX[:_CODEX.index("if prompt.startswith")] + classification + _CODEX[_CODEX.index("thread = "):]
    if hold_development:
        # Pause the first real Invocation after emitting the Thread identity.
        # The marker survives SIGKILL so the resumed Worker can finish normally.
        program = program.replace("content = 'Implementation", """if role == 'development':
    marker = Path(os.environ['ENTRY_ROLE_TRACE'] + '.held')
    if not marker.exists():
        marker.write_text('First invocation reached its real thread boundary.\\n')
        import time
        while True:
            time.sleep(1)
content = 'Implementation""")
    return program


class LiveRun:
    def __init__(self, directory, environment, state):
        self.directory, self.environment, self.state = directory, environment, state
        self.route = RepositoryRoute.for_workspace(ManagedWorkspace.for_repository("example/project"))

    def systemctl(self, *arguments, check=True):
        result = subprocess.run(["systemctl", "--user", *arguments], env=self.environment,
                                capture_output=True, text=True, timeout=20)
        if check:
            assert result.returncode == 0, (result.stdout, result.stderr)
        return result

    def fields(self, unit):
        result = self.systemctl("show", unit, "--property=LoadState,ActiveState,ControlGroup,MainPID", check=False)
        return dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)

    def kernel(self):
        if not self.route.database.exists():
            return None
        with self.route.metadata().transaction(write=False) as tx:
            rows = tx.rows("SELECT id FROM runs")
        assert len(rows) == 1, "This live test must own exactly one isolated Run"
        return Kernel._from_storage(self.route.at_run(rows[0]["id"]))

    def cli(self, action, selector="1"):
        result = subprocess.run([sys.executable, "-m", "agent_run", action, selector,
            "--repo", "example/project", "--json", *(["--no-notifications"] if action == "run" else [])],
            cwd=self.directory, env=self.environment, capture_output=True, text=True, timeout=120)
        assert result.returncode == 0, (result.returncode, result.stdout, result.stderr)
        return json.loads(result.stdout)

    def at_approval(self):
        kernel = self.kernel()
        if kernel is not None and kernel.status("parent:1")["phase"] == "approval":
            return kernel
        return None

    def retired(self, request, *, failed=False, original_cgroup=None):
        fields = self.fields(request.unit_name)
        if failed:
            if fields.get("LoadState") != "loaded" or fields.get("ActiveState") != "failed":
                return False
            # systemd may clear ControlGroup after removing it. Compare the
            # exact running Host's recorded path, never an empty default field.
            assert original_cgroup and original_cgroup.startswith("/")
            return not (Path("/sys/fs/cgroup") / original_cgroup.lstrip("/")).exists()
        return fields.get("LoadState") == "not-found"

    def close(self):
        kernel = self.kernel()
        if kernel is None:
            return
        with kernel.storage.transaction(write=False) as tx:
            units = [row[0] for row in tx.rows("SELECT unit_name FROM hosts WHERE run_id=?", (kernel.storage.run_id,))]
        for unit in units:
            assert unit and unit.startswith("agent-run-kernel-") and unit.endswith(".service")
            self.systemctl("stop", unit, check=False)
            self.systemctl("reset-failed", unit, check=False)
            _wait(lambda: self.fields(unit).get("LoadState") == "not-found", timeout=15)
        # This is independent physical verification after all unit hooks finish.
        with runner_management_lease(default_runner_lock_path(self.environment)):
            pass


@contextmanager
def _live_run(git_repo, tmp_path, monkeypatch, *, hold_development=False):
    environment, state = _control_environment(git_repo, tmp_path, monkeypatch,
                                             codex=_backend(hold_development=hold_development), tickets=())
    environment["DBUS_SESSION_BUS_ADDRESS"] = _BUS
    manager = SystemdHost(systemctl="systemctl", systemd_run="systemd-run", environment=environment)
    manager.preflight()  # Explicit failure before creating any Run or service.
    live = LiveRun(git_repo, environment, state)
    try:
        yield live
    finally:
        live.close()


def _retained(kernel):
    with kernel.storage.transaction(write=False) as tx:
        return {table: [tuple(row) for row in tx.rows(f"SELECT * FROM {table} WHERE run_id=?", (kernel.storage.run_id,))]
                for table in ("attempts", "threads", "invocation_outputs", "checks")}


@pytest.mark.parametrize("lost_reply", ["merge", "close"])
def test_approve_after_real_unit_collection(git_repo, tmp_path, monkeypatch, lost_reply):
    with _live_run(git_repo, tmp_path, monkeypatch) as live:
        started = live.cli("run")
        kernel = _wait(live.at_approval)
        original = kernel.host_request(started["action_id"])
        usage = kernel.host_usage(started["action_id"])
        assert usage is not None and usage.main_pid is not None
        _wait(lambda: live.retired(original), timeout=30)
        assert not Path(f"/proc/{usage.main_pid}").exists()
        before, budget = _retained(kernel), kernel.status("parent:1")["budgets"]
        remote = json.loads(live.state.read_text())
        remote["lose_operation"] = lost_reply
        live.state.write_text(json.dumps(remote))
        approved = live.cli("approve", original.run_id)
        assert approved["run_id"] == original.run_id and approved["action"] == "approve"
        assert approved["status"] == "action_complete"
        assert kernel.status("parent:1")["phase"] == "complete"
        assert kernel.status("parent:1")["budgets"] == budget
        after = _retained(kernel)
        assert all(row in after[table] for table, rows in before.items() for row in rows)
        remote = json.loads(live.state.read_text())
        assert remote["source_graph"]["parent"]["state"] == "CLOSED"
        assert len(remote["prs"]) == 1 and remote["prs"][0]["merged"]
        for action in ("approve", "resume"):
            repeated = live.cli(action, original.run_id)
            assert repeated["result"] == "completed"
            assert repeated["request_outcome"] == "already_completed"
            assert repeated["requested_action_applied"] is False
            assert repeated["requested_action_succeeded"] is True
            assert repeated["action_id"] == approved["action_id"]
        calls = [json.loads(line) for line in Path(live.environment["REMOTE_TEST_CALLS"]).read_text().splitlines()]
        assert sum("PUT" in call and any(value.endswith("/merge") for value in call) for call in calls) == 1
        assert sum("PATCH" in call and "repos/example/project/issues/1" in call for call in calls) == 1


def test_resume_after_real_failed_unit_loses_cgroup(git_repo, tmp_path, monkeypatch):
    with _live_run(git_repo, tmp_path, monkeypatch, hold_development=True) as live:
        started = live.cli("run")
        kernel = live.kernel()
        assert kernel is not None
        original = kernel.host_request(started["action_id"])

        def original_thread():
            with kernel.storage.transaction(write=False) as tx:
                rows = tx.rows("SELECT a.id,a.thread_id,t.backend_id FROM attempts a JOIN threads t "
                    "ON t.run_id=a.run_id AND t.id=a.thread_id WHERE a.run_id=? AND a.role='development'", (original.run_id,))
                return tuple(rows[0]) if len(rows) == 1 and rows[0][2] else None

        attempt = _wait(original_thread)
        budget = kernel.status("parent:1")["budgets"]
        usage = kernel.host_usage(started["action_id"])
        assert usage is not None and usage.cgroup
        live.systemctl("kill", "--kill-whom=main", "--signal=SIGKILL", original.unit_name)
        _wait(lambda: live.retired(original, failed=True, original_cgroup=usage.cgroup), timeout=30)
        resumed = live.cli("resume", original.run_id)
        assert resumed["run_id"] == original.run_id and resumed["action"] == "resume"
        assert resumed["action_id"] != started["action_id"]
        _wait(live.at_approval)
        assert original_thread() == attempt
        assert kernel.status("parent:1")["budgets"]["requirements"]["developments_used"] == budget["requirements"]["developments_used"]
        assert not json.loads(live.state.read_text())["prs"][0]["merged"]
        with kernel.storage.transaction(write=False) as tx:
            assert not tx.rows("SELECT id FROM grants WHERE run_id=?", (original.run_id,))
