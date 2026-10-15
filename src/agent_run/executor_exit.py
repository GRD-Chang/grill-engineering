"""Finite systemd post-exit observation; no execution or business-state authority."""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent_run.executor_environment import DEFAULT_MAX_ENVIRONMENT_BYTES, _binding
from agent_run.executor_host import ExecutorSpec
from agent_run.notification_event_facts import event_copy, event_envelope
from agent_run.notification_outbox import transaction
from agent_run.operator_gate import has_run_operator_gate
from agent_run.runner_lease import default_runner_lock_path, runner_usage_lease
from agent_run.state import StateStore
from agent_run.task_control import TaskControlStore, TaskKey

CREDENTIAL = "agent-run-exit-environment"
STOP_TIMEOUT_SECONDS = 70


def exit_properties(spec: ExecutorSpec, control: TaskControlStore, python: Path,
                    carrier: Path) -> tuple[str, ...]:
    """Arm only an enabled Run (or the admitted creation payload), never defaults."""
    record = control.load(spec.task) or {}
    state = StateStore(control.root).load_current_run(spec.run_id) if spec.run_id else None
    config = (state if state is not None else (record.get("action") or {}).get("payload", {})).get("notifications", {})
    if not isinstance(config, dict) or config.get("enabled") is not True:
        return ()
    command = [str(python), "-I", "-m", "agent_run.executor_exit",
               "--workspace", str(spec.task.workspace), "--repository", spec.task.repository,
               "--parent", str(spec.task.parent_number), "--action-id", spec.action_id,
               "--generation", str(spec.generation), "--state-root", str(spec.state_root),
               "--runner-binding", str(spec.runner_binding)]
    if spec.run_id:
        command.extend(("--run-id", spec.run_id))
    # systemd command syntax, not shell syntax. ':' disables $ expansion;
    # percent specifiers still need escaping. '-' keeps notification failures
    # from changing the service's execution result.
    rendered = " ".join(json.dumps(arg.replace("%", "%%"), ensure_ascii=False) for arg in command)
    return (f"ExecStopPost=-:{rendered}",
            f"LoadCredential={CREDENTIAL}:{str(carrier).replace('%', '%%')}",
            f"TimeoutStopSec={STOP_TIMEOUT_SECONDS}s", "TimeoutStopFailureMode=kill",
            "OOMPolicy=continue", "FileDescriptorStoreMax=1", "NotifyAccess=main")


def _current(root: Path, state: dict[str, Any], evidence: dict[str, Any]) -> bool:
    task = TaskKey(Path(evidence["workspace"]), evidence["repository"], evidence["parent"])
    record = TaskControlStore(root).load(task) or {}
    executor = record.get("executor") or {}
    action = record.get("action") or {}
    return (
        state.get("run_id") == evidence["run_id"] == record.get("run_id")
        and state.get("repository") == task.repository
        and (state.get("parent") or {}).get("number") == task.parent_number
        and all(executor.get(key) == evidence[key] for key in
                ("action_id", "generation", "run_id", "runner_binding", "pid", "process_start_token"))
        and action.get("action_id") == evidence["action_id"]
        and action.get("run_id") == evidence["run_id"]
        and action.get("executor_generation") == evidence["generation"]
        and action.get("kind") not in {"stop", "abandon"}
        and state.get("status") not in {"completed", "abandoned", "operator_stopped"}
        # A gracefully recorded failure/wait uses the existing business events.
        and not (executor.get("status") == "exited"
                 and (not executor.get("failure") or has_run_operator_gate(state)))
    )


def exit_event(root: Path, state: dict[str, Any], document: dict[str, Any]) -> list[dict[str, Any]]:
    evidence = document.get("executor_exit")
    if not isinstance(evidence, dict) or not _current(root, state, evidence):
        return []
    return [event_envelope(
        state, "executor_exit", [evidence["action_id"], evidence["generation"], evidence["runner_binding"]],
        event_copy(state, "executor_exit"), "red", important=True,
        generation=evidence["generation"], exit_evidence=evidence,
        summary=event_copy(state, "executor_exit_summary"),
        next_step=event_copy(state, "execution_next"),
    )]


def observe_exit(spec: ExecutorSpec, *, service_result: str, exit_code: str,
                 exit_status: str) -> None:
    """Consume native exit facts, then use the sole durable notification sender."""
    if not (exit_code in {"killed", "dumped"} or
            (exit_code == "exited" and exit_status.isdigit() and int(exit_status) != 0)):
        return
    if service_result not in {"signal", "core-dump", "oom-kill", "exit-code", "timeout", "success"}:
        return
    assert spec.state_root is not None
    control = TaskControlStore(spec.state_root)
    record = control.load(spec.task) or {}
    executor = record.get("executor") or {}
    run_id = spec.run_id or executor.get("run_id")
    if not isinstance(run_id, str):
        return
    states = StateStore(spec.state_root)
    state = states.load_current_run(run_id)
    if state is None or (state.get("notifications") or {}).get("enabled") is not True:
        return
    evidence = {
        "workspace": str(spec.task.workspace), "repository": spec.task.repository,
        "parent": spec.task.parent_number, "run_id": run_id,
        "action_id": spec.action_id, "generation": spec.generation,
        "runner_binding": spec.runner_binding, "service_result": service_result,
        "pid": executor.get("pid"), "process_start_token": executor.get("process_start_token"),
        "exit_code": exit_code, "exit_status": exit_status[:32],
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }
    if not _current(spec.state_root, state, evidence):
        return
    with transaction(spec.state_root, run_id) as document:
        prior = document.get("executor_exit") or {}
        # Preserve first observation and the original event budget on repeats.
        if prior.get("generation", 0) < spec.generation:
            document["executor_exit"] = evidence
    from agent_run.notifications import Notifications

    Notifications(spec.state_root, state, load_state=lambda: states.load_current_run(run_id)).close()


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent-run-executor-exit")
    for key in ("workspace", "repository", "action-id", "state-root", "runner-binding"):
        parser.add_argument("--" + key, required=True)
    parser.add_argument("--parent", type=int, required=True)
    parser.add_argument("--generation", type=int, required=True)
    parser.add_argument("--run-id")
    args = parser.parse_args(arguments)
    spec = ExecutorSpec(TaskKey(Path(args.workspace), args.repository, args.parent),
                        args.action_id, args.run_id, args.generation,
                        state_root=Path(args.state_root), runner_binding=args.runner_binding)
    result, code, status = (os.environ.get(key, "") for key in ("SERVICE_RESULT", "EXIT_CODE", "EXIT_STATUS"))
    try:
        # systemd copies this at activation, before the one-shot startup carrier
        # is consumed. Its private, read-only copy lives only until unit cleanup.
        credential = Path(os.environ["CREDENTIALS_DIRECTORY"]) / CREDENTIAL
        with credential.open("rb") as stream:
            raw = stream.read(DEFAULT_MAX_ENVIRONMENT_BYTES + 1)
        if len(raw) > DEFAULT_MAX_ENVIRONMENT_BYTES:
            raise ValueError("exit environment exceeds limit")
        snapshot = json.loads(raw)
        if snapshot.get("binding") != _binding(spec):
            raise ValueError("exit environment binding mismatch")
        environment = snapshot["environment"]
        if not isinstance(environment, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in environment.items()):
            raise ValueError("invalid exit environment")
        os.environ.clear()
        os.environ.update(environment)
        with runner_usage_lease(default_runner_lock_path(environment)):
            observe_exit(spec, service_result=result, exit_code=code, exit_status=status)
    except Exception:
        # No credentials or arbitrary native/CLI output in the host journal.
        import sys
        print("Executor exit notification observation unavailable", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
