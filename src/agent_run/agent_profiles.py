from __future__ import annotations

import json
import os
import tempfile
import uuid
from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

from agent_run.messages import error_message
from typing import Any, Callable, Iterator, Mapping

from agent_run.execution_binding import emit_execution_binding


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


class AgentProfileError(ValueError):
    """A profile is malformed or cannot be applied to a Delivery Run."""


ProfileOverrides = Mapping[str, str | bool | None]


def profile_options_present(options: ProfileOverrides | None) -> bool:
    if not options:
        return False
    return any(value is not None and value is not False for value in options.values())


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


class AgentProfileStore:
    """Run-scoped profile control plane with independent locks and writes."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.directory = root / "profiles"

    def initialize(
        self,
        run_id: str,
        *,
        preset: str | None = None,
        overrides: ProfileOverrides | None = None,
    ) -> dict[str, Any]:
        validate_profile_options(preset=preset, overrides=overrides)
        with self._locked(run_id):
            current = self._load_unlocked(run_id)
            if current is not None:
                if not profile_options_present(overrides) and preset is None:
                    return current
                requested = resolve_profiles(
                    preset=preset,
                    current=current if preset is None else None,
                    overrides=overrides,
                )
                if (
                    current.get("preset") == requested["preset"]
                    and current.get("profiles") == requested["profiles"]
                ):
                    return current
                if preset is not None or profile_options_present(overrides):
                    raise AgentProfileError(
                        error_message('profile.error.already_exists', audit="Agent Execution Profile already exists; use configure to create a new Revision")
                    )
            snapshot = self._new_document(
                run_id,
                resolve_profiles(
                    preset=preset or DEFAULT_PRESET, overrides=overrides
                ),
            )
            self._write_unlocked(run_id, snapshot)
            return snapshot

    def configure(
        self,
        run_id: str,
        *,
        preset: str | None = None,
        overrides: ProfileOverrides | None = None,
    ) -> dict[str, Any]:
        validate_profile_options(preset=preset, overrides=overrides)
        if preset is None and not profile_options_present(overrides):
            raise AgentProfileError(error_message('profile.error.option_required', audit="configure requires a preset or an explicit role override"))
        with self._locked(run_id):
            current = self._load_unlocked(run_id)
            if current is None:
                raise AgentProfileError(
                    error_message('profile.error.run_profile_missing', audit=f"unknown Agent Execution Profile for Run {run_id}", run_id=run_id)
                )
            resolved = resolve_profiles(
                preset=preset,
                current=current,
                overrides=overrides,
            )
            revision = int(current.get("profile_revision", 0)) + 1
            entry = {
                "profile_revision": revision,
                "preset": resolved["preset"],
                "profiles": resolved["profiles"],
                "updated_at": _now(),
            }
            revisions = current.get("revisions", [])
            if not isinstance(revisions, list):
                raise AgentProfileError(error_message('profile.error.revisions', audit="Agent Profile revisions are malformed"))
            document = dict(current)
            document.update(entry)
            document["revisions"] = [*revisions, deepcopy(entry)]
            self._write_unlocked(run_id, document)
            return document

    def load(self, run_id: str) -> dict[str, Any] | None:
        return self._load_unlocked(run_id)

    def bind_thread(
        self, run_id: str, *, role: str, thread_id: str | None
    ) -> dict[str, Any]:
        if role not in ROLE_NAMES:
            raise AgentProfileError(f"unknown top-level Agent role: {role}")
        if thread_id is not None and not thread_id.strip():
            raise AgentProfileError("Thread Execution Binding has an empty Thread ID")
        with self._locked(run_id):
            document = self._require_document(run_id)
            bindings = document.get("bindings", [])
            if not isinstance(bindings, list) or not all(
                isinstance(item, dict) for item in bindings
            ):
                raise AgentProfileError("Thread Execution Bindings are malformed")
            fresh_thread = False
            if thread_id is not None:
                for binding in bindings:
                    if binding.get("thread_id") == thread_id:
                        profile = _profile_for_role(document, role)
                        if not (
                            role == "publication"
                            and binding.get("role") != "publication"
                            and profile.get("reference") is None
                        ):
                            return dict(binding)
                        # An explicitly independent Publication profile cannot
                        # silently inherit the Development Thread.  The
                        # publication operation will therefore create a new
                        # top-level Thread and attach it below.
                        fresh_thread = True
                        break
            profile = _profile_for_role(document, role)
            binding = {
                "binding_id": uuid.uuid4().hex,
                "role": role,
                "thread_id": None if fresh_thread else thread_id,
                "model": profile["model"],
                "reasoning_effort": profile["reasoning_effort"],
                "profile_revision": int(document["profile_revision"]),
                "created_at": _now(),
            }
            bindings.append(binding)
            document["bindings"] = bindings
            self._write_unlocked(run_id, document)
            return dict(binding)

    def attach_thread(
        self, run_id: str, binding_id: str, thread_id: str
    ) -> dict[str, Any]:
        if not thread_id.strip():
            raise AgentProfileError("Thread Execution Binding has an empty Thread ID")
        with self._locked(run_id):
            document = self._require_document(run_id)
            bindings = _bindings(document)
            existing = next(
                (binding for binding in bindings if binding.get("binding_id") == binding_id),
                None,
            )
            if existing is None:
                raise AgentProfileError("Thread Execution Binding no longer exists")
            for binding in bindings:
                if binding.get("thread_id") == thread_id and binding is not existing:
                    if _same_binding_facts(existing, binding):
                        bindings.remove(existing)
                        self._write_unlocked(run_id, document)
                        return dict(binding)
                    bindings.remove(existing)
                    self._write_unlocked(run_id, document)
                    raise AgentProfileError(
                        "Thread ID is already bound to another profile"
                    )
            existing["thread_id"] = thread_id
            self._write_unlocked(run_id, document)
            return dict(existing)

    def discard_pending(self, run_id: str, binding_id: str) -> None:
        with self._locked(run_id):
            document = self._require_document(run_id)
            bindings = _bindings(document)
            kept = [
                binding
                for binding in bindings
                if not (
                    binding.get("binding_id") == binding_id
                    and binding.get("thread_id") is None
                )
            ]
            if len(kept) != len(bindings):
                document["bindings"] = kept
                self._write_unlocked(run_id, document)

    @contextmanager
    def _locked(self, run_id: str) -> Iterator[None]:
        self._validate_run_id(run_id)
        self.directory.mkdir(parents=True, exist_ok=True)
        lock_path = self.directory / f".{run_id}.lock"
        with lock_path.open("a+", encoding="utf-8") as lock_file:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _load_unlocked(self, run_id: str) -> dict[str, Any] | None:
        self._validate_run_id(run_id)
        path = self.directory / f"{run_id}.json"
        if not path.exists():
            return None
        value: object = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise AgentProfileError(error_message('profile.error.invalid_document', audit=f"Invalid Agent Profile document: {path}", path=path))
        return value

    def _require_document(self, run_id: str) -> dict[str, Any]:
        document = self._load_unlocked(run_id)
        if document is None:
            raise AgentProfileError(error_message('profile.error.run_profile_missing', audit=f"unknown Agent Execution Profile for Run {run_id}", run_id=run_id))
        return document

    def _write_unlocked(self, run_id: str, document: Mapping[str, Any]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        destination = self.directory / f"{run_id}.json"
        descriptor, temporary_name = tempfile.mkstemp(
            dir=self.directory,
            prefix=f".{run_id}.",
            suffix=".tmp",
            text=True,
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as temporary_file:
                json.dump(
                    document,
                    temporary_file,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                temporary_file.write("\n")
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            os.replace(temporary_path, destination)
            directory_descriptor = os.open(self.directory, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        except BaseException:
            temporary_path.unlink(missing_ok=True)
            raise

    @staticmethod
    def _validate_run_id(run_id: str) -> None:
        if not run_id or Path(run_id).name != run_id:
            raise AgentProfileError("Run ID is invalid")

    @staticmethod
    def _new_document(run_id: str, resolved: Mapping[str, Any]) -> dict[str, Any]:
        entry = {
            "profile_revision": 1,
            "preset": resolved["preset"],
            "profiles": deepcopy(resolved["profiles"]),
            "updated_at": _now(),
        }
        return {
            "run_id": run_id,
            **entry,
            "revisions": [deepcopy(entry)],
            "bindings": [],
        }


class ProfiledAgentBackend:
    """Attach a Run's Thread Execution Binding around any AgentBackend."""

    def __init__(
        self,
        backend: Any,
        profiles: AgentProfileStore,
        *,
        run_id: str | None = None,
    ) -> None:
        self.backend = backend
        self.profiles = profiles
        self.run_id = run_id
        self.execution_recovery_guard: Callable[[], None] | None = None

    def set_run_id(self, run_id: str) -> None:
        self.run_id = run_id

    def develop(self, request: dict[str, Any]) -> Any:
        return self._invoke("development", request, self.backend.develop)

    def review(self, request: dict[str, Any]) -> Any:
        return self._invoke("review", request, self.backend.review)

    def publication(self, request: dict[str, Any]) -> Any:
        return self._invoke("publication", request, self.backend.publication)

    def run_publication(self, request: dict[str, Any]) -> Any:
        return self._invoke("publication", request, self.backend.run_publication)

    def _invoke(
        self,
        role: str,
        request: dict[str, Any],
        operation: Callable[[dict[str, Any]], Any],
    ) -> Any:
        run_id = request.get("run_id") or self.run_id
        if not isinstance(run_id, str) or not run_id:
            raise AgentProfileError("top-level Agent request is missing its Run ID")
        thread_id = request.get("thread_id")
        if thread_id is not None and not isinstance(thread_id, str):
            raise AgentProfileError("top-level Agent request has an invalid Thread ID")
        binding = self.profiles.bind_thread(run_id, role=role, thread_id=thread_id)
        if thread_id is not None and binding.get("thread_id") is None:
            request["thread_id"] = None
            request["_invocation_mode"] = "fresh"
            thread_id = None
        request["_execution_binding"] = dict(binding)
        if not getattr(self.backend, "emits_execution_binding", False):
            emit_execution_binding(
                role=role,
                thread_id=thread_id,
                model=binding["model"],
                reasoning_effort=binding["reasoning_effort"],
                profile_revision=binding["profile_revision"],
            )
        original_event = request.get("_invocation_event")
        original_execution_role = request.get("_execution_role")
        event = original_event if callable(original_event) else None

        def notify(kind: str, **facts: object) -> None:
            if kind == "started":
                facts.update(
                    {
                        "binding_id": binding["binding_id"],
                        "binding_role": binding["role"],
                        "profile_role": binding["role"],
                        "invocation_role": role,
                        "model": binding["model"],
                        "reasoning_effort": binding["reasoning_effort"],
                        "profile_revision": binding["profile_revision"],
                        "thread_execution_binding": dict(binding),
                    }
                )
            elif kind == "thread_started":
                reported = facts.get("reported_thread_id")
                if isinstance(reported, str):
                    attached = self.profiles.attach_thread(
                        run_id, str(binding["binding_id"]), reported
                    )
                    binding.clear()
                    binding.update(attached)
                    facts.update(
                        {
                            "binding_id": binding["binding_id"],
                            "binding_role": binding["role"],
                            "profile_role": binding["role"],
                            "invocation_role": role,
                            "model": binding["model"],
                            "reasoning_effort": binding["reasoning_effort"],
                            "profile_revision": binding["profile_revision"],
                        }
                    )
                    facts["thread_execution_binding"] = dict(binding)
            elif kind == "failed" and binding.get("thread_id") is None:
                self.profiles.discard_pending(run_id, str(binding["binding_id"]))
            if event is not None:
                event(kind, **facts)

        deadline_seconds = getattr(event, "deadline_seconds", None)
        if deadline_seconds is not None:
            setattr(notify, "deadline_seconds", deadline_seconds)
        recovery_state = getattr(event, "recovery_state", None)
        if recovery_state is not None:
            setattr(notify, "recovery_state", recovery_state)
        can_recover = getattr(event, "recovery_allowed", None)

        def recovery_allowed() -> bool:
            guard = self.execution_recovery_guard
            if guard is None or not callable(can_recover):
                return False
            guard()
            return bool(can_recover())

        setattr(notify, "recovery_allowed", recovery_allowed)
        request["_invocation_event"] = notify
        request["_execution_role"] = role
        try:
            return operation(request)
        finally:
            request["_invocation_event"] = original_event
            if original_execution_role is None:
                request.pop("_execution_role", None)
            else:
                request["_execution_role"] = original_execution_role


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


def _profile_for_role(document: Mapping[str, Any], role: str) -> Mapping[str, Any]:
    profiles = document.get("profiles")
    if not isinstance(profiles, Mapping):
        raise AgentProfileError("Agent Profiles are malformed")
    profile = profiles.get(role)
    if not isinstance(profile, Mapping):
        raise AgentProfileError(f"Agent {role} Profile is missing")
    model = profile.get("model")
    effort = profile.get("reasoning_effort")
    if not isinstance(model, str) or not model:
        raise AgentProfileError(f"Agent {role} Profile has an invalid model")
    if not isinstance(effort, str) or effort not in REASONING_EFFORTS:
        raise AgentProfileError(f"Agent {role} Profile has an invalid reasoning effort")
    return profile


def _bindings(document: dict[str, Any]) -> list[dict[str, Any]]:
    bindings = document.get("bindings")
    if not isinstance(bindings, list) or not all(
        isinstance(item, dict) for item in bindings
    ):
        raise AgentProfileError("Thread Execution Bindings are malformed")
    return bindings


def _same_binding_facts(
    left: Mapping[str, Any], right: Mapping[str, Any]
) -> bool:
    return all(
        left.get(field) == right.get(field)
        for field in ("role", "model", "reasoning_effort", "profile_revision")
    )


def _now() -> str:
    return datetime.now(UTC).isoformat()
