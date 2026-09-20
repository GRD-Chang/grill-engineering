"""Raw source entrypoints must work without an installed package or dev paths."""
from __future__ import annotations

import os
from pathlib import Path
import shlex
import shutil
import subprocess
import venv


def test_raw_source_management_and_probes_without_installed_package(tmp_path: Path) -> None:
    project = Path(__file__).resolve().parents[1]
    source = tmp_path / "源码 archive with spaces"
    source.mkdir()
    for name in ("install.sh", "setup.sh"):
        shutil.copy2(project / name, source / name)
    shutil.copytree(project / "src/agent_run", source / "src/agent_run",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    interpreter = tmp_path / "python"
    venv.EnvBuilder(with_pip=False).create(interpreter)
    python = interpreter / "bin/python"
    tools = tmp_path / "tools"
    tools.mkdir()
    launcher = tools / "python3"
    launcher.write_text(f'#!/bin/sh\nexec {shlex.quote(str(python))} -I "$@"\n')
    launcher.chmod(0o755)
    environment = {key: value for key, value in os.environ.items()
                   if key not in ("PYTHONPATH", "PYTHONHOME")}
    environment["PATH"] = str(tools) + os.pathsep + os.defpath
    absent = subprocess.run([str(python), "-I", "-c", "import agent_run"],
                            env=environment, capture_output=True, text=True, timeout=10)
    assert absent.returncode != 0
    commands = [
        (["sh", str(source / "install.sh"), "--help"], 0, "--rollback"),
        (["sh", str(source / "install.sh"), "--rollback"], 1, "agent-run install:"),
        (["sh", str(source / "install.sh"), "--uninstall"], 0, "uninstalled"),
        (["sh", str(source / "setup.sh"), "--help"], 0, "--yes"),
        ([str(python), "-I", str(source / "src/agent_run/runner_probe.py"), "--help"], 0, "usage:"),
        ([str(python), "-I", str(source / "src/agent_run/runner_setup.py"),
          str(python), "-c", "print('raw probe ready')"], 0, "raw probe ready"),
    ]
    for command, code, expected in commands:
        result = subprocess.run(command, cwd=tmp_path, env=environment,
                                capture_output=True, text=True, timeout=15)
        assert result.returncode == code, result.stdout + result.stderr
        assert expected in result.stdout + result.stderr
        assert "Traceback" not in result.stderr
    assert not list(source.rglob("__pycache__"))
