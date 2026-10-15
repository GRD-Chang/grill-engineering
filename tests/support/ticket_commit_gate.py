"""通过真实 Git hook 控制提交收口，不占用 Agent 租约。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import subprocess
import sys
import time
from pathlib import Path

from agent_run.state import StateStore
from support.cli import run_cli
from support.workspace import managed_repo, managed_state


def run_with_second_ticket_review_first(
    repo: Path, fixture: Path, run_id: str, agent_fixture: Path,
) -> subprocess.CompletedProcess[str]:
    """Hold Ticket 2's commit until Ticket 3's Human Blocker is persisted."""
    gate = repo / "commit-gate"
    gate.mkdir()
    release = gate / "release"
    hooks = managed_repo(repo) / ".git" / "hooks"
    hook = hooks / "pre-commit"
    assert not hook.exists(), "test must not overwrite an existing hook"
    hook.write_text(
        f"#!{sys.executable}\n"
        "from pathlib import Path\nimport time\n"
        "if Path.cwd().name == 'ticket-2':\n"
        f"    release = Path({str(release)!r})\n"
        f"    Path({str(gate / 'started')!r}).touch()\n"
        "    deadline = time.monotonic() + 15\n"
        "    while not release.exists():\n"
        "        if time.monotonic() >= deadline:\n"
        "            raise RuntimeError('commit gate was not released')\n"
        "        time.sleep(0.02)\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)
    states = StateStore(managed_state(repo))
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            run_cli, repo, fixture, "run", "1", "--agent-fixture", str(agent_fixture),
        )
        try:
            deadline = time.monotonic() + 15
            while True:
                state = states.load_current_run(run_id)
                sibling = state["ticket_jobs"].get("3", {}) if state is not None else {}
                if sibling.get("phase") == "blocked":
                    assert (gate / "started").exists()
                    assert state["ticket_jobs"]["2"]["phase"] != "blocked"
                    review = sibling["review_budget"]["review_artifacts"][-1]
                    assert review["reviewer_thread_id"] == "reviewer-2"
                    break
                assert not future.done(), future.result() if future.done() else None
                assert time.monotonic() < deadline, "Ticket 3 did not reach its Review boundary"
                time.sleep(0.02)
        finally:
            release.touch()
            try:
                result = future.result(timeout=20)
            finally:
                hook.unlink()
    return result
