"""Read-only managed installation paths, identities and complete generations."""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

from agent_run.config.paths import app_data_root
from agent_run.install.runtime_tree import RuntimeTreeError, find_runtime_package


class InstallerError(RuntimeError):
    """A bounded operational error from the source installer."""


@dataclass(frozen=True)
class InstallPaths:
    data_root: Path
    snapshots: Path
    generations: Path
    staging: Path
    active: Path
    lock: Path
    user_bin: Path
    stable_entry: Path
    profile: Path

    @classmethod
    def from_environment(cls) -> "InstallPaths":
        data_root = app_data_root()
        user_bin = (Path.home() / ".local" / "bin").resolve()
        return cls(
            data_root=data_root,
            snapshots=data_root / "snapshots",
            generations=data_root / "generations",
            staging=data_root / "staging",
            active=data_root / "active",
            lock=data_root / "install.lock",
            user_bin=user_bin,
            stable_entry=user_bin / "agent-run",
            profile=(Path.home() / ".profile").resolve(),
        )


def runtime_package(candidate: Path) -> Path:
    try:
        return find_runtime_package(candidate)
    except RuntimeTreeError as error:
        raise InstallerError(str(error)) from error


def runtime_identity(package: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(package.rglob("*")):
        if "__pycache__" in path.parts:
            continue
        relative = path.relative_to(package).as_posix().encode("utf-8")
        digest.update(relative)
        digest.update(b"\0")
        if path.is_symlink():
            digest.update(b"L")
            digest.update(os.readlink(path).encode("utf-8"))
        elif path.is_file():
            digest.update(b"F")
            digest.update(path.read_bytes())
        elif path.is_dir():
            digest.update(b"D")
        else:
            raise InstallerError("runtime tree 包含不支持的文件类型")
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"


def validate_snapshot(snapshot: Path, identity: str) -> None:
    if manifest_identity(snapshot) != identity:
        raise InstallerError("已有 Snapshot manifest 与目录身份不一致")
    if runtime_identity(runtime_package(snapshot)) != identity:
        raise InstallerError("Snapshot runtime tree 与 manifest identity 不一致")


def manifest_identity(snapshot: Path) -> str:
    try:
        loaded: object = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise InstallerError("Snapshot manifest 无法读取") from error
    identity = loaded.get("content_identity") if isinstance(loaded, dict) else None
    if not isinstance(identity, str):
        raise InstallerError("Snapshot manifest 格式无效")
    return identity


def read_active(paths: InstallPaths) -> tuple[Path | None, Path | None, Path | None]:
    if not paths.active.exists() and not paths.active.is_symlink():
        return None, None, None
    if not paths.active.is_symlink():
        raise InstallerError("Active Runner 不是受管 generation symlink")
    generation = paths.active.resolve()
    if not generation.is_dir() or generation.parent != paths.generations.resolve():
        raise InstallerError("Active Runner generation 无效")
    current = generation_link(generation / "current")
    previous = (
        generation_link(generation / "previous")
        if (generation / "previous").exists() or (generation / "previous").is_symlink()
        else None
    )
    validate_snapshot(current, manifest_identity(current))
    if previous is not None:
        validate_snapshot(previous, manifest_identity(previous))
    return current, previous, generation


def generation_link(link: Path) -> Path:
    if not link.is_symlink():
        raise InstallerError("generation 缺少完整 Snapshot symlink")
    target = link.resolve()
    if not target.is_dir():
        raise InstallerError("generation 指向不存在的 Snapshot")
    return target
