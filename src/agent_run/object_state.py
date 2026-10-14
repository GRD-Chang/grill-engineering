"""StateStore bridge for current-object commits and indexed related history."""
from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from typing import Any, cast

from agent_run.state import ConcurrentRunStateError, StateStore, _append_timeline_event, _sanitize_durable_errors
from agent_run.object_projection import ObjectProjection, ProjectionBaseline, ProjectionKey
from agent_run.sqlite_encoding import copy_json_tree


_MISSING = object()

_ARCHIVE_FIELDS = frozenset({
    "retired_job_generations", "retired_parent_job", "retired_run_acceptance", "retired_run_publication",
})
_KEYED_TICKET_FIELDS = frozenset({
    "ticket_jobs", "ticket_agent_invocations", "retired_ticket_generations", "pending_ticket_retirements",
})


_PARENT_EXCLUDED_FIELDS = frozenset({
    "ticket_jobs", "ticket_agent_invocations", "ticket_pause_requests",
    "retired_ticket_generations", "pending_ticket_retirements",
    "retired_parent_job", "retired_run_acceptance", "retired_run_publication",
})


def _merge_change(old: Any, new: Any, latest: Any, owner: str) -> Any:
    if old == new:
        return deepcopy(latest)
    if isinstance(old, dict) and isinstance(new, dict) and isinstance(latest, dict):
        if owner in {"parent_job", "run_acceptance", "run_publication", "active_agent_invocation"} or (
            owner.startswith("ticket_jobs.") and owner.count(".") == 1
        ):
            if old != latest and new != latest:
                raise ConcurrentRunStateError(f"Owner changed before commit: {owner}")
        result = deepcopy(latest)
        for key in old.keys() | new.keys():
            if old.get(key, _MISSING) == new.get(key, _MISSING):
                continue
            if key not in new:
                if key in latest and latest[key] != old.get(key, _MISSING):
                    raise ConcurrentRunStateError(f"Object changed before commit: {owner}.{key}")
                result.pop(key, None)
            else:
                result[key] = _merge_change(old.get(key, _MISSING), new[key], latest.get(key, _MISSING), f"{owner}.{key}")
        return result
    if owner == "agent_invocation_history" and isinstance(old, list) and isinstance(new, list) and isinstance(latest, list):
        from agent_run.sqlite_objects import invocation_identity
        previous = {invocation_identity(item): item for item in old}
        result = {invocation_identity(item): deepcopy(item) for item in latest}
        for item in new:
            identity = invocation_identity(item)
            if item != previous.get(identity):
                result[identity] = _merge_change(previous.get(identity), item, result.get(identity), "invocation:" + identity)
        return list(result.values())
    if latest != old and latest != new:
        raise ConcurrentRunStateError(f"Object changed before commit: {owner}")
    return deepcopy(new)


def _sync_current_facts(target: dict[str, Any], source: dict[str, Any], *, excluded: set[str]) -> None:
    """Publish committed facts without invalidating a stage's owner references."""
    for key in target.keys() - source.keys() - excluded:
        target.pop(key)
    for key, value in source.items():
        if key in excluded:
            continue
        current = target.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            _sync_current_facts(current, value, excluded=set())
        elif isinstance(current, list) and isinstance(value, list):
            current[:] = deepcopy(value)
        else:
            target[key] = deepcopy(value)


def _comparison_facts(state: dict[str, Any]) -> dict[str, Any]:
    """Compare the same current Invocation relationship after owner retirement."""
    from agent_run.sqlite_objects import invocation_identity
    identities = {
        invocation_identity(item) for item in [
            state.get("active_agent_invocation"),
            *state.get("ticket_agent_invocations", {}).values(),
        ] if isinstance(item, dict)
    }
    resumes = {item.get("resume_id") for item in state.get("resume_audit", {}).get("history", [])
               if isinstance(item, dict)} - {None}
    return {**state, "agent_invocation_history": [
        item for item in state.get("agent_invocation_history", [])
        if invocation_identity(item) in identities or item.get("resume_id") in resumes
    ]}


class ObjectStateStore:
    """Use the existing authorization, FIFO and notification boundaries.

    A caller's projection baseline determines its changed top-level owners;
    unrelated committed owners and archived Invocations are never overwritten.
    """
    def __init__(self, store: StateStore, *, parent_scope: bool = False) -> None:
        self._store = store
        self._parent_scope = parent_scope
        self._excluded_fields = set(_PARENT_EXCLUDED_FIELDS | _ARCHIVE_FIELDS) if parent_scope else set(_ARCHIVE_FIELDS)
        self._local = threading.local()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._store, name)

    @property
    def run_saved_observer(self) -> Any:
        return self._store.run_saved_observer

    @run_saved_observer.setter
    def run_saved_observer(self, value: Any) -> None:
        self._store.run_saved_observer = value

    def _baseline(self) -> dict[str, tuple[dict[str, Any], dict[str, int]]]:
        if not hasattr(self._local, "baselines"):
            self._local.baselines = {}
        return cast(dict[str, tuple[dict[str, Any], dict[str, int]]], self._local.baselines)

    def invalidate_baseline(self) -> None:
        self._baseline().clear()
        self._local.expected = {}

    def _projection_key(self, run_id: str) -> ProjectionKey:
        return str(self._store.root), run_id, frozenset(self._excluded_fields)

    def _record_baseline(
        self, run_id: str, state: dict[str, Any], positions: dict[str, int],
        *, projection: dict[str, Any] | None = None,
    ) -> None:
        record = ProjectionBaseline(copy_json_tree(dict(state)), dict(positions))
        baselines = self._baseline()
        baselines.clear()
        baselines[run_id] = (record.facts, record.history_positions)
        carrier = state if projection is None else projection
        if isinstance(carrier, ObjectProjection):
            carrier.record_baseline(self._projection_key(run_id), record)

    def load_run(self, run_id: str) -> dict[str, Any] | None:
        return self._load_objects(run_id)

    def load_scheduler_run(self, run_id: str) -> dict[str, Any] | None:
        """Read current Graph inputs at the explicit scheduling query boundary."""
        return self._load_objects(run_id, scheduler=True)

    def load_scheduler_current_run(self, run_id: str) -> dict[str, Any] | None:
        state = self.load_scheduler_run(run_id)
        if state is not None:
            self._store._validate_current_run(run_id, state)
        return state

    def load_current_run_for_ticket(self, run_id: str, number: int) -> dict[str, Any] | None:
        state = self._load_objects(run_id, ticket_numbers={number})
        if state is not None:
            self._store._validate_current_run(run_id, state)
        return state

    def _load_objects(
        self, run_id: str, *, scheduler: bool = False, ticket_numbers: set[int] | None = None,
    ) -> dict[str, Any] | None:
        self._store._reject_legacy_run(run_id)
        excluded = self._excluded_fields | (set(_KEYED_TICKET_FIELDS) if not self._parent_scope else set())
        loaded = self._store._repository.load_objects(
            run_id, excluded_fields=excluded, related_tickets=not self._parent_scope,
            ticket_numbers=ticket_numbers, scheduler_tickets=scheduler,
        )
        if loaded is None:
            return None
        facts, positions = loaded
        state = ObjectProjection(facts)
        self._record_baseline(run_id, state, positions)
        return state

    def load_full_run(self, run_id: str) -> dict[str, Any] | None:
        return self._store.load_current_run(run_id)

    def load_current_run(self, run_id: str) -> dict[str, Any] | None:
        state = self.load_run(run_id)
        if state is not None:
            if self._parent_scope:
                from agent_run.state_contract import require_current_run_state
                require_current_run_state(state, parent_scope=True)
            else:
                self._store._validate_current_run(run_id, state)
        return state

    def load_delivery_evidence(self, run_id: str) -> dict[str, Any]:
        self._store._reject_legacy_run(run_id)
        evidence = self._store._repository.load_fields(run_id, {"ticket_jobs"})
        if evidence is None:
            raise ValueError("Delivery Run disappeared at its evidence boundary")
        from agent_run.integration_record_contract import require_completed_ticket_integration_records
        require_completed_ticket_integration_records(evidence)
        return evidence

    def save_run(self, run_id: str, state: dict[str, Any]) -> None:
        try:
            self._store._commit_run(lambda: self._save_run_and_notify_unlocked(run_id, state))
        except BaseException:
            self.invalidate_baseline()
            if isinstance(state, ObjectProjection):
                state.invalidate_baseline(self._projection_key(run_id))
            raise

    def _save_run_and_notify_unlocked(self, run_id: str, state: dict[str, Any]) -> None:
        key = self._projection_key(run_id)
        if isinstance(state, ObjectProjection) and state.baseline_is_invalid(key):
            raise ValueError("Object save requires a fresh read after failed commit")
        record = state.baseline(key) if isinstance(state, ObjectProjection) else None
        baseline = (record.facts, record.history_positions) if record is not None else self._baseline().get(run_id)
        original = baseline[0] if baseline is not None else {}
        touched = {
            int(number) for field in _KEYED_TICKET_FIELDS
            for number in original.get(field, {}).keys() | state.get(field, {}).keys()
            if original.get(field, {}).get(number, _MISSING) != state.get(field, {}).get(number, _MISSING)
        }
        # Keep the caller's loaded relationship set coherent after readback.
        # In particular a Controller graph refresh must retain a known sibling
        # gate rather than later rebuilding that Job as an unstarted selector.
        touched.update(int(number) for number in original.get("ticket_jobs", {}))
        selected = state.get("active_ticket_job")
        if isinstance(selected, dict) and type(selected.get("ticket_number")) is int:
            touched.add(selected["ticket_number"])
        holder = state.get("ticket_integration_holder")
        if type(holder) is int:
            touched.add(holder)
        for field in ("ticket_work_queue", "ticket_integration_queue"):
            touched.update(item["ticket_number"] for item in state.get(field, [])
                           if isinstance(item, dict) and type(item.get("ticket_number")) is int)
        expected = getattr(self._local, "expected", {}).get(run_id)
        excluded = self._excluded_fields | (set(_KEYED_TICKET_FIELDS) if not self._parent_scope else set())
        loaded = self._store._repository.load_objects(
            run_id, excluded_fields=excluded, related_tickets=not self._parent_scope,
            ticket_numbers=touched, scheduler_tickets=expected is not None,
        )
        if loaded is None:
            # New Run creation remains a complete creation transaction.
            self._store._save_run_and_notify_unlocked(run_id, state)
            created = self._store._repository.load_objects(run_id, excluded_fields=self._excluded_fields)
            if created is not None:
                self._record_baseline(run_id, created[0], created[1], projection=state)
            return
        latest, positions = loaded
        if baseline is None:
            raise ValueError("Object save requires a loaded baseline or explicit save_owned")
        old = baseline[0]
        if (expected is None and not self._parent_scope
                and state.get("resume_audit", {}).get("total", 0)
                <= latest.get("resume_audit", {}).get("total", 0)):
            # A targeted Resume supersedes earlier supervision of that owner,
            # even when the old frame has no delta. Unchanged sibling Jobs do
            # not participate in this authorization fence.
            owners = {
                str(job["ticket_number"]) for job in (
                    old.get("active_ticket_job"), state.get("active_ticket_job"),
                ) if isinstance(job, dict) and type(job.get("ticket_number")) is int
            }
            owners.update(
                number for number in old.get("ticket_jobs", {}).keys() | state.get("ticket_jobs", {}).keys()
                if old.get("ticket_jobs", {}).get(number, _MISSING)
                != state.get("ticket_jobs", {}).get(number, _MISSING)
            )
            for number in owners:
                job = latest.get("ticket_jobs", {}).get(number, {})
                proposed = state.get("ticket_jobs", {}).get(number, {})
                if (job.get("last_ticket_resume")
                        and job.get("ticket_branch_generation") == proposed.get("ticket_branch_generation")
                        and job["last_ticket_resume"] != proposed.get("last_ticket_resume")):
                    raise ConcurrentRunStateError("Ticket Resume superseded the Run projection")
        if self._parent_scope:
            # Parent results also depend on unchanged requirements and the
            # current owner, even when this write only updates an Invocation.
            for field in ("parent_job", "run_acceptance", "run_publication"):
                if latest.get(field) != old.get(field) and latest.get(field) != state.get(field):
                    raise ConcurrentRunStateError(f"Parent owner changed before commit: {field}")
            for field in ("accepted_parent_spec_revision", "accepted_ticket_graph_revision"):
                if latest.get(field) != old.get(field):
                    raise ConcurrentRunStateError(f"Parent demand changed before commit: {field}")
            for field in ("parent", "ticket_graph"):
                if latest.get(field, {}).get("revision") != old.get(field, {}).get("revision"):
                    raise ConcurrentRunStateError(f"Parent demand changed before commit: {field}")
            if latest.get("status") in {"operator_stopped", "abandoned", "abandonment_pending", "completed", "execution_failed"}:
                if latest.get("status") != old.get("status") and latest.get("status") != state.get("status"):
                    raise ConcurrentRunStateError("Parent lost Run authorization before commit")
        fields = {key for key in (old.keys() | state.keys()) - self._excluded_fields
                  if old.get(key, _MISSING) != state.get(key, _MISSING)}
        expected = getattr(self._local, "expected", {}).get(run_id)
        if expected is not None and _comparison_facts(latest) != _comparison_facts(expected):
            raise ConcurrentRunStateError("Run changed during Controller refresh")
        merged = copy_json_tree(latest)
        for field in fields:
            if field in state:
                merged[field] = _merge_change(old.get(field, _MISSING), state[field], latest.get(field, _MISSING), field)
            else:
                if field in latest and latest[field] != old.get(field, _MISSING):
                    raise ConcurrentRunStateError(f"Object changed before commit: {field}")
                merged.pop(field, None)
        # Current selection is a view of the exact Ticket Invocation, as in
        # the original StateStore bridge; association commits stay atomic.
        active = merged.get("active_agent_invocation")
        job = merged.get("active_ticket_job")
        if isinstance(active, dict) and isinstance(job, dict) and active.get("work_subject") == f"ticket:{job.get('ticket_number')}":
            merged.setdefault("ticket_agent_invocations", {})[str(job["ticket_number"])] = deepcopy(active)
        from agent_run.ticket_state import capture_ticket_wait
        if not self._parent_scope:
            capture_ticket_wait(merged)
            selected = merged.get("active_ticket_job")
            if isinstance(selected, dict) and type(selected.get("ticket_number")) is int:
                merged["ticket_jobs"][str(selected["ticket_number"])] = selected
        if not self._parent_scope and "ticket_work_queue" in merged:
            from agent_run.ticket_work import reconcile
            reconcile(merged)
        durable = _sanitize_durable_errors(merged)
        _append_timeline_event(durable, latest)
        from agent_run.state_contract import require_current_run_state
        require_current_run_state(durable, parent_scope=self._parent_scope)
        changed = {key for key in latest.keys() | durable.keys() if latest.get(key, _MISSING) != durable.get(key, _MISSING)}
        keyed_changes = {
            field: {number for number in latest.get(field, {}).keys() | durable.get(field, {}).keys()
                    if latest.get(field, {}).get(number, _MISSING) != durable.get(field, {}).get(number, _MISSING)}
            for field in _KEYED_TICKET_FIELDS & changed
        }
        self._store._repository.save_objects(
            run_id, durable, fields=changed, history_positions=positions, keyed_changes=keyed_changes,
        )
        # Requeue emits a new retirement record; append it atomically without
        # assembling any earlier generation or another Ticket's archive.
        retired = state.get("retired_job_generations")
        if isinstance(retired, list) and retired:
            self._store._repository.append_array(run_id, "retired_job_generations", retired)
            state.pop("retired_job_generations", None)
        # Structure retirement emits one former Parent owner. Persist it with
        # removal of the current owner; do not hydrate or rewrite old archives.
        if "retired_parent_job" in state:
            self._store._repository.save_objects(
                run_id, {"retired_parent_job": state["retired_parent_job"]},
                fields={"retired_parent_job"},
            )
            state.pop("retired_parent_job")
        # Publish only the bounded projection after durability, preserving the
        # same outer-lock release semantics as StateStore's existing observer.
        notifications = self._store._commit_context.notifications
        notifications.append(deepcopy(durable) if self.run_saved_observer is not None else None)
        self._record_baseline(run_id, durable, positions, projection=state)
        if expected is not None:
            self._local.expected[run_id] = deepcopy(_comparison_facts(durable))
        _sync_current_facts(state, durable, excluded=self._excluded_fields)

    @contextmanager
    def require_unchanged_run(self, run_id: str, expected: dict[str, Any]) -> Iterator[None]:
        previous = getattr(self._local, "expected", {})
        self._local.expected = {**previous, run_id: deepcopy(expected)}
        try:
            yield
        finally:
            self._local.expected = previous

    def load_unchanged_run(self, run_id: str) -> dict[str, Any] | None:
        expected = getattr(self._local, "expected", {}).get(run_id)
        latest = self.load_scheduler_current_run(run_id)
        if expected is None or latest is None or _comparison_facts(latest) != _comparison_facts(expected):
            raise ConcurrentRunStateError("Run changed before Controller returned its refresh")
        return latest

    def find_run(self, repository: str, parent_number: int) -> dict[str, Any] | None:
        matches = self._find_runs(repository, parent_number, unfinished_only=False)
        if not matches:
            return None
        selected = max(matches, key=lambda state: str(state.get("created_at", "")))
        return self.load_run(str(selected["run_id"]))

    def find_unfinished_runs(self, repository: str, parent_number: int) -> list[dict[str, Any]]:
        return self._find_runs(repository, parent_number, unfinished_only=True)

    def _find_runs(self, repository: str, parent_number: int, *, unfinished_only: bool) -> list[dict[str, Any]]:
        self._store._reject_legacy_run()
        result = []
        for run_id in self._store._repository.find_ids(repository, parent_number, unfinished_only=unfinished_only):
            state = self.load_run(run_id)
            if state is not None:
                result.append(state)
        return sorted(result, key=lambda state: str(state.get("created_at", "")))

    def create_run_if_absent(self, run_id: str, state: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        saved, created = self._store.create_run_if_absent(run_id, state)
        loaded = self.load_current_run(run_id)
        return loaded if loaded is not None else saved, created

    def create_run_if_unfinished_absent(self, repository: str, parent_number: int, run_id: str, state: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        saved, created = self._store.create_run_if_unfinished_absent(repository, parent_number, run_id, state)
        loaded = self.load_current_run(str(saved.get("run_id", run_id)))
        return loaded if loaded is not None else saved, created


def object_store(store: StateStore, *, parent_scope: bool = False) -> StateStore:
    """Adapt one scope while retaining shared authorization and commit admission."""
    if not isinstance(store, (StateStore, ObjectStateStore)):
        return store
    if isinstance(store, ObjectStateStore):
        if store._parent_scope == parent_scope:
            return cast(StateStore, store)
        store = store._store
    attribute = "_parent_object_store_adapter" if parent_scope else "_object_store_adapter"
    existing = getattr(store, attribute, None)
    if existing is None:
        existing = ObjectStateStore(store, parent_scope=parent_scope)
        setattr(store, attribute, existing)
    return cast(StateStore, existing)


object_state_store = object_store


def save_owned(
    store: StateStore, run_id: str, state: dict[str, Any], *, fields: set[str],
    expected: dict[str, Any] | None = None,
    ticket_wait: bool = False,
) -> None:
    """Commit explicit owners without loading unrelated Jobs or history.

    Expected facts are captured before an external wait/control update; stale
    results cannot erase later Stop/Abandon or a different owner's transition.
    Cleanup may omit the baseline because it owns only its cleanup receipt.
    """
    adapter = cast(ObjectStateStore, object_store(store))
    base = adapter._store
    def commit() -> None:
        base._reject_legacy_run(run_id)
        identity_fields = {"run_id", "repository", "parent", "status", "phase", "notifications", "updated_at", "delivery_cleanup"}
        marker_fields = {
            "active_ticket_job", "active_agent_invocation", "parent_job", "run_acceptance",
            "run_publication", "resume_audit", "unsupported_scope_change", "delivery_type",
            "diagnostics", "terminal_kind", "supervision_window", "supervision_wait",
            "timeline", "timeline_continuation", "timeline_at_capacity",
            "github_refresh_pending",
        }
        if fields == {"delivery_cleanup"}:
            marker_fields = set()
        latest = base._repository.load_fields(run_id, fields | set(expected or {}) | identity_fields | marker_fields)
        if latest is None:
            raise ValueError("Delivery Run disappeared during object commit")
        durable = _sanitize_durable_errors({field: deepcopy(state[field]) for field in fields if field in state})
        if expected is not None:
            observed = latest
            job = latest.get("active_ticket_job")
            wait = job.get("external_wait") if isinstance(job, dict) else None
            if (ticket_wait and isinstance(wait, dict)
                    and latest.get("status") in {"active", "ticket_delivery_pending"}
                    and not latest.get("github_refresh_pending")):
                # The scheduler's Run selection can be active while its exact
                # Ticket lane retains an external wait. Compare the same lane
                # projection that the Driver received, keeping Run stop gates.
                observed = {**latest, **wait}
            for field in expected:
                if (observed.get(field) != expected.get(field)
                        and (field not in fields or observed.get(field) != durable.get(field))):
                    raise ConcurrentRunStateError(f"Object changed before commit: {field}")
            if latest.get("status") in {"abandoned", "completed"} and latest.get("status") != state.get("status"):
                raise ConcurrentRunStateError("Run became terminal before wait/control commit")
        projection = deepcopy(latest)
        for field in fields:
            if field in durable:
                projection[field] = durable[field]
            else:
                projection.pop(field, None)
        write_fields = set(fields)
        keyed_changes: dict[str, set[str]] = {}
        if ticket_wait and isinstance((job := projection.get("active_ticket_job")), dict):
            number = str(job["ticket_number"])
            owners = base._repository.load_paths(run_id, {("ticket_jobs", number)})
            canonical = owners.get("ticket_jobs", {}).get(number) if owners else None
            if canonical != latest.get("active_ticket_job"):
                raise ConcurrentRunStateError("Ticket wait owner changed before commit")
            projection["ticket_jobs"] = {number: deepcopy(canonical)}
            from agent_run.ticket_state import capture_ticket_wait
            capture_ticket_wait(projection)
            if job != canonical:
                projection["ticket_jobs"][number] = job
                durable["active_ticket_job"] = deepcopy(job)
                durable["ticket_jobs"] = {number: deepcopy(job)}
                write_fields.update({"active_ticket_job", "ticket_jobs"})
                keyed_changes["ticket_jobs"] = {number}
        if "run_id" in projection and marker_fields:
            _append_timeline_event(projection, latest)
            for field in ("timeline", "timeline_continuation", "timeline_at_capacity"):
                if latest.get(field) != projection.get(field):
                    durable[field] = deepcopy(projection[field])
                    write_fields.add(field)
        base._repository.save_objects(run_id, durable, fields=write_fields, keyed_changes=keyed_changes)
        for field in write_fields - fields:
            if field == "ticket_jobs":
                state.setdefault(field, {}).update(deepcopy(durable[field]))
            else:
                state[field] = deepcopy(durable[field])
        observer = projection if base.run_saved_observer is not None else None
        base._commit_context.notifications.append(observer)
    base._commit_run(commit)
