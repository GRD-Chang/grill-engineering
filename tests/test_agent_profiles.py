from __future__ import annotations


from agent_run.contracts.agent_profiles import resolve_profiles


def test_preset_resolution_respects_publication_defaults_and_overrides() -> None:
    economy = resolve_profiles(preset="economy")
    assert economy["profiles"]["development"] == {
        "model": "gpt-5.6-luna",
        "reasoning_effort": "xhigh",
        "reference": None,
        "provenance": {
            "preset": "economy",
            "overrides": [],
            "reference": None,
        },
    }
    assert economy["profiles"]["publication"]["reference"] == "development"
    assert economy["profiles"]["publication"]["model"] == "gpt-5.6-luna"
    assert economy["profiles"]["publication"]["reasoning_effort"] == "xhigh"
    assert economy["profiles"]["review"]["model"] == "gpt-6-astra"
    assert economy["profiles"]["review"]["reasoning_effort"] == "low"

    defaults = resolve_profiles(preset="premium")["profiles"]
    for role in ("development", "review"):
        assert defaults[role]["model"] == "gpt-6-astra"
        assert defaults[role]["reasoning_effort"] == "low"
    assert defaults["publication"]["model"] == "gpt-5.6-luna"
    assert defaults["publication"]["reasoning_effort"] == "xhigh"
    assert defaults["publication"]["reference"] is None

    premium = resolve_profiles(
        preset="premium",
        overrides={
            "development_model": "custom-development",
            "development_effort": "high",
        },
    )
    assert premium["profiles"]["development"]["model"] == "custom-development"
    assert premium["profiles"]["development"]["reasoning_effort"] == "high"
    assert premium["profiles"]["review"]["model"] == "gpt-6-astra"
    assert premium["profiles"]["publication"] == defaults["publication"]
