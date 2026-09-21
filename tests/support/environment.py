"""按用例及不可变模板隔离用户目录、解释器与 Git 环境。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


_GIT_LOCATION_VARIABLES = {
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_TEMPLATE_DIR",
}


def _inherited_git_override(variable: str) -> bool:
    return variable in _GIT_LOCATION_VARIABLES or variable.startswith("GIT_CONFIG")


def _user_environment(root: Path) -> dict[str, str]:
    python_directory = str(Path(sys.executable).parent)
    inherited_path = os.environ.get("PATH", os.defpath)
    environment = {
        "PATH": os.pathsep.join(
            [python_directory]
            + [entry for entry in inherited_path.split(os.pathsep) if entry != python_directory]
        ),
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    for variable, directory in (
        ("HOME", "home"),
        ("XDG_STATE_HOME", "state"),
        ("XDG_CONFIG_HOME", "config"),
        ("XDG_DATA_HOME", "data"),
        ("XDG_CACHE_HOME", "cache"),
        ("XDG_RUNTIME_DIR", "runtime"),
    ):
        path = root / directory
        path.mkdir(parents=True, mode=0o700)
        environment[variable] = str(path)
    (Path(environment["HOME"]) / ".gitconfig").write_text(
        "[user]\n\tname = Agent Run Tests\n\temail = agent-run-tests@example.invalid\n",
        encoding="utf-8",
    )
    return environment


def _apply_test_environment(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for variable in tuple(os.environ):
        if _inherited_git_override(variable):
            monkeypatch.delenv(variable)
    for variable, value in _user_environment(root).items():
        monkeypatch.setenv(variable, value)


def _assert_python_and_git_environment() -> None:
    python_directory = Path(sys.executable).parent
    assert Path(os.environ["PATH"].split(os.pathsep)[0]) == python_directory
    executable, prefix, version = json.loads(subprocess.check_output(
        ["env", "python3", "-c",
         "import json, sys; print(json.dumps([sys.executable, sys.prefix, list(sys.version_info)]))"],
        text=True,
    ))
    assert Path(executable).parent == python_directory
    assert prefix == sys.prefix
    assert version == list(sys.version_info)
    assert not set(_GIT_LOCATION_VARIABLES).intersection(os.environ)
    assert {
        key: value for key, value in os.environ.items() if key.startswith("GIT_CONFIG")
    } == {"GIT_CONFIG_NOSYSTEM": "1"}
