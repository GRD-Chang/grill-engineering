"""真实 CLI 在已持久化业务边界崩溃；不依赖无关保存次数。"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal

from support.cli import PROJECT_ROOT, run_cli


DevelopmentBoundary = Literal["before_invocation", "running", "completed"]
ResumeBoundary = Literal["resume_authorized", "session_interrupted"]


def run_cli_at_development_boundary(
    repo: Path,
    fixture: Path,
    *arguments: str,
    boundary: DevelopmentBoundary,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return _run_cli_at_boundary(repo, fixture, *arguments, boundary=boundary, extra_env=extra_env)


def run_cli_at_resume_boundary(
    repo: Path,
    fixture: Path,
    *arguments: str,
    boundary: ResumeBoundary,
) -> subprocess.CompletedProcess[str]:
    return _run_cli_at_boundary(repo, fixture, *arguments, boundary=boundary)


def _run_cli_at_boundary(
    repo: Path,
    fixture: Path,
    *arguments: str,
    boundary: DevelopmentBoundary | ResumeBoundary,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    # 限定到本次子进程，退出时删除注入模块；后续 run/resume 使用原入口。
    with TemporaryDirectory(prefix="cli-crash-", dir=repo.parent) as directory:
        Path(directory, "sitecustomize.py").write_text(
            "from agent_run.state import FaultInjectingStateStore, SimulatedProcessCrash\n"
            f"boundary = {boundary!r}\n"
            "def after_saved(self):\n"
            "    if self.injected:\n"
            "        return\n"
            "    for run_id in self.list_run_ids():\n"
            "        state = self.load_run(run_id)\n"
            "        invocation = state.get('active_agent_invocation') or {}\n"
            "        if boundary == 'before_invocation':\n"
            "            reached = (isinstance(state.get('action_application_receipt'), dict)\n"
            "                       and not state.get('agent_invocation_history'))\n"
            "        elif boundary == 'resume_authorized':\n"
            "            receipt = state.get('action_application_receipt') or {}\n"
            "            audit = state.get('resume_audit', {}).get('history') or []\n"
            "            attempt = invocation.get('semantic_attempt') or {}\n"
            "            reached = (receipt.get('kind') == 'resume' and bool(audit)\n"
            "                       and audit[-1].get('semantic_attempt_id') == attempt.get('attempt_id')\n"
            "                       and not audit[-1].get('successor_invocation_started_at')\n"
            "                       and invocation.get('status') == 'resuming')\n"
            "        elif boundary == 'session_interrupted':\n"
            "            reached = (state.get('status') == 'execution_failed'\n"
            "                       and invocation.get('status') == 'failed'\n"
            "                       and invocation.get('error') == 'session_interrupted')\n"
            "        else:\n"
            "            reached = (invocation.get('work_subject') == 'ticket:3'\n"
            "                       and invocation.get('role') == 'development'\n"
            "                       and invocation.get('status') == boundary)\n"
            "        if reached:\n"
            "            self.injected = True\n"
            "            raise SimulatedProcessCrash('durable work boundary: ' + boundary)\n"
            "FaultInjectingStateStore._after_run_saved = after_saved\n",
            encoding="utf-8",
        )
        environment = dict(extra_env or {})
        python_path = environment.get("PYTHONPATH", os.environ.get("PYTHONPATH", ""))
        environment["PYTHONPATH"] = os.pathsep.join(
            item for item in (directory, str(PROJECT_ROOT / "src"), python_path) if item
        )
        return run_cli(
            repo, fixture, *arguments, "--crash-after-save", "1", extra_env=environment
        )
