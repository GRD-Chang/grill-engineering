"""Public read-only consumers; fixtures use the real canonical Kernel store."""
from __future__ import annotations

from pathlib import Path
import json

from agent_run_kernel.query_cli import main


def test_missing_exact_query_is_actionable_and_does_not_create_storage(
    tmp_path: Path, capsys,
) -> None:
    missing = tmp_path / "absent-state"
    assert main(["status", "unknown-run", "--state-dir", str(missing), "--json"]) == 2
    result = json.loads(capsys.readouterr().out)
    assert result["result"] == "error"
    assert result["diagnostics"][0]["code"] == "run_locator_missing"
    assert not missing.exists()
