"""Run all native read commands without sleeping between synthetic failures."""
from time import monotonic
from types import SimpleNamespace

import pytest


def set_read_retry_sleep(monkeypatch, sleep):
    """Observe adapter backoff without changing subprocess exit-wait timing."""
    from agent_run.adapters import github, github_commands, remote

    for module in (github_commands, github, remote):
        monkeypatch.setattr(module, 'time', SimpleNamespace(sleep=sleep, monotonic=monotonic))


@pytest.fixture
def immediate_read_retries(monkeypatch):
    set_read_retry_sleep(monkeypatch, lambda _seconds: None)
