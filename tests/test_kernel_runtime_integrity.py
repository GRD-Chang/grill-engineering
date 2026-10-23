"""All owned installed code participates in immutable Snapshot validation."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_run.install.installer import InstallerError, _runtime_identity, _validate_snapshot
from agent_run.install.runtime_tree import RuntimeTreeError, find_runtime_package


def _snapshot(root: Path, *, native: bool = True) -> tuple[Path, Path, str]:
    package = root / "lib/python3/site-packages/agent_run"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("legacy\n")
    kernel = package / "core"
    if native:
        kernel.mkdir()
        (kernel / "__init__.py").write_text("native\n")
        (kernel / "kernel.py").write_text("authority = 'original'\n")
    identity = _runtime_identity(find_runtime_package(root))
    (root / "manifest.json").write_text(json.dumps({"content_identity": identity}))
    return package, kernel, identity


def test_legacy_only_snapshot_keeps_its_original_content_identity(tmp_path: Path) -> None:
    package, _, identity = _snapshot(tmp_path, native=False)
    assert str(identity) == "sha256:858d007ba71589af660529c716590d1ad2c141600c99f34360772b91c2f3e93b"
    _validate_snapshot(tmp_path, _runtime_identity(package))


@pytest.mark.parametrize("change", ["edit", "delete-file", "delete-native"])
def test_native_authority_changes_are_rejected_by_snapshot_validation(tmp_path: Path, change: str) -> None:
    package, native, identity = _snapshot(tmp_path)
    _validate_snapshot(tmp_path, str(identity))
    if change == "edit":
        (native / "kernel.py").write_text("authority = 'replaced'\n")
    elif change == "delete-file":
        (native / "kernel.py").unlink()
    else:
        for path in native.iterdir():
            path.unlink()
        native.rmdir()
    assert _runtime_identity(package) != str(identity)
    with pytest.raises(InstallerError, match="runtime tree"):
        _validate_snapshot(tmp_path, str(identity))


@pytest.mark.parametrize("escape", ["package", "member"])
def test_native_runtime_symlink_escape_is_rejected(tmp_path: Path, escape: str) -> None:
    snapshot = tmp_path / "snapshot"
    _, native, _ = _snapshot(snapshot)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "authority.py").write_text("not part of the Snapshot\n")
    if escape == "package":
        for path in native.iterdir():
            path.unlink()
        native.rmdir()
        native.symlink_to(outside, target_is_directory=True)
    else:
        (native / "escaped.py").symlink_to(outside / "authority.py")
    with pytest.raises(RuntimeTreeError, match="invalid runtime tree|outside its Snapshot"):
        find_runtime_package(snapshot)


def test_native_pycache_does_not_replace_immutable_source_identity(tmp_path: Path) -> None:
    package, native, identity = _snapshot(tmp_path)
    cache = native / "__pycache__"
    cache.mkdir()
    (cache / "kernel.cpython-311.pyc").write_bytes(b"derived cache")
    assert _runtime_identity(package) == str(identity)
    _validate_snapshot(tmp_path, str(identity))
