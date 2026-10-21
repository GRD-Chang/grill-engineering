"""Terminal cards follow original close/cleanup and Abandon receipts."""
from pathlib import Path
from typing import Any

import pytest

from agent_run import notification_outbox as outbox
from agent_run.notification_event_facts import event_copy
from agent_run_kernel import DeliveryKind
from agent_run_kernel.local_effects import LocalEffects
from agent_run_kernel.notifications import NativeNotifications
from test_kernel_notification_lifecycle import _enabled_scenario


@pytest.mark.parametrize('kind', [DeliveryKind.PARENT_ONLY, DeliveryKind.FINAL_RUN])
def test_terminal_notification_keeps_close_cleanup_and_completed_as_distinct_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: DeliveryKind,
) -> None:
    scenario = _enabled_scenario(tmp_path, monkeypatch, kind)
    rendered: list[dict[str, Any]] = []

    def transport(_config: Any, card: dict[str, Any], *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        rendered.append(card)
        return {'outcome': 'success', 'reason': None}

    def observe() -> list[dict[str, Any]]:
        observer = NativeNotifications(scenario.kernel.storage.path, tmp_path, transport=transport)
        observer.close()
        assert observer.failure_reason is None
        return [record for record in outbox.read_notifications(tmp_path, scenario.spec.run_id)['records']
                if record['kind'] == 'boundary']

    for _ in range(100):
        step = scenario.step()
        if step.kind == 'approval':
            scenario.kernel.control('approve', 'original-approval', subject=scenario.delivery.key, world=scenario.world())
            continue
        if step.effect is not None and step.effect.operation == 'close':
            break
        assert step.kind not in {'paused', 'waiting', 'complete'}
    else:
        pytest.fail('Original close operation not reached')
    # An acknowledged remote close still needs its original committed consume.
    assert observe() == []
    assert scenario.step().kind == 'progress'
    with scenario.kernel.storage.transaction(write=False) as tx:
        assert tx.one('SELECT state FROM resource_cleanup WHERE subject=?', (scenario.delivery.key,))[0] != 'complete'
        assert tx.one("SELECT count(*) FROM effects WHERE subject=? AND operation='close' AND status='confirmed'", (scenario.delivery.key,))[0] == 1
    pending = observe()
    assert len(pending) == 1
    assert rendered[-1]['header']['template'] == 'yellow'
    assert rendered[-1]['header']['title']['content'] == event_copy({'language': 'en'}, 'cleanup')
    assert observe() == pending

    assert scenario.run(approve=True).kind == 'complete'
    completed = observe()
    assert len(completed) == 2 and completed[0] == pending[0]
    assert completed[1]['id'] != completed[0]['id']
    assert rendered[-1]['header']['template'] == 'green'
    assert rendered[-1]['header']['title']['content'] == event_copy({'language': 'en'}, 'completed')
    assert str(rendered[-1]).find('/pull/') >= 0
    assert observe() == completed


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
        observer = NativeNotifications(scenario.kernel.storage.path, tmp_path, transport=transport)
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
