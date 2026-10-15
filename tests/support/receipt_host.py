"""共享测试支撑：receipt_host；每次调用保持独立状态。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import nullcontext
from pathlib import Path
from typing import Any
import pytest
import agent_run.cli as cli_module
import agent_run.workspace_cli as workspace_cli
from agent_run.executor_host import ExecutorSpec, ExecutorStartUnknownError, HostObservation
from agent_run.github_fixture import FixtureGitHubPublisher, FixtureGitHubReader
from agent_run.task_control import TaskControlStore


def _install_host(
    git_repo: Path,
    fixture: Path,
    monkeypatch: pytest.MonkeyPatch,
    host_status: str,
    *,
    launch_unknown: bool = True,
) -> cli_module.FakeExecutorHost:
    class UsageLease:
        def fileno(self) -> int:
            return 1

    class ReceiptHost(cli_module.FakeExecutorHost):
        def __init__(self) -> None:
            super().__init__(start_outcome="unknown" if launch_unknown else "accepted")
            self.observed: list[ExecutorSpec] = []

        def check_readiness(self) -> None:
            pass

        def prepare_environment(self, _arguments: object) -> None:
            pass

        def observe(
            self, spec: ExecutorSpec, _control: TaskControlStore
        ) -> HostObservation:
            self.observed.append(spec)
            return HostObservation(
                host_status,  # type: ignore[arg-type]
                spec.generation,
                None,
                False,
                runner_binding="0" * 16 if host_status == "exited" else None,
            )

        def ensure(
            self,
            spec: ExecutorSpec,
            control: TaskControlStore,
            execute: Callable[[], Mapping[str, Any]] | None = None,
            recover: bool = False,
        ) -> HostObservation:
            # The matrix loses the successor launch response after admission.
            # The smoke tests run the real Executor callback to completion.
            result = super().ensure(spec, control, execute=execute, recover=recover)
            if launch_unknown:
                raise ExecutorStartUnknownError("successor launch response lost")
            return result

    host = ReceiptHost()
    monkeypatch.chdir(git_repo)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(git_repo.parent / "runtime"))
    monkeypatch.setattr(
        cli_module, "runner_usage_lease", lambda _path: nullcontext(UsageLease())
    )
    monkeypatch.setattr(cli_module, "_running_active_runner", lambda: True)
    monkeypatch.setattr(cli_module, "SystemdUserExecutorHost", lambda **_options: host)
    monkeypatch.setattr(
        cli_module,
        "GhGitHubReader",
        lambda _repo, *, working_directory: FixtureGitHubReader(fixture),
    )
    monkeypatch.setattr(
        workspace_cli, "GhGitHubReader",
        lambda **_options: FixtureGitHubReader(fixture),
    )
    monkeypatch.setattr(
        cli_module,
        "GhGitHubPublisher",
        lambda _repo, git: FixtureGitHubPublisher(fixture, git),
    )
    return host
