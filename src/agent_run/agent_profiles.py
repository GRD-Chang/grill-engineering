from __future__ import annotations

import json
import os
import sys
import tempfile
import uuid
from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping


ROLE_NAMES = ("development", "review", "publication")
REASONING_EFFORTS = frozenset(
    {"minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
)
DEFAULT_PRESET = "economy"
PRESETS: dict[str, dict[str, dict[str, str]]] = {
    "economy": {
        "development": {"model": "gpt-5.6-luna", "reasoning_effort": "max"},
        "review": {"model": "gpt-5.6-sol", "reasoning_effort": "high"},
    },
    "premium": {
        "development": {"model": "gpt-5.6-sol", "reasoning_effort": "medium"},
        "review": {"model": "gpt-5.6-sol", "reasoning_effort": "high"},
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
            f"unknown Agent Execution Preset {preset!r}; choose one of: {available}"
        )
    for key, value in (overrides or {}).items():
        if value is None:
            continue
        if key == "publication_from_development":
            if not isinstance(value, bool):
                raise AgentProfileError(
                    "publication_from_development must be a boolean"
                )
            continue
        if key.endswith("_model"):
            if not isinstance(value, str) or not value.strip():
                raise AgentProfileError(f"{key} must be a non-empty model identifier")
        elif key.endswith("_effort"):
            if not isinstance(value, str) or value not in REASONING_EFFORTS:
                available = ", ".join(sorted(REASONING_EFFORTS))
                raise AgentProfileError(
                    f"{key} must be one of: {available}"
                )
        else:
            raise AgentProfileError(f"unknown Agent Execution Profile option: {key}")
    supplied = overrides or {}
    if supplied.get("publication_from_development") is True and any(
        supplied.get(key) is not None
        for key in ("publication_model", "publication_effort")
    ):
        raise AgentProfileError(
            "publication_from_development cannot be combined with Publication overrides"
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
    roles["publication"] = {
        "model": roles["development"]["model"],
        "reasoning_effort": roles["development"]["reasoning_effort"],
        "reference": "development",
    }

    if current is not None and preset is None:
        current_profiles = current.get("profiles")
        if not isinstance(current_profiles, Mapping):
            raise AgentProfileError("current Agent Profile Revision is malformed")
        for role in ROLE_NAMES:
            existing = current_profiles.get(role)
            if not isinstance(existing, Mapping):
                raise AgentProfileError(f"current {role} profile is malformed")
            model = existing.get("model")
            effort = existing.get("reasoning_effort")
            if not isinstance(model, str) or not model:
                raise AgentProfileError(f"current {role} model is malformed")
            if not isinstance(effort, str) or effort not in REASONING_EFFORTS:
                raise AgentProfileError(f"current {role} reasoning effort is malformed")
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

    publication_from_development = supplied.get("publication_from_development") is True
    for role in ("development", "review", "publication"):
        model_key = f"{role}_model"
        effort_key = f"{role}_effort"
        if isinstance(supplied.get(model_key), str):
            roles[role]["model"] = str(supplied[model_key]).strip()
        if isinstance(supplied.get(effort_key), str):
            roles[role]["reasoning_effort"] = str(supplied[effort_key])

    publication_overridden = any(
        supplied.get(key) is not None
        for key in ("publication_model", "publication_effort")
    )
    if publication_from_development or (
        not publication_overridden
        and (preset is not None or roles["publication"].get("reference") == "development")
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
        roles[role]["provenance"] = {
            "preset": baseline_name,
            "overrides": list(dict.fromkeys([*prior_overrides, *override_fields])),
            "reference": roles[role].get("reference"),
        }
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
                        "Agent Execution Profile already exists; use configure to create a new Revision"
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
            raise AgentProfileError("configure requires a preset or an explicit role override")
        with self._locked(run_id):
            current = self._load_unlocked(run_id)
            if current is None:
                raise AgentProfileError(
                    f"unknown Agent Execution Profile for Run {run_id}"
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
                raise AgentProfileError("Agent Profile revisions are malformed")
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
            raise AgentProfileError(f"Invalid Agent Profile document: {path}")
        return value

    def _require_document(self, run_id: str) -> dict[str, Any]:
        document = self._load_unlocked(run_id)
        if document is None:
            raise AgentProfileError(f"unknown Agent Execution Profile for Run {run_id}")
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
        print(
            "Agent Execution Binding: "
            f"role={role} "
            f"thread={'resume' if thread_id is not None else 'new'} "
            f"model={binding['model']} "
            f"reasoning_effort={binding['reasoning_effort']} "
            f"profile_revision={binding['profile_revision']}",
            file=sys.stderr,
            flush=True,
        )
        original_event = request.get("_invocation_event")
        event = original_event if callable(original_event) else None

        def notify(kind: str, **facts: object) -> None:
            if kind == "started":
                facts.update(
                    {
                        "binding_id": binding["binding_id"],
                        "binding_role": binding["role"],
                        "profile_role": binding["role"],
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

        request["_invocation_event"] = notify
        try:
            return operation(request)
        finally:
            request["_invocation_event"] = original_event


def _current_preset(current: Mapping[str, Any] | None) -> str | None:
    if current is None:
        return None
    value = current.get("preset")
    return value if isinstance(value, str) and value in PRESETS else None


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
