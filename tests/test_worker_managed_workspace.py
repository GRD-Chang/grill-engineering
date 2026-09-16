from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from agent_run.codex import _worker_hidden_paths
from agent_run.worker_sandbox import bubblewrap_command, worker_environment


@pytest.mark.parametrize("writable_checkout", [True, False])
def test_worker_can_use_managed_checkout_without_exposing_runner_data(
    git_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    writable_checkout: bool,
) -> None:
    if shutil.which("bwrap") is None:
        pytest.skip("bubblewrap is unavailable")
    data_home = tmp_path / "data"
    runner_root = data_home / "agent-run"
    repository_root = runner_root / "repositories" / "example" / "project"
    clone = repository_root / "repository"
    shutil.copytree(git_repo, clone)
    state_root = repository_root / "state"
    checkout = state_root / "worktrees" / "run" / "ticket"
    subprocess.run(
        ["git", "worktree", "add", "--detach", str(checkout), "HEAD"],
        cwd=clone,
        capture_output=True,
        check=True,
    )
    protected = (
        clone / "base-checkout-secret",
        runner_root / "snapshots" / "installed" / "runner-secret",
        runner_root / "run-locator.json",
        state_root / "runs" / "run.json",
        state_root / "task-control" / "authority.json",
        state_root / "worktrees" / "other-run" / "private-source.py",
        checkout / ".agent-run" / "runs" / "nested-secret.json",
    )
    for path in protected:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("private", encoding="utf-8")
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    monkeypatch.setenv("AGENT_RUN_INTERNAL_STATE_ROOT", str(state_root))
    temporary = tmp_path / "worker-temp"
    temporary.mkdir()
    environment = worker_environment(temporary / "gh", "read-token")
    environment["PROTECTED_PATHS"] = json.dumps([str(path) for path in protected])
    probe = temporary / "probe.py"
    probe.write_text(
        "import json, os, subprocess\n"
        "from pathlib import Path\n"
        "source = Path('README.md')\n"
        "before = source.read_text()\n"
        "read = subprocess.run(['git', 'show', 'HEAD:README.md'], "
        "text=True, capture_output=True)\n"
        "mutation = subprocess.run(['git', 'update-ref', "
        "'refs/heads/worker-escape', 'HEAD'], capture_output=True)\n"
        "try:\n"
        "    source.write_text('worker change\\n')\n"
        "    writable = True\n"
        "except OSError:\n"
        "    writable = False\n"
        "print(json.dumps({\n"
        "    'source': before, 'git_read': read.stdout,\n"
        "    'git_returncode': read.returncode, 'writable': writable,\n"
        "    'git_mutation_blocked': mutation.returncode != 0,\n"
        "    'protected_visible': [Path(p).exists() "
        "for p in json.loads(os.environ['PROTECTED_PATHS'])],\n"
        "}))\n",
        encoding="utf-8",
    )
    command = bubblewrap_command(
        [
            sys.executable,
            "-c",
            "import subprocess, sys; "
            "subprocess.run([sys.executable, sys.argv[1]], check=True)",
            str(probe),
        ],
        checkout=checkout,
        temporary=temporary,
        writable_checkout=writable_checkout,
        environment=environment,
        hidden_paths=_worker_hidden_paths(None, checkout=checkout),
    )

    result = subprocess.run(
        command, cwd=checkout, env=environment, text=True, capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "source": "# fixture\n",
        "git_read": "# fixture\n",
        "git_returncode": 0,
        "writable": writable_checkout,
        "git_mutation_blocked": True,
        "protected_visible": [False] * len(protected),
    }
    assert (checkout / "README.md").read_text(encoding="utf-8") == (
        "worker change\n" if writable_checkout else "# fixture\n"
    )
    assert all(path.read_text(encoding="utf-8") == "private" for path in protected)
