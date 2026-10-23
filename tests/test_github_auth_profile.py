from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_run.config.github_app import GitHubAppProfileStore, GitHubAuthProfileError


def _legacy_profile(tmp_path: Path) -> tuple[GitHubAppProfileStore, Path]:
    key = tmp_path / "reference.pem"
    key.write_text("synthetic key material, not read by migration")
    key.chmod(0o600)
    path = tmp_path / "config" / "github-app.json"
    path.parent.mkdir(mode=0o700)
    path.write_text(json.dumps({
        "app_id": "123", "installation_id": "456", "private_key_path": str(key),
    }))
    path.chmod(0o600)
    return GitHubAppProfileStore(path), key


def test_load_reads_legacy_reference_without_reading_key_or_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, key = _legacy_profile(tmp_path)
    before = store.path.read_bytes()
    real_open = Path.open

    def metadata_only(path: Path, *args: object, **kwargs: object):
        assert path != key, "loading legacy metadata must not read private key bytes"
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", metadata_only)
    profile = store.load()
    assert profile is not None
    assert (profile.app_id, profile.installation_id, profile.private_key_path) == ("123", "456", key)
    assert store.path.read_bytes() == before
    assert store.path.stat().st_mode & 0o777 == 0o600
    assert store.path.parent.stat().st_mode & 0o777 == 0o700
    assert list(store.path.parent.iterdir()) == [store.path]


@pytest.mark.parametrize("directory_present", [False, True])
def test_missing_legacy_profile_does_not_create_files(tmp_path: Path, directory_present: bool) -> None:
    path = tmp_path / "config" / "github-app.json"
    if directory_present:
        path.parent.mkdir(mode=0o700)
    assert GitHubAppProfileStore(path).load() is None
    assert path.parent.exists() == directory_present
    assert not path.exists()


@pytest.mark.parametrize("invalid", [
    {"app_id": "0"}, {"installation_id": "9" * 65}, {"app_id": "１２３"},
    {"app_id": 123}, {"extra": True}, {"private_key_path": "relative.pem"},
])
def test_invalid_legacy_values_fail_without_rewriting(tmp_path: Path, invalid: dict) -> None:
    store, _ = _legacy_profile(tmp_path)
    document = json.loads(store.path.read_text())
    store.path.write_text(json.dumps({**document, **invalid}))
    before = store.path.read_bytes()
    with pytest.raises(GitHubAuthProfileError):
        store.load()
    assert store.path.read_bytes() == before


@pytest.mark.parametrize("raw", [b"{broken", b"\xff", b"[]"])
def test_malformed_legacy_document_fails_closed(tmp_path: Path, raw: bytes) -> None:
    store, _ = _legacy_profile(tmp_path)
    store.path.write_bytes(raw)
    with pytest.raises(GitHubAuthProfileError):
        store.load()
    assert store.path.read_bytes() == raw


def test_invalid_existing_profile_fails_closed(tmp_path: Path) -> None:
    store, key = _legacy_profile(tmp_path)
    key.unlink()
    with pytest.raises(GitHubAuthProfileError, match="私钥文件不存在"):
        store.load()


@pytest.mark.parametrize("unsafe", ["directory-mode", "file-mode", "directory-link", "file-link", "file-directory"])
def test_unsafe_legacy_metadata_fails_closed(tmp_path: Path, unsafe: str) -> None:
    store, _ = _legacy_profile(tmp_path)
    if unsafe == "directory-mode":
        store.path.parent.chmod(0o755)
    elif unsafe == "file-mode":
        store.path.chmod(0o644)
    elif unsafe == "directory-link":
        moved = tmp_path / "actual-config"
        store.path.parent.rename(moved)
        store.path.parent.symlink_to(moved, target_is_directory=True)
    elif unsafe == "file-link":
        moved = store.path.with_name("actual.json")
        store.path.rename(moved)
        store.path.symlink_to(moved)
    else:
        store.path.unlink()
        store.path.mkdir()
    with pytest.raises(GitHubAuthProfileError):
        store.load()


@pytest.mark.parametrize("unsafe", ["public", "directory", "worktree", "bare"])
def test_unsafe_legacy_key_reference_fails_closed(tmp_path: Path, unsafe: str) -> None:
    store, key = _legacy_profile(tmp_path)
    if unsafe == "public":
        key.chmod(0o644)
    elif unsafe == "directory":
        key.unlink()
        key.mkdir(mode=0o700)
    elif unsafe == "worktree":
        (tmp_path / ".git").mkdir()
    else:
        bare = tmp_path / "bare"
        bare.mkdir()
        key.rename(bare / key.name)
        for name in ("HEAD", "config"):
            (bare / name).write_text("synthetic git metadata")
        for name in ("objects", "refs"):
            (bare / name).mkdir()
        document = json.loads(store.path.read_text())
        document["private_key_path"] = str(bare / key.name)
        store.path.write_text(json.dumps(document))
    with pytest.raises(GitHubAuthProfileError):
        store.load()
