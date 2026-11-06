"""Abandon has its own terminal receipt and never announces delivery."""
from pathlib import Path
from typing import Any

import pytest

import agent_run.storage.notification_outbox as outbox
from agent_run.notifications.envelope import event_copy
from agent_run.model import DeliveryKind
from agent_run.adapters.local_git import LocalEffects
from agent_run.notifications.sender import NativeNotifications
from support.notification import _enabled_scenario


def test_abandon_notification_waits_for_original_finite_cleanup_and_does_not_announce_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = _enabled_scenario(tmp_path, monkeypatch, DeliveryKind.PARENT_ONLY)
    inspections = LocalEffects(scenario.port.git).inspect_resources(scenario.kernel.generation_resources())
    assert scenario.kernel.control('abandon', 'original-abandon', resource_inspections=inspections) == 'pending'
    rendered: list[dict[str, Any]] = []

    def transport(_config: Any, card: dict[str, Any], *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        rendered.append(card)
        return {'outcome': 'success', 'reason': None}

    def observe() -> list[dict[str, Any]]:
        observer = NativeNotifications(scenario.kernel.storage.path, tmp_path, transport=transport, storage=scenario.kernel.storage)
        observer.close()
        assert observer.failure_reason is None
        return [record for record in outbox.read_notifications(tmp_path, scenario.spec.run_id)['records']
                if record['kind'] == 'boundary']

    assert observe() == []
    assert scenario.run().kind == 'complete'
    abandoned = observe()
    assert len(abandoned) == 1
    assert rendered[-1]['header']['title']['content'] == event_copy({'language': 'en'}, 'abandoned')
    assert rendered[-1]['header']['template'] == 'yellow'
    assert observe() == abandoned
    with scenario.kernel.storage.transaction(write=False) as tx:
        assert tx.one("SELECT status FROM actions WHERE id='original-abandon'")[0] == 'complete'
        assert not tx.rows("SELECT id FROM effects WHERE operation='close'")
