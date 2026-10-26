"""Repeated observations of one milestone keep one original delivery budget."""
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

import agent_run.storage.notification_outbox as outbox
from agent_run.notifications.envelope import event_envelope
from agent_run.notifications.sender import NativeNotifications
from support.notification import _kernel


@pytest.mark.parametrize('attempted', [False, True])
def test_one_repair_milestone_updates_only_an_unsent_card_and_keeps_its_original_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, attempted: bool,
) -> None:
    kernel = _kernel(tmp_path)
    state = {'run_id': 'run-native', 'repository': 'example/project', 'language': 'en',
             'parent': {'number': 1, 'title': 'Parent'}}
    planned = event_envelope(state, 'acceptance_repair', 'run-native', 'Repair is planned', live=False)
    active = {**planned, 'title': 'Repair is active', 'live': True}
    pages = iter([([planned], False, 1), ([active], False, 2), ([active], False, 3)])
    # Isolate delivery observation from the physical sender. These are two
    # observations of one committed milestone, not two domain transitions.
    monkeypatch.setattr(NativeNotifications, '_page', lambda _self, _after: next(pages))
    monkeypatch.setattr(NativeNotifications, '_work', lambda _self: None)
    sender = NativeNotifications(kernel.storage.path, tmp_path)
    try:
        with outbox.transaction(tmp_path, 'run-native') as document:
            item = next(row for row in document['pending'] if row['event']['id'] == planned['id'])
            if attempted:
                item.update(attempts=1, outcome='failed')
                item['attempt_history'] = [{'number': 1, 'started_at': item['created_at'], 'outcome': 'failed'}]
            before = deepcopy(item)
        sender.observe_committed()
        assert sender.failure_reason is None
        document = outbox.read_notifications(tmp_path, 'run-native')
        matches = [row for row in document['pending'] if row['event']['id'] == planned['id']]
        assert len(matches) == 1
        current = matches[0]
        assert {key: value for key, value in current.items() if key != 'event'} == {
            key: value for key, value in before.items() if key != 'event'}
        assert current['event'] == (planned if attempted else active)
        # Delivery completion does not authorize a second budget on a later
        # history observation of the same planned/active milestone.
        with outbox.transaction(tmp_path, 'run-native') as document:
            item = next(row for row in document['pending'] if row['event']['id'] == planned['id'])
            if not attempted:
                item.update(attempts=1, outcome='sending')
                item['attempt_history'] = [{'number': 1, 'started_at': item['created_at'], 'outcome': 'sending'}]
            outbox.finish_attempt(item, {'outcome': 'success', 'reason': None}, item['created_at'] + 1)
            outbox.settle(document, item['created_at'] + 1)
            delivered = deepcopy(next(row for row in document['records'] if row['id'] == planned['id']))
        sender.observe_committed()
        assert sender.failure_reason is None
        document = outbox.read_notifications(tmp_path, 'run-native')
        assert not any(row['event']['id'] == planned['id'] for row in document['pending'])
        assert [row for row in document['records'] if row['id'] == planned['id']] == [delivered]
    finally:
        sender.close(drain=False)
