from __future__ import annotations

from agent_run.errors import AgentProfileError


from copy import deepcopy

from agent_run.errors import error_message
from typing import Any, Mapping


ROLE_NAMES = ("development", "review", "publication")
REASONING_EFFORTS = frozenset(
    {"minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
)
DEFAULT_PRESET = "economy"
PRESETS: dict[str, dict[str, dict[str, str]]] = {
    "economy": {
        "development": {"model": "gpt-5.6-luna", "reasoning_effort": "xhigh"},
        "review": {"model": "gpt-6-astra", "reasoning_effort": "low"},
    },
    "premium": {
        "development": {"model": "gpt-6-astra", "reasoning_effort": "low"},
        "review": {"model": "gpt-6-astra", "reasoning_effort": "low"},
        "publication": {"model": "gpt-5.6-luna", "reasoning_effort": "xhigh"},
    },
}




ProfileOverrides = Mapping[str, str | bool | None]


def validate_profile_options(
    *, preset: str | None = None, overrides: ProfileOverrides | None = None
) -> None:
    if preset is not None and preset not in PRESETS:
        available = ", ".join(sorted(PRESETS))
        raise AgentProfileError(
            error_message('profile.error.unknown_preset', audit=f"unknown Agent Execution Preset {preset!r}; choose one of: {available}", preset=preset, available=available)
        )
    for key, value in (overrides or {}).items():
        if value is None:
            continue
        if key == "publication_from_development":
            if not isinstance(value, bool):
                raise AgentProfileError(
                    error_message('profile.error.reference_boolean', audit="publication_from_development must be a boolean")
                )
            continue
        if key.endswith("_model"):
            if not isinstance(value, str) or not value.strip():
                raise AgentProfileError(error_message('profile.error.model_identifier', audit=f"{key} must be a non-empty model identifier", field=key))
        elif key.endswith("_effort"):
            if not isinstance(value, str) or value not in REASONING_EFFORTS:
                available = ", ".join(sorted(REASONING_EFFORTS))
                raise AgentProfileError(
                    error_message('profile.error.effort_choice', audit=f"{key} must be one of: {available}", field=key, available=available)
                )
        else:
            raise AgentProfileError(error_message('profile.error.unknown_option', audit=f"unknown Agent Execution Profile option: {key}", field=key))
    supplied = overrides or {}
    if supplied.get("publication_from_development") is True and any(
        supplied.get(key) is not None
        for key in ("publication_model", "publication_effort")
    ):
        raise AgentProfileError(
            error_message('profile.error.reference_conflict', audit="publication_from_development cannot be combined with Publication overrides")
        )


def resolve_profiles(
    *,
    preset: str | None = None,
    current: Mapping[str, Any] | None = None,
    overrides: ProfileOverrides | None = None,
) -> dict[str, Any]:
    """Resolve one compact, exact profile snapshot.

    ``current`` is used for an incremental configuration update.  A supplied
    preset starts a new preset baseline; otherwise the current resolved values
    are retained and only explicitly supplied role values change.
    """

    validate_profile_options(preset=preset, overrides=overrides)
    supplied = dict(overrides or {})
    publication_from_development = supplied.get("publication_from_development") is True
    publication_overridden = any(
        supplied.get(key) is not None
        for key in ("publication_model", "publication_effort")
    )
    baseline_name = preset or _current_preset(current) or DEFAULT_PRESET
    baseline = deepcopy(PRESETS[baseline_name])
    roles: dict[str, dict[str, Any]] = {
        role: {
            "model": values["model"],
            "reasoning_effort": values["reasoning_effort"],
            "reference": None,
        }
        for role, values in baseline.items()
    }
    roles.setdefault(
        "publication",
        {
            "model": roles["development"]["model"],
            "reasoning_effort": roles["development"]["reasoning_effort"],
            "reference": "development",
        },
    )

    current_profiles: Mapping[str, Any] = {}
    preserve_independent_publication_provenance = False
    publication_inherited_overrides: list[str] = []
    if current is not None:
        loaded_profiles = current.get("profiles")
        if not isinstance(loaded_profiles, Mapping):
            raise AgentProfileError(error_message('profile.error.current_revision', audit="current Agent Profile Revision is malformed"))
        current_profiles = loaded_profiles
    if current is not None and preset is None:
        for role in ROLE_NAMES:
            existing = current_profiles.get(role)
            if not isinstance(existing, Mapping):
                raise AgentProfileError(error_message('profile.error.current_profile', audit=f"current {role} profile is malformed", role=role))
            model = existing.get("model")
            effort = existing.get("reasoning_effort")
            if not isinstance(model, str) or not model:
                raise AgentProfileError(error_message('profile.error.current_model', audit=f"current {role} model is malformed", role=role))
            if not isinstance(effort, str) or effort not in REASONING_EFFORTS:
                raise AgentProfileError(error_message('profile.error.current_effort', audit=f"current {role} reasoning effort is malformed", role=role))
            if role == "publication" and existing.get("reference") is None:
                preserve_independent_publication_provenance = True
            if role == "publication" and existing.get("reference") == "development":
                development = current_profiles.get("development")
                if isinstance(development, Mapping):
                    publication_inherited_overrides = _provenance_overrides(
                        development.get("provenance")
                    )
            roles[role] = {
                "model": model,
                "reasoning_effort": effort,
                "reference": existing.get("reference")
                if existing.get("reference") in {None, "development"}
                else None,
                "provenance": deepcopy(existing.get("provenance"))
                if isinstance(existing.get("provenance"), Mapping)
                else {
                    "preset": _current_preset(current) or DEFAULT_PRESET,
                    "overrides": [],
                    "reference": existing.get("reference"),
                },
            }
    elif current is not None and not publication_from_development:
        existing = current_profiles.get("publication")
        if not isinstance(existing, Mapping):
            raise AgentProfileError(error_message('profile.error.publication_profile', audit="current publication profile is malformed"))
        if existing.get("reference") is None:
            preserve_independent_publication_provenance = True
            model = existing.get("model")
            effort = existing.get("reasoning_effort")
            if not isinstance(model, str) or not model:
                raise AgentProfileError(error_message('profile.error.publication_model', audit="current publication model is malformed"))
            if not isinstance(effort, str) or effort not in REASONING_EFFORTS:
                raise AgentProfileError(
                    error_message('profile.error.publication_effort', audit="current publication reasoning effort is malformed")
                )
            roles["publication"] = {
                "model": model,
                "reasoning_effort": effort,
                "reference": None,
                "provenance": deepcopy(existing.get("provenance"))
                if isinstance(existing.get("provenance"), Mapping)
                else {
                    "preset": _current_preset(current) or DEFAULT_PRESET,
                    "overrides": [],
                    "reference": None,
                },
            }

    for role in ("development", "review", "publication"):
        model_key = f"{role}_model"
        effort_key = f"{role}_effort"
        if isinstance(supplied.get(model_key), str):
            roles[role]["model"] = str(supplied[model_key]).strip()
        if isinstance(supplied.get(effort_key), str):
            roles[role]["reasoning_effort"] = str(supplied[effort_key])

    if publication_from_development or (
        not publication_overridden
        and roles["publication"].get("reference") == "development"
    ):
        roles["publication"]["reference"] = "development"
        roles["publication"]["model"] = roles["development"]["model"]
        roles["publication"]["reasoning_effort"] = roles["development"][
            "reasoning_effort"
        ]
    elif publication_overridden:
        roles["publication"]["reference"] = None

    for role in ROLE_NAMES:
        override_fields = [
            field
            for field in ("model", "effort")
            if supplied.get(f"{role}_{field}") is not None
        ]
        prior_provenance = roles[role].get("provenance")
        prior_overrides = (
            prior_provenance.get("overrides", [])
            if isinstance(prior_provenance, Mapping)
            else []
        )
        if not isinstance(prior_overrides, list):
            prior_overrides = []
        provenance_preset = baseline_name
        inherited_overrides: list[str] = []
        if role == "publication" and roles[role].get("reference") == "development":
            # A restored/reference Publication has no independent override history;
            # its provenance is the Development profile it currently follows.
            prior_overrides = []
        elif role == "publication":
            inherited = _publication_inherited_overrides(prior_provenance)
            inherited_overrides = publication_inherited_overrides or inherited
            inherited_overrides = [
                field for field in inherited_overrides if field not in override_fields
            ]
        if (
            role == "publication"
            and preserve_independent_publication_provenance
            and isinstance(prior_provenance, Mapping)
            and isinstance(prior_provenance.get("preset"), str)
        ):
            provenance_preset = str(prior_provenance["preset"])
        provenance: dict[str, Any] = {
            "preset": provenance_preset,
            "overrides": list(dict.fromkeys([*prior_overrides, *override_fields])),
            "reference": roles[role].get("reference"),
        }
        if role == "publication" and inherited_overrides:
            provenance["inherited"] = {
                "role": "development",
                "overrides": list(dict.fromkeys(inherited_overrides)),
            }
        roles[role]["provenance"] = provenance
    return {
        "preset": baseline_name,
        "profiles": roles,
    }


def _current_preset(current: Mapping[str, Any] | None) -> str | None:
    if current is None:
        return None
    value = current.get("preset")
    return value if isinstance(value, str) and value in PRESETS else None


def _provenance_overrides(value: object) -> list[str]:
    if not isinstance(value, Mapping):
        return []
    overrides = value.get("overrides")
    if not isinstance(overrides, list):
        return []
    return [field for field in overrides if field in {"model", "effort"}]


def _publication_inherited_overrides(value: object) -> list[str]:
    if not isinstance(value, Mapping):
        return []
    inherited = value.get("inherited")
    if not isinstance(inherited, Mapping) or inherited.get("role") != "development":
        return []
    return _provenance_overrides(inherited)
