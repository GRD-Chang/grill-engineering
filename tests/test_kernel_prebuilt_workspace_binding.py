"""Ordinary native workspace allocation does not require an import certificate."""
from support.effect_boundary import workspace_intent


def test_no_applicable_binding_preserves_the_ordinary_workspace_payload(dependent_ticket, monkeypatch):
    scenario, h1 = dependent_ticket
    effect = workspace_intent(scenario, monkeypatch).effect
    assert effect.before_sha == h1.sha and effect.base == h1
    assert "prebuilt_workspace" not in effect.payload
