"""Native process fixtures and actual SQLite observation in isolated Snapshots."""
from __future__ import annotations

import json
import site
import sqlite3
import sys
import venv
from pathlib import Path
from typing import Any
import os
from agent_run.core.kernel import Kernel
from agent_run.model import AgentPreparation
from agent_run.runtime.codex import execute_codex
from agent_run.runtime.execution import ProcessExit


def current_runtime() -> dict[str, Any]:
    libraries = sorted({line.split()[-1] for line in Path('/proc/self/maps').read_text().splitlines()
                        if '/libsqlite3.so' in line})
    return {"version": sqlite3.sqlite_version, "libraries": libraries}


def snapshot_python(root: Path, report: Path) -> Path:
    """A private importable Runner with an observer at real SQLite connects."""
    venv.EnvBuilder(with_pip=False, symlinks=True).create(root)
    packages = root / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"
    source = Path(__file__).resolve().parents[2] / "src"
    (packages / "source.pth").write_text("\n".join((str(source), *site.getsitepackages(),
                                                   "import agent_run_test_runtime_probe", "")))
    observer = f"REPORT = {str(report)!r}\n" + '''import json, sys
from pathlib import Path
recorded = False
def observe(event, arguments):
    global recorded
    if event != 'sqlite3.connect' or recorded:
        return
    recorded = True
    module = sys.modules['sqlite3']
    libraries = sorted({line.split()[-1] for line in Path('/proc/self/maps').read_text().splitlines()
                        if '/libsqlite3.so' in line})
    with Path(REPORT).open('a') as stream:
        stream.write(json.dumps({'version': module.sqlite_version, 'libraries': libraries}) + '\\n')
sys.addaudithook(observe)
'''
    (packages / "agent_run_test_runtime_probe.py").write_text(observer)
    return root / "bin" / "python"


def observed_runtimes(report: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in report.read_text().splitlines()]


def local_codex(kernel: Kernel, call: AgentPreparation, tmp_path: Path, program: str) -> ProcessExit | None:
    executable = tmp_path / "local-codex"
    executable.write_text(f"#!{sys.executable}\n" + "import json, os, sys\nfrom pathlib import Path\n" + program)
    executable.chmod(0o700)
    credential = tmp_path / "selected-hosts.yml"
    credential.write_text("github.com:\n  oauth_token: local-fixture-only\n")
    credential.chmod(0o600)
    return execute_codex(
        kernel, call, executable=str(executable), prompt="Continue the allocated task.",
        environment={"PATH": os.defpath, "HOME": str(tmp_path)}, credential_file=credential,
        hidden_paths=(), authorize_source=lambda admitted: None, timeout=5,
    )
