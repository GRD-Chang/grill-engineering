"""Shared locations for Runner-owned data, configuration and temporary files."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path


def _directory(
    variable: str, fallback: Path, environment: Mapping[str, str] | None
) -> Path:
    selected = os.environ if environment is None else environment
    value = selected.get(variable)
    root = Path(value).expanduser() if value else fallback
    if not root.is_absolute():
        raise ValueError(f"{variable} 必须是绝对路径")
    return Path(os.path.normpath(root))


def app_data_root(environment: Mapping[str, str] | None = None) -> Path:
    return _directory(
        "XDG_DATA_HOME", Path.home() / ".local" / "share", environment
    ).resolve() / "agent-run"


def app_config_root(environment: Mapping[str, str] | None = None) -> Path:
    return _directory("XDG_CONFIG_HOME", Path.home() / ".config", environment) / "agent-run"


def app_runtime_root(environment: Mapping[str, str] | None = None) -> Path:
    selected = os.environ if environment is None else environment
    if selected.get("XDG_RUNTIME_DIR"):
        return _directory("XDG_RUNTIME_DIR", Path("/tmp"), selected) / "agent-run"
    return Path("/tmp") / f"agent-run-{os.geteuid()}"
