"""共享测试支撑：resume_intent；每次调用保持独立状态。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from support.seeding import write_fixture
from support.cli import load_only_run_state, run_cli
from support.cli_payloads import parent_round_agents


def _checkpoint(repo: Path) -> tuple[Path, Path, dict[str, Any]]:
    fixture = write_fixture(repo / "github.json", issues={})
    agents = repo / "agents.json"
    agents.write_text(json.dumps(parent_round_agents(1, passing_last=False)))
    result = run_cli(
        repo, fixture, "run", "1", "--parent-only-paired-rounds", "1",
        "--agent-fixture", str(agents),
    )
    assert result.returncode == 2, result.stdout
    return fixture, agents, load_only_run_state(repo)
