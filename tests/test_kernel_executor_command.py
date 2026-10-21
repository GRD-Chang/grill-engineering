"""The actual service command preserves its selected Python and loader input."""
from __future__ import annotations

from pathlib import Path
import sys

import pytest

from agent_run_kernel import executor


def test_host_command_seals_library_path_without_resolving_venv_or_leaking_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected = tmp_path / "venv" / "bin" / "python"
    selected.parent.mkdir(parents=True)
    selected.symlink_to(sys.executable)
    monkeypatch.setattr(sys, "executable", str(selected))
    database, lock = tmp_path / "native.sqlite", tmp_path / "install.lock"
    environment = {"LD_LIBRARY_PATH": "/private/sqlite/lib:/another path/lib", "GH_TOKEN": "secret-sentinel"}
    command = executor.command(database, "original-action", lock, environment)
    assert command == (
        "/usr/bin/env", "LD_LIBRARY_PATH=/private/sqlite/lib:/another path/lib", str(selected),
        "-m", "agent_run_kernel.executor", "--database", str(database),
        "--action", "original-action", "--runner-lock", str(lock),
    )
    assert "secret-sentinel" not in " ".join(command)
    assert executor.command(database, "original-action", lock, {})[0] == str(selected)
    assert not database.exists() and not lock.exists()


def test_unmanaged_executor_entry_refuses_before_accessing_a_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    database, lock = tmp_path / "never-created.sqlite", tmp_path / "never-created.lock"
    assert executor.main(["--database", str(database), "--action", "unadmitted", "--runner-lock", str(lock)]) == 1
    assert "managed_host_required" in capsys.readouterr().err
    assert not database.exists() and not lock.exists()
