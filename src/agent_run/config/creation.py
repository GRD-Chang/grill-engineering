"""Resolve creation inputs once through the existing validated configuration leaves."""


from dataclasses import replace
from typing import Any, Mapping

from agent_run.contracts.agent_profiles import ProfileOverrides, resolve_profiles
from agent_run.contracts.policy_values import resolve_delivery_policy
from agent_run.adapters.auth.identity import resolve_runner_identity, resolve_worker_identity
from agent_run.config.resources import resolve_resources
from agent_run.contracts.prompt import validate_resources
from agent_run.config.defaults import UserDefaultsStore, notification_snapshot

from agent_run.model import CreationInputs, Policy, Profile, RunSpec




def freeze_creation(spec: RunSpec, defaults: UserDefaultsStore, *, preset: str | None = None,
                    profile_overrides: ProfileOverrides | None = None,
                    policy_overrides: Mapping[str, Any] | None = None,
                    identity_overrides: Mapping[str, Any] | None = None,
                    prompt_resources: Mapping[str, str] | None = None,
                    disable_notifications: bool = False, notification_mode: str | None = None) -> RunSpec:
    """Return complete creation values without creating a Run or reading secrets."""
    document = defaults.load()
    language = defaults.language(document)
    selected, overrides = defaults.resolve_creation(preset=preset, overrides=profile_overrides, document=document)
    profiles = resolve_profiles(preset=selected, overrides=overrides)
    policy = resolve_delivery_policy(user_defaults=document.get("policy"), command_overrides=policy_overrides)
    resources = dict(prompt_resources) if prompt_resources is not None else resolve_resources(language)
    validate_resources(resources)
    identities = document.get("github", {})
    inputs = CreationInputs(
        profiles, resources,
        resolve_worker_identity(identities, identity_overrides, app_profile_path=defaults.path.with_name("github-app.json")),
        resolve_runner_identity(identities, identity_overrides),
        notification_snapshot(document.get("notifications"), disabled=disable_notifications, mode=notification_mode),
        {"source": "creation", "defaults_path": str(defaults.path), "defaults": document,
         "preset": preset, "profile_overrides": dict(profile_overrides or {}),
         "policy_overrides": dict(policy_overrides or {}), "identity_overrides": dict(identity_overrides or {}),
         "disable_notifications": disable_notifications, "notification_mode": notification_mode},
    )
    roles = profiles["profiles"]
    return replace(spec, language=language, inputs=inputs, policy=replace(policy,
        development_profile=Profile(roles["development"]["model"], roles["development"]["reasoning_effort"]),
        review_profile=Profile(roles["review"]["model"], roles["review"]["reasoning_effort"]),
        publication_profile=Profile(roles["publication"]["model"], roles["publication"]["reasoning_effort"]),
    ))
