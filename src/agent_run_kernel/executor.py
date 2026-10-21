"""Concrete native Host entry and delivery I/O supervision.

Kernel owns every transition, seat, queue and exit decision. This process owns
the outstanding local calls, not a second persisted workflow or Run snapshot.
"""
from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor, wait
import json
import os
from pathlib import Path
import sys
import threading
import time
from typing import Any

from agent_run.error_safety import bounded_error

from .host_adapter import SystemdHost
from .execution import ProcessExitUnknown
from .kernel import Kernel
from .model import AgentCall, Execution, KernelError, Step, World
from .native_host import managed_host_environment
from .remote_effects import RemoteEffects
from .role_prompt import render_role_prompt


def command(database: Path, action: str, runner_lock: Path,
            environment: Mapping[str, str]) -> tuple[str, ...]:
    """Seal the selected interpreter and its SQLite loader path before launch.

    Preserve the venv executable spelling: resolving its symlink would select
    another environment. Only the non-secret dynamic-library path enters argv;
    the complete terminal environment still uses the private one-shot carrier.
    """
    if (not database.is_absolute() or not runner_lock.is_absolute()
            or not action or len(action) > 256 or "\0" in action
            or not Path(sys.executable).is_absolute()):
        raise ValueError("An exact native Host command is required")
    arguments = (sys.executable, "-m", "agent_run_kernel.executor", "--database", str(database),
                 "--action", action, "--runner-lock", str(runner_lock))
    library = environment.get("LD_LIBRARY_PATH", "")
    if "\0" in library:
        raise ValueError("Invalid captured library path")
    return ("/usr/bin/env", "LD_LIBRARY_PATH=" + library, *arguments) if library else arguments


class _Executor:
    def __init__(self, kernel: Kernel, execution: Execution, environment: Mapping[str, str]) -> None:
        self.kernel, self.execution = kernel, execution
        self.environment = dict(environment)
        with kernel.storage.transaction(write=False) as tx:
            row = tx.one("SELECT repository,repository_root FROM runs WHERE id=?", (execution.run_id,))
            binding = tx.one("SELECT subject,material_id FROM run_inputs WHERE run_id=?", (execution.run_id,))
            inputs = json.loads(tx.get(binding["material_id"], binding["subject"]))
        if not isinstance(row["repository_root"], str):
            raise KernelError("managed_repository_required")
        root = Path(row["repository_root"])
        if not root.is_absolute() or root.resolve() != root:
            raise KernelError("managed_repository_required")
        remote = inputs.get("repository_remote")
        if not isinstance(remote, str) or not remote:
            raise KernelError("creation_repository_remote_required")
        self.remote = RemoteEffects(root, row["repository"], runner_identity=inputs["runner_identity"], remote=remote)
        self.worker_identity = inputs["worker_identity"]
        self.source_lock = threading.Lock()
        self.hidden_paths = (kernel.storage.path.parent,)
        self.hidden_environment: tuple[str, ...] = ()
        protected = list(self.hidden_paths)
        variables: list[str] = []
        for identity in (inputs["runner_identity"], self.worker_identity):
            reference = identity.get("pat", {})
            if reference.get("file"):
                protected.append(Path(reference["file"]))
            if reference.get("env"):
                variables.append(reference["env"])
            app = identity.get("app_profile", {})
            if app.get("private_key_path"):
                protected.append(Path(app["private_key_path"]))
        self.hidden_paths, self.hidden_environment = tuple(protected), tuple(variables)

    def observe(self, subject: str) -> World:
        # One source reader for this Host's own calls. Cross-process version
        # checks remain in Kernel; this lock does not grant mutation authority.
        with self.source_lock:
            return self.remote.observe(self.kernel, subject)

    def agent(self, call: AgentCall) -> None:
        try:
            prompt = render_role_prompt(call)
        except Exception as error:
            # Match the process adapter's dispatch-before-preparation contract:
            # this exact allocated call is known not to have launched anything.
            if self.kernel.dispatch_agent(call):
                self.kernel.agent_unavailable(call, "worker_preparation_failed", {"error_type": type(error).__name__})
            return
        from .codex_execution import execute_selected_codex

        def authorize_source() -> None:
            self.observe(call.subject)

        execute_selected_codex(
            self.kernel, call, identity=self.worker_identity, environment=self.environment,
            prompt=prompt, hidden_paths=self.hidden_paths,
            hidden_environment=self.hidden_environment,
            authorize_source=authorize_source,
        )

    def effect(self, step: Step) -> Step:
        from .local_effects import LocalEffects

        effect = step.effect
        if effect is None:
            raise KernelError("effect_request_required")
        port = (self.remote if effect.operation in Kernel.REMOTE else
                LocalEffects(self.remote.git, owned_root=Path(effect.payload["owned_root"])))
        if step.kind == "reconcile":
            # Original physical outcome is durable before any new source I/O.
            result = port.reconcile(effect)
            settled = self.kernel.reconcile(effect, result, self.execution)
            if settled.kind != "observe":
                return settled
            return self.kernel.reconcile(effect, result, self.execution, self.observe(effect.subject))
        if (effect.operation == "publication_commit" and not effect.payload.get("reuse_sha")
                and not effect.payload.get("publication_commit_intent")):
            if not isinstance(port, LocalEffects):
                raise KernelError("local_publication_required")
            effect = self.kernel.attach_publication_commit(
                effect, port.prepare_publication(effect), self.execution, self.observe(effect.subject),
            )
        dispatched = False

        def authorize() -> None:
            nonlocal dispatched
            world = self.observe(effect.subject)
            if not dispatched:
                if not self.kernel.dispatch(effect, self.execution, world):
                    raise KernelError("operation_cancelled")
                dispatched = True
            self.kernel.guard_effect(effect, self.execution, world)

        try:
            result = port.execute(effect, authorize)
        except Exception as error:
            if dispatched:
                self.kernel.observe_effect_failure(effect, {"type": type(error).__name__,
                                                          "message": bounded_error(str(error))})
            raise
        if dispatched and result.raw.get("write_returncode") not in (None, 0):
            self.kernel.observe_effect_failure(effect, {
                "returncode": result.raw["write_returncode"],
                "message": bounded_error(str(result.raw.get("write_diagnostic", ""))),
            })
        if effect.operation == "issue_link":
            self.kernel.acknowledge_link(effect, result)
            return Step("progress", effect.subject)
        settled = self.kernel.reconcile(effect, result, self.execution)
        if settled.kind == "observe":
            settled = self.kernel.reconcile(effect, result, self.execution, self.observe(effect.subject))
        return settled

    def advance(self, subject: str, *, cleanup: bool = False) -> Step:
        advance = self.kernel.advance_cleanup if cleanup else self.kernel.advance
        step = advance(subject, self.execution)
        if step.kind == "observe":
            step = advance(subject, self.execution, self.observe(subject))
        if step.kind in {"effect", "reconcile"}:
            return self.effect(step)
        return step

    def subjects(self, scheduling: Mapping[str, Any]) -> list[str]:
        with self.kernel.storage.transaction(write=False) as tx:
            ids = [row[0] for row in tx.all("SELECT id FROM subjects ORDER BY rowid")]
        return list(dict.fromkeys([*scheduling["work_fifo"], *scheduling["integration_fifo"], *ids]))

    def activate_final(self) -> bool:
        with self.kernel.storage.transaction(write=False) as tx:
            if not tx.rows("SELECT id FROM final_target") or tx.rows("SELECT id FROM subjects WHERE kind='final_run'"):
                return False
        with self.source_lock:
            head, default, proof = self.remote.final_observation(self.kernel)
        try:
            self.kernel.activate_final(self.execution, head, default, ancestry=proof)
        except KernelError as error:
            if error.code in {"ticket_completion_required", "final_admission_closed"}:
                return False
            raise
        return True


def run(kernel: Kernel, execution: Execution, environment: Mapping[str, str]) -> int:
    """Supervise allocated calls while other subjects' program work advances."""
    driver = _Executor(kernel, execution, environment)
    scheduling = kernel.scheduling()
    workers: dict[str, Future[None]] = {}
    with ThreadPoolExecutor(max_workers=scheduling["concurrency"], thread_name_prefix="agent-run") as pool:
        while True:
            for subject, future in list(workers.items()):
                if future.done():
                    # Process transport has already durably settled raw/exit or
                    # exact start-unknown facts. It never authorizes a retry.
                    try:
                        future.result()
                    except ProcessExitUnknown:
                        # Only the actual managed Host/cgroup exit can settle
                        # this remaining physical uncertainty.
                        raise
                    except Exception as error:
                        # The one process adapter has recorded unavailable,
                        # raw/exit or fatal facts. Kernel decides recovery from
                        # those facts; an exception is not a second policy.
                        print(bounded_error(str(error)), file=sys.stderr)
                    del workers[subject]
            progress = False
            for subject in driver.subjects(scheduling):
                if subject in workers:
                    continue
                step = driver.advance(subject)
                if step.kind == "agent" and step.call is not None:
                    workers[subject] = pool.submit(driver.agent, step.call)
                    progress = True
                elif step.kind == "complete":
                    retired = driver.advance(subject, cleanup=True)
                    progress = progress or retired.kind == "progress"
                else:
                    progress = progress or step.kind == "progress"
            scheduling = kernel.scheduling()
            if not workers and not progress and scheduling["disposition"] in {"quiescent", "complete"}:
                return 0
            if not progress and not workers:
                progress = driver.activate_final()
            if not progress:
                deadline = scheduling["next_deadline_at"]
                delay = 1.0 if deadline is None else min(1.0, max(0.01, deadline - kernel.clock()))
                if workers:
                    wait(workers.values(), timeout=delay)
                else:
                    time.sleep(delay)


def main(arguments: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Exact admitted native Host entry")
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--action", required=True)
    parser.add_argument("--runner-lock", type=Path, required=True)
    parsed = parser.parse_args(arguments)
    try:
        credential_directory, notify_socket = os.environ.get("CREDENTIALS_DIRECTORY"), os.environ.get("NOTIFY_SOCKET")
        if not credential_directory or not notify_socket:
            raise KernelError("managed_host_required")
        kernel = Kernel(parsed.database)
        request = kernel.host_request(parsed.action)
        manager = SystemdHost(systemctl="systemctl", systemd_run="systemd-run", environment=os.environ)
        with managed_host_environment(
            kernel, parsed.action, manager, runner_lock=parsed.runner_lock,
            credential=Path(credential_directory) / "agent-run-environment", notify_socket=notify_socket,
        ) as inherited:
            execution = Execution(request.run_id, request.owner, request.epoch)
            return run(kernel, execution, inherited.environment)
    except (OSError, ValueError, RuntimeError) as error:
        print(bounded_error(str(error)), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
