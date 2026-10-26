"""Real cleanup timeout after normal delivery; no database/proof mutations."""
from __future__ import annotations
from support.core_operations import prepare_scripted_agent

from contextlib import contextmanager
import ctypes
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

from agent_run.runtime.process_identity import _process_start_token
from agent_run.runtime.process_cleanup import child_subreaper
from agent_run.model import DeliveryKind
from agent_run.core.kernel import Kernel
from agent_run.errors import KernelError
import agent_run.adapters.cleanup as cleanup
from support.worker_process_timing import wait_until
from support.cleanup_scenarios import _native_scenario, _local_head


def _kill_owned_group(root: Path) -> None:
    """Failure fallback only: signal a group while an exact recorded member lives."""
    records = root / "git.json"
    if not records.exists():
        return
    git = json.loads(records.read_text())
    members = [git]
    if (root / "hook.json").exists():
        members.append(json.loads((root / "hook.json").read_text()))
    for member in members:
        if _process_start_token(member["pid"]) == member["token"]:
            try:
                if os.getpgid(member["pid"]) == git["pid"]:
                    os.killpg(git["pid"], signal.SIGKILL)
                    return
            except ProcessLookupError:
                pass


def _reap_created(root: Path, name: str) -> int | None:
    if not (root / name).exists():
        return None
    hook = json.loads((root / name).read_text())

    def exited():
        current = _process_start_token(hook["pid"])
        if current is None or current != hook["token"]:
            return True
        try:
            return os.waitid(os.P_PID, hook["pid"], os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None
        except ChildProcessError:
            # A zombie may still belong to its dying parent until adoption.
            return _process_start_token(hook["pid"]) != hook["token"]

    # Observe both exit and ownership without consuming the exact wait status.
    wait_until(exited, timeout=3)
    if _process_start_token(hook["pid"]) != hook["token"]:
        return None  # The original process is gone; never wait on a reused PID.
    try:
        pid, status = os.waitpid(hook["pid"], os.WNOHANG)
    except ChildProcessError:
        assert _process_start_token(hook["pid"]) != hook["token"]
        return None
    assert pid == hook["pid"] and os.WIFSIGNALED(status)
    (root / ("reaped-" + name)).write_text(json.dumps({
        "pid": pid, "token": hook["token"], "reaper": os.getpid(),
        "wait_status": status, "signal": os.WTERMSIG(status),
    }))
    return os.WTERMSIG(status)


def _cleanup_created(root: Path) -> None:
    errors, attempts = [], []
    try:
        _kill_owned_group(root)
    except BaseException as error:
        errors.append(error)
    for name in ("git.json", "hook.json"):
        try:
            attempts.append({"name": name, "signal": _reap_created(root, name)})
        except BaseException as error:
            attempts.append({"name": name, "error": repr(error)})
            errors.append(error)
    with (root / "cleanup-attempts.jsonl").open("a") as stream:
        stream.write(json.dumps({"caller": os.getpid(), "attempts": attempts}) + "\n")
    if errors:
        raise BaseExceptionGroup("Recorded descendant cleanup failed", errors)


def _subreaper_state() -> int:
    value = ctypes.c_int()
    if ctypes.CDLL(None, use_errno=True).prctl(37, ctypes.byref(value), 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "Cannot read process subreaper state")
    return value.value


def _controller_output(command, root, environment, *, timeout=60, grace=5, ready=None):
    previous = _subreaper_state()
    during = None
    try:
        # The outer owner survives even a SIGKILL of the inner subreaper.
        with child_subreaper():
            during = _subreaper_state()
            if during != 1:
                raise RuntimeError("The isolated test requires child adoption")
            controller = subprocess.Popen(command, env=environment, stdout=subprocess.PIPE,
                                          stderr=subprocess.PIPE, text=True, start_new_session=True)
            stdout = stderr = ""
            hard_kill = False
            try:
                if ready is not None:
                    ready()
                stdout, stderr = controller.communicate(timeout=timeout)
            finally:
                try:
                    if controller.poll() is None:
                        controller.send_signal(signal.SIGINT)
                        try:
                            controller.wait(timeout=grace)
                        except subprocess.TimeoutExpired:
                            hard_kill = True
                            controller.kill()
                            controller.wait(timeout=grace)
                finally:
                    try:
                        _cleanup_created(root)
                    finally:
                        try:
                            stdout, stderr = controller.communicate(timeout=grace)
                        finally:
                            controller.stdout.close()
                            controller.stderr.close()
                            (root / "controller.stdout").write_text(stdout)
                            (root / "controller.stderr").write_text(stderr)
                            (root / "controller-cleanup.json").write_text(json.dumps({
                                "pid": controller.pid, "returncode": controller.returncode,
                                "hard_kill": hard_kill,
                            }))
            return controller, stdout, stderr
    finally:
        (root / "subreaper-scope.json").write_text(json.dumps({
            "before": previous, "during": during, "after": _subreaper_state(),
        }))


def _exercise(root: Path) -> None:
    scenario = _native_scenario(root / "scenario", DeliveryKind.TICKET)
    kernel, subject = scenario.kernel, scenario.delivery.key
    # Normal Scenario agents feed the same Core runner; stop before cleanup IO.
    for _ in range(200):
        if kernel.status(subject)["phase"] == "complete":
            break
        step = scenario.step()
        assert step.kind not in {"paused", "waiting", "approval"}, step
    else:
        pytest.fail("Normal producer did not finish Ticket business")
    actual_execute, actual_popen = scenario.port.execute, subprocess.Popen
    actual_hold = cleanup._hold_ref
    hook = scenario.port.git.root / ".git" / "hooks" / "reference-transaction"
    assert not hook.exists()
    attempts, timeouts, physical = [], [], []
    before = {}

    def periods():
        with kernel.storage.transaction(write=False) as tx:
            return [dict(row) for row in tx.all("SELECT * FROM invocations ORDER BY id")]

    def popen(command, *args, **kwargs):
        process = actual_popen(command, *args, **kwargs)
        if command[:2] == ["git", "update-ref"] and "--stdin" in command:
            assert not (root / "git.json").exists()
            (root / "git.json").write_text(json.dumps({"pid": process.pid,
                "token": _process_start_token(process.pid), "group": os.getpgid(process.pid)}))
        return process

    @contextmanager
    def observe_timeout(*args, **kwargs):
        started = time.monotonic()
        try:
            with actual_hold(*args, **kwargs):
                yield
        except KernelError as error:
            timeouts.append({"code": error.code, "detail": str(error), "seconds": time.monotonic() - started})
            (root / "timeout.json").write_text(json.dumps(timeouts[-1]))
            raise

    def execute(effect, authorize=None):
        selected = effect.operation == "cleanup_worktree" and effect.subject == subject
        if selected:
            attempts.append(effect)
            assert len(attempts) == 1
            status = kernel.status(subject)
            assert status["phase"] == "complete"
            checkout = Path(effect.payload["checkout"])
            common = Path(effect.payload["common_git_dir"])
            head_lock = scenario.port.git.run("rev-parse", "--path-format=absolute", "--git-path", "HEAD.lock", cwd=checkout).stdout.strip()
            before.update(budgets=status["budgets"], calls=list(scenario.role_calls), periods=periods(),
                          head=scenario.port.git.run("rev-parse", "HEAD", cwd=checkout).stdout,
                          local=_local_head(scenario, effect.source_ref), remote=scenario.port.remote_refs(),
                          refs=scenario.port.git.run("for-each-ref", "--format=%(refname) %(objectname)").stdout)
            hook.write_text(f"#!{sys.executable}\n" +
                f"import json,os,signal,sys\nfrom pathlib import Path\nfrom agent_run.runtime.process_identity import _process_start_token\nif sys.argv[1] == 'prepared' and Path({head_lock!r}).exists() and not Path({str(root / 'hook.json')!r}).exists():\n" +
                f"    locks=[str(p) for p in Path({str(common)!r}).rglob('*.lock')]\n" +
                f"    Path({str(root / 'hook.json')!r}).write_text(json.dumps(dict(pid=os.getpid(),token=_process_start_token(os.getpid()),parent=os.getppid(),group=os.getpgrp(),locks=locks)))\n"
                "    signal.pause()\n")
            hook.chmod(0o700)
            patch.setattr(cleanup.subprocess, "Popen", popen)
            patch.setattr(cleanup, "_hold_ref", observe_timeout)
        result = actual_execute(effect, authorize)
        if selected:
            physical.append({"disposition": result.disposition, "raw": dict(result.raw)})
            (root / "physical-result.json").write_text(json.dumps(physical[-1]))
        return result

    with child_subreaper(), pytest.MonkeyPatch.context() as patch:
        patch.setattr(scenario.port, "execute", execute)
        try:
            runner = scenario.runner()
            for _ in range(40):
                step = prepare_scripted_agent(kernel, runner.run_once(agent_capacity=0))
                assert step.call is None
                if attempts:
                    break
            else:
                pytest.fail("Normal runner did not reach Ticket cleanup")
            assert len(timeouts) == len(attempts) == 1
            assert physical[0]["disposition"] == "unknown"
            assert timeouts[0]["code"] == "cleanup_lock_timeout"
            assert 5 <= timeouts[0]["seconds"] < 30
            original = attempts[0]
            checkout = Path(original.payload["checkout"])
            durable = Kernel(kernel.storage.path).status(subject)
            assert durable["phase"] == "complete" and durable["budgets"] == before["budgets"]
            assert durable["resource_cleanup"]["state"] == "pending"
            assert durable["resource_cleanup"]["effect_id"] == original.effect_id
            # The regular port may already read back prior after the timeout.
            assert checkout.is_dir()
            assert scenario.port.git.run("rev-parse", "HEAD", cwd=checkout).stdout == before["head"]
            assert _local_head(scenario, original.source_ref) == before["local"]
            assert scenario.port.remote_refs() == before["remote"]
            assert scenario.port.git.run("for-each-ref", "--format=%(refname) %(objectname)").stdout == before["refs"]
            git, witness = [json.loads((root / name).read_text()) for name in ("git.json", "hook.json")]
            assert witness["parent"] == witness["group"] == git["pid"] == git["group"]
            assert witness["locks"] and all(Path(p).is_relative_to(Path(original.payload["common_git_dir"])) for p in witness["locks"])
            hook_signal = _reap_created(root, "hook.json")
            assert _process_start_token(git["pid"]) != git["token"]
            assert _process_start_token(witness["pid"]) != witness["token"]
            with pytest.raises(ProcessLookupError):
                os.killpg(git["pid"], 0)
            assert all(not Path(p).exists() for p in witness["locks"])
            # Only our newly created hook is removed. Never repair lock files.
            hook.unlink()
            patch.setattr(scenario.port, "execute", actual_execute)
            patch.setattr(cleanup.subprocess, "Popen", actual_popen)
            patch.setattr(cleanup, "_hold_ref", actual_hold)
            runner = scenario.runner()
            for _ in range(40):
                step = prepare_scripted_agent(kernel, runner.run_once(agent_capacity=0))
                assert step.call is None
                if kernel.status(subject)["resource_cleanup"]["state"] == "complete":
                    break
            else:
                pytest.fail("Original cleanup did not complete through normal runner reentry")
            assert not checkout.exists() and _local_head(scenario, original.source_ref) is None
            assert original.source_ref not in scenario.port.remote_refs()
            assert kernel.status(subject)["phase"] == "complete"
            assert kernel.status(subject)["budgets"] == before["budgets"]
            assert periods() == before["periods"] and scenario.role_calls == before["calls"]
            with kernel.storage.transaction(write=False) as tx:
                assert [row[0] for row in tx.all("SELECT id FROM effects WHERE subject=? AND operation='cleanup_worktree'", (subject,))] == [original.effect_id]
            (root / "evidence.json").write_text(json.dumps({"timeout": timeouts[0], "git": git,
                "hook": witness, "hook_signal": hook_signal, "recorded_locks_absent": True,
                "physical_result": physical[0], "durable_outcome_after_readback": durable["resource_cleanup"]["outcome"],
                "original_intent": original.effect_id, "same_periods": True, "same_budgets": True,
                "same_agents": True, "cleanup_complete": True}, indent=2))
        finally:
            if hook.exists():
                hook.unlink()
            _cleanup_created(root)


def test_cleanup_prepare_timeout_preserves_complete_delivery_and_reenters(tmp_path: Path) -> None:
    project = Path(__file__).resolve().parents[1]
    environment = {**os.environ, "PYTHONPATH": os.pathsep.join(str(project / p) for p in ("src", ".", "tests"))}
    controller, stdout, stderr = _controller_output(
        [sys.executable, str(Path(__file__).resolve()), str(tmp_path)], tmp_path, environment)
    assert controller.returncode == 0, stdout + "\n" + stderr
    evidence = json.loads((tmp_path / "evidence.json").read_text())
    assert evidence["cleanup_complete"] and evidence["recorded_locks_absent"]
    print(json.dumps(evidence))


@pytest.mark.parametrize("first_reap_error", [False, True], ids=["normal-reap", "continue-after-error"])
def test_forced_controller_exit_reaps_only_its_recorded_descendants(tmp_path, monkeypatch, first_reap_error):
    """Mechanical cleanup validation only; synthetic Python children, no Core/DB."""
    hook_program = """import json,os,signal,sys
from pathlib import Path
from agent_run.runtime.process_identity import _process_start_token
root=Path(sys.argv[1])
record=root/'hook.tmp'
record.write_text(json.dumps(dict(pid=os.getpid(),token=_process_start_token(os.getpid()))))
record.replace(root/'hook.json')
signal.pause()
"""
    git_program = """import json,os,signal,subprocess,sys
from pathlib import Path
from agent_run.runtime.process_identity import _process_start_token
root=Path(sys.argv[1])
record=root/'git.tmp'
record.write_text(json.dumps(dict(pid=os.getpid(),token=_process_start_token(os.getpid()))))
record.replace(root/'git.json')
subprocess.Popen([sys.executable,'-c',sys.argv[2],str(root)])
signal.pause()
"""
    controller_program = """import signal,subprocess,sys
signal.signal(signal.SIGINT,signal.SIG_IGN)
subprocess.Popen([sys.executable,'-c',sys.argv[2],sys.argv[1],sys.argv[3]],start_new_session=True)
signal.pause()
"""
    project = Path(__file__).resolve().parents[1]
    environment = {**os.environ, "PYTHONPATH": os.pathsep.join(str(project / p) for p in ("src", ".", "tests"))}
    original = _reap_created

    def fail_first(root, name):
        if name == "git.json":
            raise RuntimeError("Synthetic first-reaper error")
        return original(root, name)

    def ready():
        wait_until(lambda: (tmp_path / "hook.json").exists(), timeout=5)

    previous = _subreaper_state()
    with child_subreaper():
        try:
            with monkeypatch.context() as patch:
                if first_reap_error:
                    patch.setitem(globals(), "_reap_created", fail_first)
                expected = BaseExceptionGroup if first_reap_error else subprocess.TimeoutExpired
                with pytest.raises(expected):
                    _controller_output([sys.executable, "-c", controller_program, str(tmp_path),
                                        git_program, hook_program], tmp_path, environment,
                                       timeout=0.05, grace=0.2, ready=ready)
            # A failed first reaper must not prevent the second one's exact wait.
            hook = json.loads((tmp_path / "reaped-hook.json").read_text())
            assert hook["reaper"] == os.getpid() and hook["signal"] == signal.SIGKILL
            if first_reap_error:
                attempts = [json.loads(line) for line in (tmp_path / "cleanup-attempts.jsonl").read_text().splitlines()]
                assert [a["name"] for a in attempts[0]["attempts"]] == ["git.json", "hook.json"]
                assert "error" in attempts[0]["attempts"][0]
                original(tmp_path, "git.json")
            git = json.loads((tmp_path / "reaped-git.json").read_text())
            assert git["reaper"] == os.getpid() and git["signal"] == signal.SIGKILL
            controller = json.loads((tmp_path / "controller-cleanup.json").read_text())
            assert controller["hard_kill"] and controller["returncode"] == -signal.SIGKILL
            scope = json.loads((tmp_path / "subreaper-scope.json").read_text())
            assert scope["during"] == 1 and scope["before"] == scope["after"]
            assert _process_start_token(git["pid"]) != git["token"]
            assert _process_start_token(hook["pid"]) != hook["token"]
            with pytest.raises(ProcessLookupError):
                os.killpg(git["pid"], 0)
            print(json.dumps({"mechanical_only": True, "first_reap_error": first_reap_error,
                              "controller": controller, "git": git, "hook": hook, "subreaper": scope}))
        finally:
            _cleanup_created(tmp_path)
    assert _subreaper_state() == previous


def test_recorded_descendant_reaper_waits_for_real_adoption(tmp_path, monkeypatch):
    """A zombie remains non-waitable until its still-live parent releases it."""
    parent_program = """import json,os,signal,sys
from pathlib import Path
from agent_run.runtime.process_identity import _process_start_token
root=Path(sys.argv[1])
pid=os.fork()
if pid == 0:
    (root/'hook.json').write_text(json.dumps(dict(pid=os.getpid(),token=_process_start_token(os.getpid()))))
    os.kill(os.getpid(),signal.SIGKILL)
os.waitid(os.P_PID,pid,os.WEXITED | os.WNOWAIT)
(root/'zombie-ready').touch()
os.read(int(sys.argv[2]),1)
os._exit(0)
"""
    release_read, release_write = os.pipe()
    released = False
    observed = []
    actual_waitid = os.waitid

    def release_after_non_child(idtype, pid, options):
        nonlocal released
        try:
            return actual_waitid(idtype, pid, options)
        except ChildProcessError:
            if pid == hook["pid"] and not released:
                fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
                assert fields[0] == "Z" and int(fields[1]) == parent.pid
                assert _process_start_token(pid) == hook["token"]
                observed.append("zombie-not-adopted")
                os.write(release_write, b"1")
                released = True
            raise

    with child_subreaper():
        assert _subreaper_state() == 1
        parent = None
        try:
            parent = subprocess.Popen(
                [sys.executable, "-c", parent_program, str(tmp_path), str(release_read)],
                pass_fds=(release_read,), start_new_session=True,
            )
            wait_until(lambda: (tmp_path / "zombie-ready").exists(), timeout=5)
            hook = json.loads((tmp_path / "hook.json").read_text())
            # Observe the real kernel ECHILD boundary; never fake a wait result.
            with monkeypatch.context() as patch:
                patch.setattr(os, "waitid", release_after_non_child)
                assert _reap_created(tmp_path, "hook.json") == signal.SIGKILL
            assert observed == ["zombie-not-adopted"]
            reaped = json.loads((tmp_path / "reaped-hook.json").read_text())
            assert reaped["pid"] == hook["pid"] and reaped["token"] == hook["token"]
            assert reaped["reaper"] == os.getpid() and reaped["signal"] == signal.SIGKILL
            assert _process_start_token(hook["pid"]) != hook["token"]
            assert parent.wait(timeout=3) == 0
        finally:
            # Release the barrier even when the unfixed reaper fails before waitid.
            try:
                if not released:
                    os.write(release_write, b"1")
            finally:
                os.close(release_write)
                os.close(release_read)
            if parent is not None:
                try:
                    parent.wait(timeout=3)
                finally:
                    if parent.poll() is None:
                        os.killpg(parent.pid, signal.SIGKILL)
                        parent.wait(timeout=3)
                    _reap_created(tmp_path, "hook.json")


if __name__ == "__main__":
    _exercise(Path(sys.argv[1]))
