"""Use verified local build wheels without changing the real installer."""

from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path
import subprocess


def prepare_offline_wheels(source: Path, destination: Path, requirements: Path) -> None:
    """Copy only pinned universal wheels; stale cache entries cannot affect pip."""
    destination.mkdir(parents=True)
    for line in requirements.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        requirement, digest_option = line.split()
        name, version = requirement.split("==")
        wheel = source / f"{name.replace('-', '_')}-{version}-py3-none-any.whl"
        if not wheel.is_file():
            raise ValueError(f"Missing test build wheel {wheel.name}; run make test-prepare")
        expected = digest_option.removeprefix("--hash=sha256:")
        actual = hashlib.sha256(wheel.read_bytes()).hexdigest()
        if actual != expected:
            raise ValueError(f"Invalid test build wheel {wheel.name}; run make test-prepare")
        shutil.copy2(wheel, destination / wheel.name)


def offline_pip_environment(wheelhouse: Path) -> dict[str, str]:
    return {
        "PIP_CONFIG_FILE": os.devnull,
        "PIP_NO_INDEX": "1",
        "PIP_FIND_LINKS": wheelhouse.resolve().as_uri(),
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    }


PROJECT_ROOT = Path(__file__).parents[2]

def _source_tree(tmp_path: Path, *, real_install: bool = False) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    shutil.copy2(PROJECT_ROOT / "install.sh", source / "install.sh")
    shutil.copy2(PROJECT_ROOT / "pyproject.toml", source / "pyproject.toml")
    shutil.copy2(PROJECT_ROOT / "LICENSE", source / "LICENSE")
    shutil.copytree(
        PROJECT_ROOT / "src",
        source / "src",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.egg-info"),
    )
    if not real_install:
        shutil.copy2(
            PROJECT_ROOT / "tests" / "support" / "fast_runner_installer.py",
            source / "fast_runner_installer.py",
        )
        (source / "install.sh").write_text(
            "#!/bin/sh\n"
            "set -eu\n"
            'SOURCE_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)\n'
            'exec python3 "$SOURCE_DIR/fast_runner_installer.py" "$@" '
            '--source "$SOURCE_DIR"\n',
            encoding="utf-8",
        )
    (source / "install.sh").chmod(0o755)
    return source

def _fake_codex(
    tmp_path: Path,
    *,
    status: str = "ok",
    behavior: str = "success",
) -> tuple[Path, Path, Path]:
    directory = tmp_path / "bin"
    directory.mkdir()
    count = tmp_path / "codex-count"
    status_file = tmp_path / "codex-status"
    status_file.write_text(status, encoding="utf-8")
    behavior_file = tmp_path / "codex-behavior"
    behavior_file.write_text(behavior, encoding="utf-8")
    pid_file = tmp_path / "codex-pid"
    descendant_pid_file = tmp_path / "codex-descendant-pid"
    cwd_file = tmp_path / "codex-cwd"
    path_file = tmp_path / "codex-path"
    script = directory / "codex"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "import os\n"
        "import pathlib\n"
        "import sys\n"
        "import time\n"
        f"count = pathlib.Path({str(count)!r})\n"
        f"status_file = pathlib.Path({str(status_file)!r})\n"
        f"behavior_file = pathlib.Path({str(behavior_file)!r})\n"
        f"pid_file = pathlib.Path({str(pid_file)!r})\n"
        f"descendant_pid_file = pathlib.Path({str(descendant_pid_file)!r})\n"
        f"cwd_file = pathlib.Path({str(cwd_file)!r})\n"
        f"path_file = pathlib.Path({str(path_file)!r})\n"
        "current = int(count.read_text() if count.exists() else '0')\n"
        "count.write_text(str(current + 1))\n"
        "pid_file.write_text(str(os.getpid()))\n"
        "cwd_file.write_text(str(pathlib.Path.cwd()))\n"
        "path_file.write_text(os.environ['PATH'])\n"
        "behavior = behavior_file.read_text()\n"
        "if behavior == 'nonzero':\n"
        "    sys.exit(7)\n"
        "if behavior == 'missing':\n"
        "    sys.exit(0)\n"
        "if behavior == 'large':\n"
        "    output = pathlib.Path(sys.argv[sys.argv.index('--output-last-message') + 1])\n"
        "    output.write_bytes(b'x' * (16 * 1024 + 1))\n"
        "    sys.exit(0)\n"
        "if behavior == 'timeout':\n"
        "    time.sleep(130)\n"
        "if behavior == 'sleep':\n"
        "    time.sleep(2)\n"
        "if behavior == 'wait-for-release':\n"
        "    deadline = time.monotonic() + 30\n"
        "    while not pid_file.with_name('codex-release').exists():\n"
        "        if time.monotonic() >= deadline:\n"
        "            sys.exit(8)\n"
        "        time.sleep(0.01)\n"
        "if behavior == 'fork-setsid':\n"
        "    if '--output-last-message' not in sys.argv:\n"
        "        sys.exit(0)\n"
        "    child = os.fork()\n"
        "    if child == 0:\n"
        "        descendant_pid_file.write_text(str(os.getpid()))\n"
        "        os.setsid()\n"
        "        time.sleep(30)\n"
        "    for _ in range(100):\n"
        "        if descendant_pid_file.exists():\n"
        "            break\n"
        "        time.sleep(0.01)\n"
        "    output = pathlib.Path(sys.argv[sys.argv.index('--output-last-message') + 1])\n"
        "    output.write_text(json.dumps({'status': status_file.read_text()}))\n"
        "    os._exit(0)\n"
        "schema = pathlib.Path(sys.argv[sys.argv.index('--output-schema') + 1])\n"
        "assert json.loads(schema.read_text()) == {\n"
        "    'type': 'object',\n"
        "    'additionalProperties': False,\n"
        "    'properties': {'status': {'type': 'string', 'enum': ['ok']}},\n"
        "    'required': ['status'],\n"
        "}\n"
        "if behavior == 'wrong-result':\n"
        "    output = pathlib.Path(sys.argv[sys.argv.index('--output-last-message') + 1])\n"
        "    output.write_text(json.dumps({'status': 'not-ok'}))\n"
        "    sys.exit(0)\n"
        "output = pathlib.Path(sys.argv[sys.argv.index('--output-last-message') + 1])\n"
        "output.write_text(json.dumps({'status': status_file.read_text()}))\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return directory, count, status_file

def _run(
    source: Path,
    home: Path,
    fake_bin: Path,
    *arguments: str,
    path: str | None = None,
    cwd: Path | None = None,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    process_environment = (
        os.environ.copy() if environment is None else environment.copy()
    )
    process_environment.update(
        {
            "HOME": str(home),
            "XDG_DATA_HOME": str(home / "data"),
            "XDG_CONFIG_HOME": str(home / "config"),
            "XDG_STATE_HOME": str(home / "state"),
            "PATH": path
            if path is not None
            else f"{fake_bin}{os.pathsep}{process_environment.get('PATH', '')}",
        }
    )
    return subprocess.run(
        [str(source / "install.sh"), *arguments],
        cwd=cwd or source,
        env=process_environment,
        text=True,
        capture_output=True,
        check=False,
    )

def _data_root(home: Path) -> Path:
    return home / "data" / "agent-run"
