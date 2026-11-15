"""Run all native read commands without sleeping between synthetic failures."""
import pytest


@pytest.fixture
def immediate_read_retries(monkeypatch):
    from types import SimpleNamespace
    from agent_run.adapters import github_commands

    monkeypatch.setattr(github_commands, 'time', SimpleNamespace(sleep=lambda _seconds: None))
