from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from threading import Barrier

import pytest

from agent_run.agent_profiles import resolve_profiles
from agent_run.user_defaults import UserDefaultsError, UserDefaultsStore


def test_read_only_builtin_and_legacy_migration(tmp_path: Path) -> None:
    store = UserDefaultsStore(tmp_path / "config" / "user-defaults.json")
    assert store.describe()["source"] == "builtin"
    assert not store.path.parent.exists()
    store.path.parent.mkdir()
    store.legacy_path.write_text('{"ticket_review_rounds": 7}')
    assert store.describe()["source"] == "legacy-delivery-policy"
    assert not store.path.exists()
    result = store.configure(profile={"preset": "premium"})
    assert result["policy"]["ticket_review_rounds"] == 7
    assert result["legacy_ignored"] is True
    store.legacy_path.write_text("broken")
    assert store.describe()["source"] == "user-defaults"


@pytest.mark.parametrize("raw,diagnostic", [
    ('{"surprise":1}', "surprise"), ('{"profile":null}', "profile"),
    ('{"policy":{"ticket_review_rounds":null}}', "ticket_review_rounds"),
    ('{"profile":{"development_model":null}}', "development_model"),
    ('{"profile":{"other_model":"x"}}', "other_model"),
    ('{"profile":{"preset":[]}}', "preset"),
    ('{"profile":{"development_effort":"wrong"}}', "development_effort"),
    ('{broken', "line"),
])
def test_invalid_direct_edits_fail_without_rewriting(tmp_path: Path, raw: str, diagnostic: str) -> None:
    store = UserDefaultsStore(tmp_path / "user-defaults.json")
    store.path.write_text(raw)
    with pytest.raises(UserDefaultsError, match=diagnostic):
        store.load()
    with pytest.raises(UserDefaultsError):
        store.configure(policy={"run_repair_rounds": 4})
    assert store.path.read_text() == raw


def test_sparse_updates_priorities_and_references(tmp_path: Path) -> None:
    store = UserDefaultsStore(tmp_path / "user-defaults.json")
    store.configure(policy={"review_deadline": "3h"}, profile={"development_model": "custom"})
    store.configure(policy={"ticket_review_rounds": 8}, profile={"review_effort": "high"})
    document = json.loads(store.path.read_text())
    assert document["policy"] == {"invocation_deadlines": {"review": "3h"}, "ticket_review_rounds": 8}
    preset, overrides = store.resolve_creation()
    resolved = resolve_profiles(preset=preset, overrides=overrides)["profiles"]
    assert resolved["publication"]["model"] == "custom"
    assert resolved["publication"]["reference"] == "development"
    preset, overrides = store.resolve_creation(overrides={"development_model": "once"})
    assert resolve_profiles(preset=preset, overrides=overrides)["profiles"]["publication"]["model"] == "once"
    preset, overrides = store.resolve_creation(preset="premium")
    assert overrides == {}
    assert preset == "premium"
    store.configure(profile={"publication_model": "independent"})
    store.configure(profile={"publication_from_development": True})
    assert "publication_model" not in store.load()["profile"]
    assert store.describe()["profile"]["profiles"]["publication"]["reference"] == "development"
    store.configure(profile={"publication_effort": "high"})
    assert store.describe()["profile"]["profiles"]["publication"]["reference"] is None


def test_validation_and_atomic_replace_failure_preserve_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = UserDefaultsStore(tmp_path / "user-defaults.json")
    store.configure(policy={"ticket_review_rounds": 5})
    before = store.path.read_bytes()
    with pytest.raises(UserDefaultsError):
        store.configure(profile={"publication_from_development": True, "publication_model": "x"})
    def fail_replace(*args: object) -> None:
        raise OSError("replace failed")
    monkeypatch.setattr("agent_run.user_defaults.os.replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        store.configure(profile={"preset": "premium"})
    assert store.path.read_bytes() == before
    assert not list(tmp_path.glob("*.tmp"))


def test_concurrent_updates_preserve_both_sections(tmp_path: Path) -> None:
    path = tmp_path / "user-defaults.json"
    barrier = Barrier(2, timeout=5)
    def update(policy: bool) -> None:
        barrier.wait()
        store = UserDefaultsStore(path)
        if policy:
            store.configure(policy={"ticket_review_rounds": 9})
        else:
            store.configure(profile={"review_model": "custom-review"})
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(update, policy) for policy in (True, False)]
        for future in futures:
            future.result(timeout=10)
    document = UserDefaultsStore(path).load()
    assert document["policy"]["ticket_review_rounds"] == 9
    assert document["profile"]["review_model"] == "custom-review"


@pytest.mark.parametrize("existing", [False, True])
def test_directory_sync_failure_restores_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, existing: bool
) -> None:
    import os
    import stat

    store = UserDefaultsStore(tmp_path / "user-defaults.json")
    if existing:
        store.configure(policy={"ticket_review_rounds": 5})
    before = store.path.read_bytes() if existing else None
    real_fsync = os.fsync

    def fail_directory_sync(descriptor: int) -> None:
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError("directory sync failed")
        real_fsync(descriptor)

    monkeypatch.setattr("agent_run.user_defaults.os.fsync", fail_directory_sync)
    with pytest.raises(OSError, match="已恢复原配置"):
        store.configure(profile={"preset": "premium"})
    assert (store.path.read_bytes() if store.path.exists() else None) == before
    assert not list(tmp_path.glob("*.tmp"))


def test_save_reports_normalized_model_and_result(tmp_path: Path) -> None:
    store = UserDefaultsStore(tmp_path / "user-defaults.json")
    assert store.describe()["result"] == "settings"
    result = store.configure(profile={"development_model": "  custom-model  "})
    assert result["result"] == "configured"
    assert result["changed"]["profile"]["development_model"] == "custom-model"
    assert result["profile"]["profiles"]["development"]["model"] == "custom-model"
    assert json.loads(store.path.read_text())["profile"]["development_model"] == "custom-model"
