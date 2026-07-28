from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agent_run.state import StateStore


def test_interrupted_replace_preserves_previous_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = StateStore(tmp_path)
    store.save_run("run-1", {"attempt": 1})
    original_replace = os.replace

    def fail_run_replace(source: str | Path, destination: str | Path) -> None:
        if Path(destination).name == "run-1.json":
            raise OSError("simulated interruption")
        original_replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_run_replace)

    with pytest.raises(OSError, match="simulated interruption"):
        store.save_run("run-1", {"attempt": 2})

    persisted = json.loads(
        (tmp_path / "runs" / "run-1.json").read_text(encoding="utf-8")
    )
    assert persisted == {"attempt": 1}
    assert not list((tmp_path / "runs").glob("*.tmp"))
