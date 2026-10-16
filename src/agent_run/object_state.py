"""StateStore adapter for current-object commits and indexed related history."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from typing import Any, cast

from agent_run.state import ConcurrentRunStateError, StateStore, _append_timeline_event, _sanitize_durable_errors
from agent_run.state_observation import saved_observation
from agent_run.object_projection import ObjectProjection, ProjectionBaseline, ProjectionKey
from agent_run.sqlite_encoding import copy_json_tree, encode_json
from agent_run.sqlite_job_history import FIELDS as JOB_ARCHIVES
from agent_run.sqlite_change_job_schema import CYCLE_DERIVED
from agent_run.sqlite_mutations import _same
from agent_run import sqlite_timeline
from agent_run.work_projection import JobHistoryAppend, JobProjection, restore_work_scopes


_MISSING = object()

_ARCHIVE_FIELDS = frozenset(
    {
        "retired_job_generations",
        "retired_parent_job",
        "retired_run_acceptance",
        "retired_run_publication",
        "retired_semantic_attempt_owners",
    }
)
_KEYED_TICKET_FIELDS = frozenset(
    {
        "ticket_jobs",
        "ticket_agent_invocations",
        "retired_ticket_generations",
        "pending_ticket_retirements",
    }
)


_PARENT_EXCLUDED_FIELDS = frozenset(
    {
        "ticket_jobs",
        "ticket_agent_invocations",
        "ticket_pause_requests",
        "retired_ticket_generations",
        "pending_ticket_retirements",
        "retired_parent_job",
        "retired_run_acceptance",
        "retired_run_publication",
    }
)


def _ticket_current(job: Any) -> Any:
    if not isinstance(job, dict):
        return job
    return {key: value for key, value in job.items() if key not in JOB_ARCHIVES}


def _owner_facts(job: Any) -> Any:
    """Compare Repair facts rather than temporary mirrors of that same Job."""
    if not isinstance(job, dict):
        return job
    job = {key: value for key, value in job.items() if key not in JOB_ARCHIVES}
    if isinstance(job.get("repair_job"), dict):
        job["repair_job"] = _owner_facts(job["repair_job"])
    repair, cycle = job.get("repair_job"), job.get("repair_cycle")
    budgets = job.get("repair_integration_budgets")
    if isinstance(budgets, dict):
        generation = str(repair.get("repair_generation")) if isinstance(repair, dict) else None
        job["repair_integration_budgets"] = {
            generation: budgets[generation]
        } if generation in budgets else {}
    if not (
        isinstance(repair, dict)
        and isinstance(cycle, dict)
        and repair.get("repair_generation") == cycle.get("generation")
    ):
        return job
    return {
        **{
            key: value
            for key, value in job.items()
            if key not in {"modification_attempts", "code_modification_attempts"}
        },
        "repair_cycle": {key: value for key, value in cycle.items() if key not in CYCLE_DERIVED},
    }


def _merge_change(old: Any, new: Any, latest: Any, owner: str) -> Any:
    if isinstance(new, JobHistoryAppend):
        if isinstance(old, JobHistoryAppend) and (old.base_count != new.base_count or new[: len(old)] != old):
            raise ConcurrentRunStateError(f"History append changed before commit: {owner}")
        return deepcopy(new)
    if _same(old, new):
        return deepcopy(latest)
    if isinstance(old, dict) and isinstance(new, dict) and isinstance(latest, dict):
        if owner == "active_ticket_job" or owner.startswith("ticket_jobs.") and owner.count(".") == 1:
            if not _same(_ticket_current(old), _ticket_current(latest)) and not _same(
                _ticket_current(new), _ticket_current(latest)
            ):
                raise ConcurrentRunStateError(f"Owner changed before commit: {owner}")
            if owner == "active_ticket_job" and (
                old.get("ticket_number"), old.get("ticket_branch_generation")
            ) != (new.get("ticket_number"), new.get("ticket_branch_generation")):
                # Selection replaces the owner, including its exact query
                # scope; merging its fields would retain the prior Job's ID.
                return deepcopy(new)
        elif owner in {"parent_job", "run_acceptance", "run_publication", "active_agent_invocation"}:
            if not _same(_owner_facts(old), _owner_facts(latest)) and not _same(
                _owner_facts(new), _owner_facts(latest)
            ):
                raise ConcurrentRunStateError(f"Owner changed before commit: {owner}")
        result = deepcopy(latest)
        if isinstance(result, JobProjection) and isinstance(new, JobProjection):
            result.publication_update = deepcopy(new.publication_update)
        for key in old.keys() | new.keys():
            if _same(old.get(key, _MISSING), new.get(key, _MISSING)):
                continue
            if key not in new:
                unchanged = (
                    _same(_owner_facts(latest.get(key)), _owner_facts(old.get(key)))
                    if owner == "run_acceptance" and key == "repair_job"
                    else _same(latest.get(key, _MISSING), old.get(key, _MISSING))
                )
                if key in latest and not unchanged:
                    raise ConcurrentRunStateError(f"Object changed before commit: {owner}.{key}")
                result.pop(key, None)
            else:
                result[key] = _merge_change(
                    old.get(key, _MISSING), new[key], latest.get(key, _MISSING), f"{owner}.{key}"
                )
        return result
    if (
        owner == "agent_invocation_history"
        and isinstance(old, list)
        and isinstance(new, list)
        and isinstance(latest, list)
    ):
        from agent_run.sqlite_work_execution_invocations import invocation_identity

        previous = {invocation_identity(item): item for item in old}
        result = {invocation_identity(item): deepcopy(item) for item in latest}
        for item in new:
            identity = invocation_identity(item)
            if not _same(item, previous.get(identity)):
                result[identity] = _merge_change(
                    previous.get(identity), item, result.get(identity), "invocation:" + identity
                )
        return list(result.values())
    if not _same(latest, old) and not _same(latest, new):
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


def current_projection_facts(state: dict[str, Any]) -> dict[str, Any]:
    """Compare the same current Invocation relationship after owner retirement."""
    from agent_run.sqlite_work_execution_invocations import invocation_identity

    invocations = [
        item for item in [state.get("active_agent_invocation"),
                          *state.get("ticket_agent_invocations", {}).values()]
        if isinstance(item, dict)
    ]
    identities = {
        invocation_identity(item)
        for item in invocations
    }
    required_resumes = {item.get("resume_id") for item in invocations} - {None}
    audit = state.get("resume_audit")
    current_audit = audit
    if isinstance(audit, dict):
        # A frame can still hold an earlier event after appending the next
        # Resume. Compare the shared current contract, not its loaded prefix.
        current_audit = {**audit, "history": [
            item for item in audit.get("history", []) if isinstance(item, dict)
            and (item.get("sequence") == audit.get("total") or item.get("resume_id") in required_resumes)
        ]}
    resumes = {
        item.get("resume_id") for item in (current_audit or {}).get("history", []) if isinstance(item, dict)
    } - {None}
    result = {
        **{key: value for key, value in state.items() if key not in _ARCHIVE_FIELDS},
        **({"resume_audit": current_audit} if "resume_audit" in state else {}),
        **({"human_response_audit": {key: value for key, value in state["human_response_audit"].items()
                                     if key in resumes}} if "human_response_audit" in state else {}),
        "active_ticket_job": _ticket_current(state.get("active_ticket_job")),
        "ticket_jobs": {key: _ticket_current(job) for key, job in state.get("ticket_jobs", {}).items()},
        **{
            field: _owner_facts(state[field])
            for field in ("parent_job", "run_acceptance", "run_publication")
            if field in state
        },
        "agent_invocation_history": [
            item
            for item in state.get("agent_invocation_history", [])
            if invocation_identity(item) in identities or item.get("resume_id") in resumes
        ],
    }
    # Compare complete and current views by the same current stream facts.
    # Scope values are local comparison inputs, never fields saved to a Run.
    timelines = state.timelines if isinstance(state, ObjectProjection) else {}
    for field in sqlite_timeline.FIELDS:
        events = state.get(field)
        if isinstance(events, list):
            result[field] = (len(events), deepcopy(events[-1]) if events else None)
        elif field in timelines:
            result[field] = (timelines[field].count, timelines[field].tail())
    return result


class ObjectStateStore:
    """Use the existing authorization, FIFO and notification boundaries.

    A caller's projection baseline determines its changed top-level owners;
    unrelated committed owners and archived Invocations are never overwritten.
    """

    def __init__(self, store: StateStore, *, parent_scope: bool = False) -> None:
        self._store = store
        self._parent_scope = parent_scope
        self._excluded_fields = (
            set(_PARENT_EXCLUDED_FIELDS | _ARCHIVE_FIELDS) if parent_scope else set(_ARCHIVE_FIELDS)
        )
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
        self,
        run_id: str,
        state: dict[str, Any],
        positions: dict[str, int],
        *,
        projection: dict[str, Any] | None = None,
    ) -> None:
        record = ProjectionBaseline(copy_json_tree(dict(state)), dict(positions))
        baselines = self._baseline()
        baselines.clear()
        baselines[run_id] = (record.facts, record.history_positions)
        carrier = state if projection is None else projection
        if isinstance(carrier, ObjectProjection):
            key = self._projection_key(run_id)
            carrier.record_baseline(key, record)
            self._store._repository._on_rollback(lambda: carrier.invalidate_baseline(key))

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
        self,
        run_id: str,
        *,
        scheduler: bool = False,
        ticket_numbers: set[int] | None = None,
    ) -> dict[str, Any] | None:
        self._store._reject_legacy_run(run_id)
        excluded = self._excluded_fields | (set(_KEYED_TICKET_FIELDS) if not self._parent_scope else set())
        loaded = self._store._read_run_materials(
            run_id,
            lambda: self._store._repository.load_objects(
                run_id,
                excluded_fields=excluded,
                related_tickets=not self._parent_scope,
                ticket_numbers=ticket_numbers,
                scheduler_tickets=scheduler,
                thin_timeline=True,
            ),
        )
        if loaded is None:
            return None
        facts, positions = loaded
        state = ObjectProjection(facts)
        state.archive_scope = dict(getattr(self._store._repository._local, "object_archives", {}))
        state.attempt_scope = deepcopy(getattr(self._store._repository._local, "object_attempts", {}))
        state.timelines = deepcopy(self._store._repository.work_timelines())
        self._store._repository._local.object_archives = {}
        self._store._repository._local.object_attempts = {}
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
        evidence = self._store._read_run_materials(
            run_id,
            lambda: self._store._repository.load_fields(run_id, {"ticket_jobs"}),
        )
        if evidence is None:
            raise ValueError("Delivery Run disappeared at its evidence boundary")
        from agent_run.integration_record_contract import require_completed_ticket_integration_records

        require_completed_ticket_integration_records(evidence)
        return evidence

    def save_run(self, run_id: str, state: dict[str, Any]) -> None:
        try:
            self._store._commit_run(lambda: self._save_objects_and_notify_unlocked(run_id, state))
        except BaseException:
            self.invalidate_baseline()
            if isinstance(state, ObjectProjection):
                state.invalidate_baseline(self._projection_key(run_id))
            raise

    def _save_objects_and_notify_unlocked(self, run_id: str, state: dict[str, Any]) -> None:
        key = self._projection_key(run_id)
        if isinstance(state, ObjectProjection) and state.baseline_is_invalid(key):
            raise ValueError("Object save requires a fresh read after failed commit")
        record = state.baseline(key) if isinstance(state, ObjectProjection) else None
        baseline = (record.facts, record.history_positions) if record is not None else self._baseline().get(run_id)
        original = baseline[0] if baseline is not None else {}
        if isinstance(state, ObjectProjection) and state.timelines.keys() & state.keys():
            raise ValueError("Object save cannot replace an unread Timeline")
        thin_timeline = not bool(sqlite_timeline.FIELDS & (original.keys() | state.keys()))
        touched = {
            int(number)
            for field in _KEYED_TICKET_FIELDS
            for number in original.get(field, {}).keys() | state.get(field, {}).keys()
            if not _same(original.get(field, {}).get(number, _MISSING), state.get(field, {}).get(number, _MISSING))
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
            touched.update(
                item["ticket_number"]
                for item in state.get(field, [])
                if isinstance(item, dict) and type(item.get("ticket_number")) is int
            )
        expected = getattr(self._local, "expected", {}).get(run_id)
        excluded = self._excluded_fields | (set(_KEYED_TICKET_FIELDS) if not self._parent_scope else set())
        loaded = self._store._repository.load_objects(
            run_id,
            excluded_fields=excluded,
            related_tickets=not self._parent_scope,
            ticket_numbers=None if self._parent_scope else touched,
            scheduler_tickets=not self._parent_scope,
            thin_timeline=thin_timeline,
        )
        if loaded is None:
            # New Run creation remains a complete creation transaction.
            self._store._create_run_and_notify_unlocked(run_id, state)
            created = self._store._repository.load_objects(run_id, excluded_fields=self._excluded_fields)
            if created is not None:
                self._record_baseline(run_id, created[0], created[1], projection=state)
            return
        latest, positions = loaded
        timelines = self._store._repository.work_timelines() if thin_timeline else {}
        if timelines:
            latest = ObjectProjection(latest)
            latest.timelines = deepcopy(timelines)
        if baseline is not None:

            def retain_loaded_archives(previous: Any, proposed: Any, current: Any) -> None:
                if not all(isinstance(value, dict) for value in (previous, proposed, current)):
                    return
                if isinstance(current, JobProjection):
                    requested = JOB_ARCHIVES & (previous.keys() | proposed.keys())
                    requested = {
                        name for name in requested if not isinstance(proposed.get(name), JobHistoryAppend)
                    }
                    current.materialize_histories(requested)
                    for name in requested:
                        if (
                            name in current
                            and name not in previous
                            and isinstance(proposed, JobProjection)
                            and name in proposed.loaded_histories
                        ):
                            if (proposed.job_id, proposed.revision) != (current.job_id, current.revision):
                                raise ConcurrentRunStateError("Job changed before its loaded history commit")
                            previous[name] = deepcopy(current[name])
                retain_loaded_archives(
                    previous.get("repair_job"), proposed.get("repair_job"), current.get("repair_job")
                )

            for owner_field in ("parent_job", "run_acceptance", "run_publication"):
                retain_loaded_archives(original.get(owner_field), state.get(owner_field), latest.get(owner_field))
        # Keep an explicitly loaded target history available for later role
        # closeout. A scheduling-only sibling has no loaded archive fields
        # and remains outside this read and commit scope.
        if baseline is not None:
            for ticket_number in touched:
                key_number = str(ticket_number)
                old_job = original.get("ticket_jobs", {}).get(key_number, {})
                new_job = state.get("ticket_jobs", {}).get(key_number, {})
                if (
                    isinstance(old_job, dict)
                    and isinstance(new_job, dict)
                    and any(field in old_job or field in new_job for field in JOB_ARCHIVES)
                ):
                    current = self._store._repository.load_paths(run_id, {("ticket_jobs", key_number)})
                    owner = current.get("ticket_jobs", {}).get(key_number) if current else None
                    if isinstance(owner, dict):
                        retain_loaded_archives(old_job, new_job, owner)
                        latest.setdefault("ticket_jobs", {})[key_number] = owner
                        archive_scope = self._store._repository._local.object_archives
                        for field in JOB_ARCHIVES & owner.keys():
                            archive_scope.pop(encode_json(("ticket_jobs", key_number, field)), None)
                        selected = latest.get("active_ticket_job")
                        if isinstance(selected, dict) and selected.get("ticket_number") == ticket_number:
                            retain_loaded_archives(
                                original.get("active_ticket_job"), state.get("active_ticket_job"), owner
                            )
                            latest["active_ticket_job"] = owner
                            for field in JOB_ARCHIVES & owner.keys():
                                archive_scope.pop(encode_json(("active_ticket_job", field)), None)
        from agent_run.invocation_settlement import current_settlement, commit_settlement
        from agent_run.ticket_state import RUN_STOP_STATUSES

        permission = current_settlement()
        if permission is not None and (permission.limited or latest.get("status") in RUN_STOP_STATUSES):
            commit_settlement(self._store, run_id, state)
            self.invalidate_baseline()
            return
        if baseline is None:
            raise ValueError("Object save requires a loaded baseline or explicit save_owned")
        old = baseline[0]
        if (
            expected is None
            and not self._parent_scope
            and state.get("resume_audit", {}).get("total", 0) <= latest.get("resume_audit", {}).get("total", 0)
        ):
            # A targeted Resume supersedes earlier supervision of that owner,
            # even when the old frame has no delta. Unchanged sibling Jobs do
            # not participate in this authorization fence.
            owners = {
                str(job["ticket_number"])
                for job in (
                    old.get("active_ticket_job"),
                    state.get("active_ticket_job"),
                )
                if isinstance(job, dict) and type(job.get("ticket_number")) is int
            }
            owners.update(
                number
                for number in old.get("ticket_jobs", {}).keys() | state.get("ticket_jobs", {}).keys()
                if not _same(
                    old.get("ticket_jobs", {}).get(number, _MISSING),
                    state.get("ticket_jobs", {}).get(number, _MISSING),
                )
            )
            for number in owners:
                job = latest.get("ticket_jobs", {}).get(number, {})
                proposed = state.get("ticket_jobs", {}).get(number, {})
                if (
                    job.get("last_ticket_resume")
                    and job.get("ticket_branch_generation") == proposed.get("ticket_branch_generation")
                    and job["last_ticket_resume"] != proposed.get("last_ticket_resume")
                ):
                    raise ConcurrentRunStateError("Ticket Resume superseded the Run projection")
        if self._parent_scope:
            # Parent results also depend on unchanged requirements and the
            # current owner, even when this write only updates an Invocation.
            for field in ("parent_job", "run_acceptance", "run_publication"):
                if not _same(_owner_facts(latest.get(field)), _owner_facts(old.get(field))) and not _same(
                    _owner_facts(latest.get(field)), _owner_facts(state.get(field))
                ):
                    raise ConcurrentRunStateError(f"Parent owner changed before commit: {field}")
            for field in ("accepted_parent_spec_revision", "accepted_ticket_graph_revision"):
                if latest.get(field) != old.get(field):
                    raise ConcurrentRunStateError(f"Parent demand changed before commit: {field}")
            for field in ("parent", "ticket_graph"):
                if latest.get(field, {}).get("revision") != old.get(field, {}).get("revision"):
                    raise ConcurrentRunStateError(f"Parent demand changed before commit: {field}")
            if latest.get("status") in {
                "operator_stopped",
                "abandoned",
                "abandonment_pending",
                "completed",
                "execution_failed",
            }:
                if latest.get("status") != old.get("status") and latest.get("status") != state.get("status"):
                    raise ConcurrentRunStateError("Parent lost Run authorization before commit")
        fields = {
            key
            for key in (old.keys() | state.keys()) - self._excluded_fields
            if not _same(old.get(key, _MISSING), state.get(key, _MISSING))
        }
        expected = getattr(self._local, "expected", {}).get(run_id)
        if expected is not None and not _same(current_projection_facts(latest), current_projection_facts(expected)):
            raise ConcurrentRunStateError("Run changed during Controller refresh")
        merged = copy_json_tree(latest)
        for field in fields:
            if field in state:
                merged[field] = _merge_change(
                    old.get(field, _MISSING), state[field], latest.get(field, _MISSING), field
                )
            else:
                if field in latest and not _same(latest[field], old.get(field, _MISSING)):
                    raise ConcurrentRunStateError(f"Object changed before commit: {field}")
                merged.pop(field, None)
        # Current selection is a view of the exact Ticket Invocation, as in
        # the initial Run association; association commits stay atomic.
        active = merged.get("active_agent_invocation")
        job = merged.get("active_ticket_job")
        if (
            isinstance(active, dict)
            and isinstance(job, dict)
            and active.get("work_subject") == f"ticket:{job.get('ticket_number')}"
        ):
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
        from agent_run.work_projection import restore_work_scopes

        restore_work_scopes(durable, merged)
        timeline_updates = sqlite_timeline.append_events(durable, latest, timelines) if timelines else {}
        if not timelines:
            _append_timeline_event(durable, latest)
        from agent_run.state_contract import require_current_run_state

        archives = dict(getattr(self._store._repository._local, "object_archives", {}))
        from agent_run.sqlite_change_jobs import projection_archives

        archives = projection_archives(durable, archives)
        attempts = dict(getattr(self._store._repository._local, "object_attempts", {}))
        if archives or timelines:
            from agent_run.state_contract import require_current_work_state

            scope = ObjectProjection(durable)
            scope.archive_scope, scope.attempt_scope = archives, attempts
            scope.timelines = timelines
            # Graph refresh can select a different Ticket. Follow its exact
            # canonical owner rather than retaining the old selection's proof.
            scope.refresh_scope()
            archives, attempts = scope.archive_scope, scope.attempt_scope
            require_current_work_state(durable, archives, attempts, timelines, parent_scope=self._parent_scope)
        else:
            require_current_run_state(durable, parent_scope=self._parent_scope)
        changed = {
            key
            for key in latest.keys() | durable.keys()
            if not _same(latest.get(key, _MISSING), durable.get(key, _MISSING))
        }
        keyed_changes = {
            field: {
                number
                for number in latest.get(field, {}).keys() | durable.get(field, {}).keys()
                if not _same(latest.get(field, {}).get(number, _MISSING), durable.get(field, {}).get(number, _MISSING))
            }
            for field in _KEYED_TICKET_FIELDS & changed
        }
        self._store._repository.save_objects(
            run_id,
            durable,
            fields=changed,
            history_positions=positions,
            keyed_changes=keyed_changes,
            archive_scope=archives,
            timeline_updates=timeline_updates if thin_timeline else None,
        )
        gates_changed, gate_mirrors = self._store._repository.project_saved_job_gates(
            run_id,
            durable,
            fields=changed,
            keyed_changes=keyed_changes,
        )
        if gates_changed:
            if archives or timelines:
                require_current_work_state(durable, archives, attempts, timelines, parent_scope=self._parent_scope)
            else:
                require_current_run_state(durable, parent_scope=self._parent_scope)
            if gate_mirrors:
                self._store._repository.save_objects(run_id, durable, fields=gate_mirrors)
        # Requeue emits a new retirement record; append it atomically without
        # assembling any earlier generation or another Ticket's archive.
        for archive_field in ("retired_job_generations", "retired_semantic_attempt_owners"):
            retired = state.get(archive_field)
            if isinstance(retired, list) and retired:
                self._store._repository.append_array(run_id, archive_field, retired)
                state.pop(archive_field, None)
        # Structure retirement emits one former Parent owner. Persist it with
        # removal of the current owner; do not hydrate or rewrite old archives.
        if "retired_parent_job" in state:
            self._store._repository.save_objects(
                run_id,
                {"retired_parent_job": state["retired_parent_job"]},
                fields={"retired_parent_job"},
            )
            state.pop("retired_parent_job")
        if thin_timeline:
            timelines = self._store._repository.work_timelines()
            for field in timelines:
                durable.pop(field, None)
            durable = ObjectProjection(durable)
            durable.timelines = deepcopy(timelines)
        # Publish after durability with the observer's declared query scope.
        notifications = self._store._commit_context.notifications
        notifications.append(saved_observation(self._store, run_id, durable, timelines))
        self._record_baseline(run_id, durable, positions, projection=state)
        if expected is not None:
            self._local.expected[run_id] = deepcopy(current_projection_facts(durable))
        _sync_current_facts(state, durable, excluded=self._excluded_fields)
        restore_work_scopes(state, durable, preserve_owner_references=True)
        if isinstance(state, ObjectProjection):
            state.archive_scope = archives
            state.attempt_scope = attempts
            state.timelines = deepcopy(timelines)

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
        if (
            expected is None
            or latest is None
            or not _same(current_projection_facts(latest), current_projection_facts(expected))
        ):
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

    def create_run_if_unfinished_absent(
        self, repository: str, parent_number: int, run_id: str, state: dict[str, Any]
    ) -> tuple[dict[str, Any], bool]:
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
    store: StateStore,
    run_id: str,
    state: dict[str, Any],
    *,
    fields: set[str],
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
        identity_fields = {
            "run_id",
            "repository",
            "parent",
            "status",
            "phase",
            "notifications",
            "updated_at",
            "delivery_cleanup",
        }
        marker_fields = {
            "active_ticket_job",
            "active_agent_invocation",
            "parent_job",
            "run_acceptance",
            "run_publication",
            "resume_audit",
            "unsupported_scope_change",
            "delivery_type",
            "diagnostics",
            "terminal_kind",
            "supervision_window",
            "supervision_wait",
            "timeline",
            "timeline_continuation",
            "timeline_at_capacity",
            "github_refresh_pending",
        }
        if fields == {"delivery_cleanup"}:
            marker_fields = set()
        expected_timelines = expected.timelines if isinstance(expected, ObjectProjection) else {}
        if isinstance(state, ObjectProjection) and fields & (state.timelines.keys() - state.keys()):
            raise ValueError("Object save cannot remove an unread Timeline")
        thin_timeline = bool(marker_fields or expected_timelines) and not bool(
            sqlite_timeline.FIELDS & (fields | set(expected or {}) | state.keys())
        )
        latest = base._repository.load_fields(
            run_id, fields | set(expected or {}) | identity_fields | marker_fields | expected_timelines.keys(),
            thin_timeline=thin_timeline,
        )
        if latest is None:
            raise ValueError("Delivery Run disappeared during object commit")
        timelines = base._repository.work_timelines() if thin_timeline else {}
        durable = _sanitize_durable_errors({field: deepcopy(state[field]) for field in fields if field in state})
        if expected is not None:
            if expected_timelines:
                for field, previous_scope in expected_timelines.items():
                    events = latest.get(field)
                    current_scope = (sqlite_timeline.TimelineScope(len(events), events[-1] if events else None)
                                     if isinstance(events, list) else timelines.get(field))
                    if field not in fields and (
                        current_scope is None
                        or not _same((previous_scope.count, previous_scope.latest),
                                     (current_scope.count, current_scope.latest))
                    ):
                        raise ConcurrentRunStateError(f"Object changed before commit: {field}")
            observed = latest
            job = latest.get("active_ticket_job")
            wait = job.get("external_wait") if isinstance(job, dict) else None
            if (
                ticket_wait
                and isinstance(wait, dict)
                and latest.get("status") in {"active", "ticket_delivery_pending"}
                and not latest.get("github_refresh_pending")
            ):
                # The scheduler's Run selection can be active while its exact
                # Ticket lane retains an external wait. Compare the same lane
                # projection that the Driver received, keeping Run stop gates.
                observed = {**latest, **wait}
            for field in expected:

                def matches(left: Any, right: Any) -> bool:
                    # A wait owns current facts. The exact Ticket's archived
                    # evidence can be absent from its scheduling projection.
                    if field == "active_ticket_job":
                        return _same(_ticket_current(left), _ticket_current(right))
                    # Numeric wait clocks allow equivalent int/float values
                    # across the typed SQL and remaining Run representations.
                    return bool(left == right)

                if not matches(observed.get(field), expected.get(field)) and (
                    field not in fields or not matches(observed.get(field), durable.get(field))
                ):
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
        timeline_updates = {}
        if "run_id" in projection and marker_fields:
            if timelines:
                timeline_updates = sqlite_timeline.append_events(projection, latest, timelines)
            else:
                _append_timeline_event(projection, latest)
            for field in ("timeline", "timeline_continuation", "timeline_at_capacity"):
                if latest.get(field) != projection.get(field):
                    durable[field] = deepcopy(projection[field])
                    write_fields.add(field)
        base._repository.save_objects(
            run_id, durable, fields=write_fields, keyed_changes=keyed_changes,
            timeline_updates=timeline_updates if thin_timeline else None,
        )
        for field in (write_fields - fields) | (
            write_fields & {"timeline", "timeline_continuation", "timeline_at_capacity"}
        ):
            if field not in durable:
                state.pop(field, None)
            elif field == "ticket_jobs":
                state.setdefault(field, {}).update(deepcopy(durable[field]))
            else:
                state[field] = deepcopy(durable[field])
        if thin_timeline:
            timelines = base._repository.work_timelines()
            for field in timelines:
                projection.pop(field, None)
                state.pop(field, None)
            if isinstance(state, ObjectProjection):
                state.timelines = deepcopy(timelines)
        elif isinstance(state, ObjectProjection):
            state.timelines = {
                field: sqlite_timeline.TimelineScope(len(projection[field]), deepcopy(projection[field][-1]) if projection[field] else None)
                for field in state.timelines if field not in state and isinstance(projection.get(field), list)
            }
        base._commit_context.notifications.append(saved_observation(base, run_id, projection, timelines))
        if isinstance(state, ObjectProjection):
            key = adapter._projection_key(run_id)
            base._repository._on_rollback(lambda: state.invalidate_baseline(key))

    try:
        base._commit_run(commit)
    except BaseException:
        if isinstance(state, ObjectProjection):
            state.invalidate_baseline(adapter._projection_key(run_id))
        raise
