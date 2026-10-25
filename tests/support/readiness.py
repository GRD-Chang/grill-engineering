"""Local command doubles shared by public readiness tests."""
from pathlib import Path

import pytest


def fake_readiness_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    tools = tmp_path / "tools"
    tools.mkdir()
    calls = tmp_path / "calls"
    programs = {
        "gh": "case \"$1\" in --version) echo 'gh version 2.65.0';; api) echo '--paginate --slurp --method --header';; *) exit 1;; esac",
        "git": "echo 'git version 2.40.0'",
        "codex": "echo '--json --output-last-message --output-schema --dangerously-bypass-approvals-and-sandbox --model --config --cd --color'",
        "systemctl": "echo 257", "systemd-run": "exit 0", "bwrap": "exit 0", "openssl": "exit 0",
    }
    for name, body in programs.items():
        path = tools / name
        path.write_text(f"#!/bin/sh\necho '{name}' \"$@\" >> '{calls}'\n{body}\n")
        path.chmod(0o700)
    monkeypatch.setenv("PATH", str(tools))
    return calls
