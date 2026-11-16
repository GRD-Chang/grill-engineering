from __future__ import annotations

from support.user_defaults import (
    settings,
)

import json
from pathlib import Path
from typing import Any

import pytest


from agent_run.config.defaults import UserDefaultsStore


def test_resync_limit_configures_future_runs() -> None:
    code, saved = settings("configure", "--integration-resync-limit", "0")
    assert code == 0
    assert saved["policy"]["integration_resync_limit"] == 0
    assert UserDefaultsStore().load()["policy"]["integration_resync_limit"] == 0
    code, shown = settings("show")
    assert code == 0
    assert shown["policy_sources"]["integration_resync_limit"] == "user-defaults"


def test_public_settings_migrate_old_policy_and_preserve_atomic_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    store = UserDefaultsStore()
    legacy = store.path.with_name("delivery-policy.json")
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text('{"ticket_review_rounds": 7}')
    old_bytes = legacy.read_bytes()
    code, initial = settings("show")
    assert code == 0 and initial["source"] == "legacy-delivery-policy"
    assert initial["source_path"] == str(legacy)
    assert initial["defaults"] == {"policy": {"ticket_review_rounds": 7}}
    assert "首次 settings configure" in initial["notice"]
    assert legacy.read_bytes() == old_bytes
    assert not store.path.exists()
    code, saved = settings("configure", "--preset", "premium")
    assert code == 0 and saved["defaults"] == {
        "policy": {"ticket_review_rounds": 7}, "profile": {"preset": "premium"}}
    assert saved["source"] == "user-defaults" and saved["source_path"] == str(store.path)
    assert "已忽略并保留" in saved["notice"]
    assert legacy.read_bytes() == old_bytes
    previous = store.path.read_bytes()
    code, rejected = settings("configure", "--development-effort", "invalid")
    assert code == 2 and rejected["result"] == "error"
    assert store.path.read_bytes() == previous

    def fail_replace(*args: object) -> None:
        raise OSError("injected write failure")

    with monkeypatch.context() as scoped:
        scoped.setattr('agent_run.config.defaults.os.replace', fail_replace)
        code, rejected = settings("configure", "--ticket-review-rounds", "9")
    assert code == 2 and rejected["result"] == "error"
    assert store.path.read_bytes() == previous
    assert not list(store.path.parent.glob("*.tmp"))
    store.path.write_text('{"profile":{"review_effort":"invalid"}}')
    code, rejected = settings("show")
    assert code == 2
    assert "review_effort" in json.dumps(rejected)
    assert store.path.read_text() == '{"profile":{"review_effort":"invalid"}}'


@pytest.mark.parametrize("value", [None, False, 1, "invalid", [], {}])
def test_thread_policy_file_rejects_invalid_values(value: Any) -> None:
    store = UserDefaultsStore()
    store.path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps({"policy": {"development_thread_policy": value}})
    store.path.write_text(content)
    code, rejected = settings("show")
    assert code == 2
    assert "development_thread_policy" in json.dumps(rejected)
    assert store.path.read_text() == content


def test_notification_settings_are_optional_and_bound_to_an_app() -> None:
    code, initial = settings("show")
    assert code == 0
    assert initial["notifications"]["enabled"] is False
    code, configured = settings(
        "configure", "--notifications", "--notification-open-id", "ou_recipient",
        "--notification-profile", "work", "--notification-app-id", "cli_application",
    )
    assert code == 0
    assert configured["notifications"] == {
        "enabled": True, "open_id": "ou_recipient", "profile": "work", "app_id": "cli_application", "mode": "concise",
    }
    settings("configure", "--no-notifications")
    assert UserDefaultsStore().load()["notifications"]["open_id"] == "ou_recipient"


def test_bad_notification_settings_do_not_block_run_configuration() -> None:
    from agent_run.config.defaults import notification_snapshot

    store = UserDefaultsStore()
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text('{"notifications":{"enabled":"yes"}}')
    document = store.load()
    snapshot = notification_snapshot(document["notifications"])
    assert snapshot["enabled"] is False
    assert "notifications.enabled" in snapshot["unavailable_reason"]
    assert "unavailable_reason" not in notification_snapshot(document["notifications"], disabled=True)
    assert store.resolve_creation(document=document)[0] == "economy"


@pytest.mark.parametrize("mode", ["concise", "detailed"])
def test_notification_mode_configuration_and_override(mode: str) -> None:
    from agent_run.config.defaults import notification_snapshot

    code, configured = settings("configure", "--notification-mode", mode)
    assert code == 0
    assert configured["notifications"]["enabled"] is True
    assert configured["notifications"]["mode"] == mode
    legacy = {"enabled": True, "open_id": "ou_old"}
    assert notification_snapshot(legacy)["mode"] == "concise"
    assert "mode" not in legacy
    assert notification_snapshot(legacy, mode=mode)["mode"] == mode
    settings("configure", "--no-notifications")
    assert settings("show")[1]["notifications"]["enabled"] is False
    assert settings("show")[1]["notifications"]["mode"] == mode
    assert notification_snapshot({"mode": "invalid"})["unavailable_reason"]


@pytest.mark.parametrize("value", [None, False, 1, "fr", [], {}])
def test_language_file_validation_preserves_original(value: object) -> None:
    store = UserDefaultsStore()
    store.path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps({"language": value})
    store.path.write_text(raw)
    code, error = settings("show")
    assert code == 2
    assert "language" in json.dumps(error)
    assert store.path.read_text() == raw


def test_short_copy_selects_frozen_run_or_personal_language() -> None:
    from agent_run.contracts.copy import selected_language, text
    from agent_run.cli.messages import personal_language

    assert personal_language() == "zh"
    UserDefaultsStore().configure(language="en")
    assert personal_language() == "en"
    assert selected_language({"language": "zh"}) == "zh"
    assert text("settings.personal_title", language=personal_language()) == "Personal Run defaults"
    assert text("settings.personal_title", language=selected_language({"language": "zh"})) == "个人运行默认配置"


def test_ticket_ci_settings_are_explicit_and_can_be_disabled() -> None:
    code, builtin = settings("show")
    assert code == 0
    assert builtin["policy"]["ticket_ci_development_limit"] == 3
    assert builtin["policy"]["ticket_ci_review"] is False
    code, enabled = settings("configure", "--ticket-ci-development-limit", "5", "--ticket-ci-review")
    assert code == 0
    assert enabled["policy"]["ticket_ci_development_limit"] == 5
    assert enabled["policy"]["ticket_ci_review"] is True
    code, disabled = settings("configure", "--no-ticket-ci-review")
    assert code == 0
    assert disabled["policy"]["ticket_ci_review"] is False
    assert disabled["policy"]["ticket_ci_development_limit"] == 5
    assert UserDefaultsStore().load()["policy"]["ticket_ci_review"] is False
