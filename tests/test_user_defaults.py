from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from threading import Barrier

import pytest

from agent_run.contracts.agent_profiles import resolve_profiles
from agent_run.config.defaults import UserDefaultsError, UserDefaultsStore


def test_read_only_legacy_policy_and_first_save_preserve_all_settings(tmp_path: Path) -> None:
    store = UserDefaultsStore(tmp_path / "config" / "user-defaults.json")
    assert store.describe()["source"] == "builtin"
    assert not store.path.parent.exists()
    store.path.parent.mkdir()
    legacy = store.path.with_name("delivery-policy.json")
    legacy.write_text(json.dumps({"ticket_review_rounds": 7, "parent_only_paired_round": 8,
                                  "run_repair_rounds": 9, "review_deadline": "3h",
                                  "development_timeout_seconds": 1800}))
    original = legacy.read_bytes()
    shown = store.describe()
    expected = {"ticket_review_rounds": 7, "parent_only_paired_rounds": 8, "run_repair_rounds": 9,
                "invocation_deadlines": {"review": "3h", "development": 1800}}
    assert shown["source"] == "legacy-delivery-policy" and shown["source_path"] == str(legacy)
    assert shown["defaults"] == {"policy": expected}
    assert shown["policy_sources"]["ticket_review_rounds"] == "legacy-delivery-policy"
    assert shown["policy_sources"]["invocation_deadlines"]["review"] == "legacy-delivery-policy"
    assert "首次" in shown["notice"] and str(legacy) in shown["notice"]
    assert not store.path.exists()
    assert list(store.path.parent.iterdir()) == [legacy]
    with pytest.raises(UserDefaultsError):
        store.configure()
    assert list(store.path.parent.iterdir()) == [legacy] and legacy.read_bytes() == original
    result = store.configure(profile={"preset": "premium"})
    assert result["defaults"] == {"policy": expected, "profile": {"preset": "premium"}}
    assert "忽略" in result["notice"] and "保留" in result["notice"]
    assert json.loads(store.path.read_text()) == result["defaults"]
    assert legacy.read_bytes() == original
    store.configure(policy={"ticket_review_rounds": 8, "development_deadline": "2h"})
    assert store.describe()["policy"]["ticket_review_rounds"] == 8
    assert store.load()["policy"]["invocation_deadlines"] == {"review": "3h", "development": "2h"}
    assert legacy.read_bytes() == original
    assert store.describe()["source"] == "user-defaults"


@pytest.mark.parametrize("raw,diagnostic", [
    (b"{broken", "line"), (b"\xff", "utf-8"), (b"[]", "Policy"),
    (b'{"surprise":1}', "surprise"), (b'{"ticket_review_rounds":null}', "ticket_review_rounds"),
    (b'{"review_deadline":0}', "review"),
    (b'{"review_deadline":"1h","review_timeout":"2h"}', "review"),
])
def test_corrupt_legacy_policy_fails_closed(tmp_path: Path, raw: bytes, diagnostic: str) -> None:
    store = UserDefaultsStore(tmp_path / "user-defaults.json")
    legacy = store.path.with_name("delivery-policy.json")
    legacy.write_bytes(raw)
    for action in (store.describe, lambda: store.configure(language="en")):
        with pytest.raises(UserDefaultsError, match=diagnostic) as caught:
            action()
        assert str(legacy) in str(caught.value)
        assert not store.path.exists() and legacy.read_bytes() == raw


@pytest.mark.parametrize("current", ['{}', '{"policy":{"ticket_review_rounds":4}}', '{broken'])
def test_current_file_is_authoritative_even_when_empty_or_corrupt(tmp_path: Path, current: str) -> None:
    store = UserDefaultsStore(tmp_path / "user-defaults.json")
    legacy = store.path.with_name("delivery-policy.json")
    legacy.write_text("broken ignored old file")
    store.path.write_text(current)
    if current == '{broken':
        for action in (store.describe, lambda: store.configure(language="en")):
            with pytest.raises(UserDefaultsError) as caught:
                action()
            assert str(store.path) in str(caught.value)
        assert store.path.read_text() == current
    else:
        assert store.describe()["source"] == "user-defaults"
        assert store.load() == json.loads(current)
        result = store.configure(language="en")
        assert result["defaults"] == {**json.loads(current), "language": "en"}
        assert "ignored" in result["notice"] and "preserved" in result["notice"]
    assert legacy.read_text() == "broken ignored old file"


@pytest.mark.parametrize("name", ["user-defaults.json", "delivery-policy.json"])
@pytest.mark.parametrize("failure", ["unreadable", "broken-link"])
def test_inaccessible_config_is_not_treated_as_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, failure: str,
) -> None:
    store = UserDefaultsStore(tmp_path / "user-defaults.json")
    path = tmp_path / name
    if failure == "broken-link":
        path.symlink_to(tmp_path / "missing-target")
    else:
        read_text = Path.read_text

        def read(candidate: Path, *args, **kwargs):
            if candidate == path:
                raise PermissionError("injected read failure")
            return read_text(candidate, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", read)
    for action in (store.describe, lambda: store.configure(language="en")):
        with pytest.raises(UserDefaultsError) as caught:
            action()
        assert str(path) in str(caught.value)
    assert not store.path.exists()
    assert not (tmp_path / "missing-target").exists()


@pytest.mark.parametrize("old", ['{"ticket_review_rounds":7}', '{broken', None])
def test_new_file_appearing_during_fallback_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, old: str | None,
) -> None:
    store = UserDefaultsStore(tmp_path / "user-defaults.json")
    legacy = store.path.with_name("delivery-policy.json")
    if old is not None:
        legacy.write_text(old)
    read_text = Path.read_text
    appeared = False

    def read(path: Path, *args, **kwargs):
        nonlocal appeared
        if path == legacy and not appeared:
            appeared = True
            # A competing settings writer completes before this read resumes.
            # Its current document keeps the test independent of old corruption.
            store.path.write_text('{"language":"en"}')
            UserDefaultsStore(store.path).configure(policy={"ticket_review_rounds": 4})
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    shown = store.describe()
    assert appeared and shown["source"] == "user-defaults"
    assert shown["defaults"] == {"language": "en", "policy": {"ticket_review_rounds": 4}}
    assert (legacy.read_text() if old is not None else None) == old


@pytest.mark.parametrize("failure", ["replace", "directory-sync"])
def test_first_save_failure_keeps_read_only_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    store = UserDefaultsStore(tmp_path / "user-defaults.json")
    legacy = store.path.with_name("delivery-policy.json")
    legacy.write_text('{"ticket_review_rounds":7}')
    original = legacy.read_bytes()

    def fail(*args: object) -> None:
        raise OSError("injected first-save failure")

    if failure == "replace":
        monkeypatch.setattr('agent_run.config.defaults.os.replace', fail)
    else:
        monkeypatch.setattr(store, "_sync_directory", fail)
    with pytest.raises(OSError, match="injected first-save failure"):
        store.configure(language="en")
    assert not store.path.exists() and legacy.read_bytes() == original
    assert store.describe()["source"] == "legacy-delivery-policy"
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize("raw,diagnostic", [
    ('{"surprise":1}', "surprise"), ('{"policy":{"surprise":1}}', "surprise"), ('{"profile":null}', "profile"),
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
    assert store.path.stat().st_mode & 0o777 == 0o600
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
    monkeypatch.setattr('agent_run.config.defaults.os.replace', fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        store.configure(profile={"preset": "premium"}, language="en")
    assert store.path.read_bytes() == before
    assert not list(tmp_path.glob("*.tmp"))


def test_concurrent_updates_preserve_policy_fields_and_other_sections(tmp_path: Path) -> None:
    path = tmp_path / "user-defaults.json"
    legacy = path.with_name("delivery-policy.json")
    legacy.write_text('{"run_repair_rounds":7,"development_deadline":"4h"}')
    original = legacy.read_bytes()
    barrier = Barrier(4, timeout=5)
    def update(section: str) -> None:
        barrier.wait()
        store = UserDefaultsStore(path)
        if section == "policy":
            store.configure(policy={"ticket_review_rounds": 9})
        elif section == "deadline":
            store.configure(policy={"review_deadline": "90m"})
        elif section == "profile":
            store.configure(profile={"review_model": "custom-review"})
        else:
            store.configure(language="en")
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(update, section) for section in ("policy", "deadline", "profile", "language")]
        for future in futures:
            future.result(timeout=10)
    document = UserDefaultsStore(path).load()
    assert document["policy"]["ticket_review_rounds"] == 9
    assert document["policy"]["invocation_deadlines"] == {"development": "4h", "review": "90m"}
    assert document["policy"]["run_repair_rounds"] == 7
    assert legacy.read_bytes() == original
    assert document["profile"]["review_model"] == "custom-review"
    assert document["language"] == "en"


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

    monkeypatch.setattr('agent_run.config.defaults.os.fsync', fail_directory_sync)
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


@pytest.mark.parametrize("source", ["app", "host"])
def test_app_settings_sync_failure_restores_identity_and_preserves_legacy_materials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str,
) -> None:
    import os
    import stat

    store = UserDefaultsStore(tmp_path / "user-defaults.json")
    app = {"app_id": "123", "installation_id": "456", "private_key_path": str(tmp_path / "key")}
    key = tmp_path / "key"
    key.write_bytes(b"synthetic private material")
    legacy = tmp_path / "github-app.json"
    legacy.write_text(json.dumps(app))
    store.configure(github={"worker_source": "app", "app": app})
    before = store.path.read_bytes()
    legacy_before, key_before = legacy.read_bytes(), key.read_bytes()
    real_fsync = os.fsync
    failed = False

    def fail_once(descriptor: int) -> None:
        nonlocal failed
        if not failed and stat.S_ISDIR(os.fstat(descriptor).st_mode):
            failed = True
            raise OSError("injected settings directory fsync failure")
        real_fsync(descriptor)

    monkeypatch.setattr('agent_run.config.defaults.os.fsync', fail_once)
    with pytest.raises(OSError, match="injected settings directory fsync failure"):
        store.configure(github={"worker_source": source, "app": {**app, "app_id": "789"}})
    assert store.path.read_bytes() == before
    assert store.path.stat().st_mode & 0o777 == 0o600
    assert store.describe()["github"]["worker"]["app_profile"] == app
    assert legacy.read_bytes() == legacy_before
    assert key.read_bytes() == key_before
    assert not list(tmp_path.glob("*.tmp"))
