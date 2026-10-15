"""Observe the actual SQLite loaded by isolated Snapshot processes."""
from __future__ import annotations

import json
import site
import sqlite3
import sys
import venv
from pathlib import Path
from typing import Any


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
