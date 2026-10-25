"""Integrity checks for a fixed, offline product distribution (not publisher authentication)."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import re
from typing import Any
import zipfile
from email.parser import BytesParser


FORMAT = "agent-run-product-release-v1"
MANIFEST = "release-manifest.json"


class ReleaseError(ValueError):
    pass


def file_digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def inventory(root: Path) -> dict[str, dict[str, Any]]:
    files = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ReleaseError("Release contains a symbolic link")
        if path.is_file():
            name = path.relative_to(root).as_posix()
            if name != MANIFEST:
                files[name] = {"sha256": file_digest(path), "size": path.stat().st_size}
        elif not path.is_dir():
            raise ReleaseError("Release contains a non-regular entry")
    return files


def runtime_digest(package: Path) -> str:
    files = {p.relative_to(package).as_posix(): file_digest(p)
             for p in sorted(package.rglob("*"))
             if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"}
    return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()


def wheel_requirement(path: Path) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_]+-[A-Za-z0-9_.+!]+-py3-none-any\.whl", path.name):
        raise ReleaseError("Only universal Python wheels are supported in this release format")
    with zipfile.ZipFile(path) as wheel:
        entries = [info for info in wheel.infolist() if info.filename.endswith(".dist-info/METADATA")]
        if len(entries) != 1 or entries[0].file_size > 128 * 1024:
            raise ReleaseError("Invalid wheel metadata")
        metadata = BytesParser().parsebytes(wheel.read(entries[0]))
    name, version = metadata.get("Name", ""), metadata.get("Version", "")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name) or not re.fullmatch(r"[A-Za-z0-9_.+!]+", version):
        raise ReleaseError("Invalid wheel name or version")
    return f"{name.lower().replace('_', '-')}=={version} --hash=sha256:{file_digest(path)}"


def verify_release(root: Path) -> dict[str, Any] | None:
    """Validate the complete directory before creating any installation state.

    Source checkouts without release markers retain the existing source installer.
    A partially removed release must never fall back to an online source build.
    """
    manifest_path = root / MANIFEST
    if not any((root / name).exists() or (root / name).is_symlink()
               for name in (MANIFEST, "requirements.lock", "wheels")):
        return None
    try:
        if manifest_path.is_symlink() or manifest_path.stat().st_size > 1024 * 1024:
            raise ReleaseError("Invalid release manifest")
        manifest = json.loads(manifest_path.read_bytes())
        if (not isinstance(manifest, dict) or manifest.get("format") != FORMAT
                or not isinstance(manifest.get("release"), str)
                or manifest.get("includes_python") is not False
                or not re.fullmatch(r"[a-f0-9]{64}", str(manifest.get("runtime_sha256", "")))):
            raise ReleaseError("Invalid release manifest")
        expected = manifest.get("files")
        if not isinstance(expected, dict) or not expected:
            raise ReleaseError("Release manifest has no file inventory")
        for name in expected:
            if (not isinstance(name, str) or PurePosixPath(name).is_absolute()
                    or any(part in {".", ".."} for part in name.split("/")) or "\\" in name):
                raise ReleaseError("Unsafe release file path")
        if inventory(root) != expected:
            raise ReleaseError("Release file inventory or SHA256 mismatch")
        wheels = sorted((root / "wheels").glob("*.whl"))
        requirements = sorted(wheel_requirement(path) for path in wheels)
        if (not wheels or len({line.split("==")[0] for line in requirements}) != len(wheels)
                or not any(line.startswith("agent-run==") for line in requirements)
                or (root / "requirements.lock").read_text() != "\n".join(requirements) + "\n"):
            raise ReleaseError("Release wheels and hashed requirements do not agree")
        if runtime_digest(root / "src/agent_run") != manifest["runtime_sha256"]:
            raise ReleaseError("Release source runtime mismatch")
        return manifest
    except (OSError, UnicodeError, json.JSONDecodeError, zipfile.BadZipFile) as error:
        raise ReleaseError("Release is incomplete or unreadable") from error
