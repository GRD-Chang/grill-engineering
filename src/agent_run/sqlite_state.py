"""单库、明确领域记录；当前与历史由同一只读快照按需查询。"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from typing import Any, cast

from agent_run import sqlite_control
from agent_run import sqlite_work_records as work_records
from agent_run import sqlite_run_audit as audit
from agent_run import sqlite_timeline
from agent_run.sqlite_encoding import copy_json_tree, encode_json, object_path
from agent_run.sqlite_mutations import _same
from agent_run.sqlite_query_rows import bounded_query
from agent_run.sqlite_state_connection import SQLiteStateConnection, ReadConnection as ReadConnection
from agent_run.state_limits import check_response, require_response_limit
from agent_run.work_projection import JobProjection, JobHistoryAppend

_FIELDS = work_records.FIELDS | audit.FIELDS


class SQLiteStateRepository(SQLiteStateConnection):
    """Query and commit explicit Run domains using the shared SQLite lifecycle."""

    def _check_size(self, state: Mapping[str, Any]) -> None:
        require_response_limit(state, max_bytes=self.max_bytes)

    def _require_run_query(self, connection: sqlite3.Connection, run_id: str) -> bool:
        return bounded_query(connection, "SELECT 1 FROM runs WHERE run_id=?", (run_id,)).fetchone() is not None

    def _attach_readers(self, run_id: str, state: dict[str, Any]) -> None:
        def attach(value: Any) -> None:
            if isinstance(value, JobProjection):
                value.history_reader = lambda job, fields: self._read_job_histories(run_id, job, fields)
                value.previous_review_reader = lambda job: self._read_previous_review(run_id, job)
                value.publication_reader = lambda job: self._read_publication_attempt(run_id, job)
                value.latest_thread_reader = lambda job, field: self._read_latest_thread(run_id, job, field)
            if isinstance(value, dict):
                for key in ("repair_job", "repair_integration_budgets"):
                    nested = value.get(key)
                    if key == "repair_integration_budgets" and isinstance(nested, dict):
                        for item in nested.values():
                            attach(item)
                    else:
                        attach(nested)

        for field in ("active_ticket_job", "parent_job", "run_acceptance", "run_publication"):
            attach(state.get(field))
        for job in state.get("ticket_jobs", {}).values():
            attach(job)

    def _read_job_histories(self, run_id: str, job: JobProjection, fields: set[str]) -> dict[str, list[Any]]:
        from agent_run.sqlite_change_jobs import read_owner_history

        with self._reading() as connection:
            if connection is None:
                raise ValueError("Run disappeared before an exact historical query")
            row = bounded_query(connection,
                "SELECT revision FROM work_jobs WHERE run_id=? AND job_id=?", (run_id, job.job_id)
            ).fetchone()
            if row != (job.revision,):
                raise ValueError("Job changed before its exact historical query")
            histories = {field: read_owner_history(connection, run_id, job.job_id, field) for field in fields}
            check_response(histories, max_bytes=self.max_bytes)
            return histories

    def _read_previous_review(self, run_id: str, job: JobProjection) -> dict[str, Any] | None:
        from agent_run.sqlite_change_jobs import read_previous_review

        with self._reading() as connection:
            if connection is None:
                raise ValueError("Run disappeared before an exact Reviewer query")
            row = bounded_query(connection,
                "SELECT revision FROM work_jobs WHERE run_id=? AND job_id=?", (run_id, job.job_id)
            ).fetchone()
            if row != (job.revision,):
                raise ValueError("Job changed before its exact Reviewer query")
            value = read_previous_review(connection, run_id, job.job_id)
            check_response({"review": value}, max_bytes=self.max_bytes)
            return value

    def _read_latest_thread(self, run_id: str, job: JobProjection, field: str) -> str | None:
        from agent_run.sqlite_change_job_queries import read_latest_thread
        from agent_run.sqlite_change_job_schema import THREAD_FIELDS

        if field not in THREAD_FIELDS:
            raise ValueError("Unsupported exact Thread history")
        with self._reading() as connection:
            if connection is None:
                raise ValueError("Run disappeared before an exact Thread query")
            row = bounded_query(connection,
                "SELECT revision FROM work_jobs WHERE run_id=? AND job_id=?", (run_id, job.job_id),
            ).fetchone()
            if row != (job.revision,):
                raise ValueError("Job changed before its exact Thread query")
            value = read_latest_thread(connection, run_id, job.job_id, field)
            check_response({"thread_id": value}, max_bytes=self.max_bytes)
            return value

    def _read_publication_attempt(self, run_id: str, job: JobProjection) -> dict[str, Any] | None:
        from agent_run.sqlite_work_execution import load_completed_publication_attempt

        ordinal = job.get("publication_attempts")
        if type(ordinal) is not int or ordinal < 1:
            return None
        with self._reading() as connection:
            if connection is None:
                raise ValueError("Run disappeared before an exact Publication query")
            row = bounded_query(connection,
                "SELECT revision FROM work_jobs WHERE run_id=? AND job_id=?", (run_id, job.job_id)
            ).fetchone()
            if row != (job.revision,):
                raise ValueError("Job changed before its exact Publication query")
            value = load_completed_publication_attempt(connection, run_id, job.job_id, ordinal)
            check_response({"attempt": value}, max_bytes=self.max_bytes)
            return value

    def _returned(self, run_id: str, state: dict[str, Any]) -> dict[str, Any]:
        self._attach_readers(run_id, state)
        self._check_size(state)
        return state

    def load(self, run_id: str) -> dict[str, Any] | None:
        with self._reading() as connection:
            if connection is None or not self._require_run_query(connection, run_id):
                return None
            base, _ = audit.load(connection, run_id)
            return self._returned(run_id, work_records.load_complete(connection, run_id, base))

    def load_work(
        self, run_id: str, ticket_number: int | None, *, thin_timeline: bool = False
    ) -> dict[str, Any] | None:
        with self._reading() as connection:
            if connection is None or not self._require_run_query(connection, run_id):
                return None
            base, timelines = audit.load(connection, run_id, current_only=True, thin_timeline=thin_timeline)
            state, archives, related = work_records.load_work(connection, run_id, base, ticket_number)
            self._local.work_archives = archives
            self._local.work_attempts = related
            self._local.work_timelines = timelines
            self._check_size(state)
            self._attach_readers(run_id, state)
            self._local.work_view = (
                work_records.WorkBaseline(run_id, copy_json_tree(state), connection.total_changes)
                if self.in_transaction else None
            )
            return state

    def load_dispatch(self, run_id: str) -> dict[str, Any] | None:
        from agent_run import sqlite_ticket_dispatch

        with self._reading() as connection:
            self._local.work_view = None
            if connection is None or not self._require_run_query(connection, run_id):
                return None
            state = sqlite_ticket_dispatch.load(connection, run_id)
            self._check_size(state)
            return state

    def work_baseline(self, run_id: str, state: dict[str, Any]) -> dict[str, Any]:
        view = getattr(self._local, "work_view", None)
        connection = getattr(self._local, "connection", None)
        if (
            connection is not None
            and isinstance(view, work_records.WorkBaseline)
            and view.run_id == run_id
            and view.total_changes == connection.total_changes
            and _same(view.baseline, state)
        ):
            return view.baseline
        return cast(dict[str, Any], copy_json_tree(state))

    def current_work_baseline(self, run_id: str) -> dict[str, Any]:
        view = getattr(self._local, "work_view", None)
        connection = getattr(self._local, "connection", None)
        if (
            connection is None
            or not isinstance(view, work_records.WorkBaseline)
            or view.run_id != run_id
            or view.total_changes != connection.total_changes
        ):
            raise ValueError("Work baseline requires a current transaction read")
        return view.baseline

    def work_archives(self, state: Mapping[str, Any] | None = None) -> dict[str, tuple[int, int]]:
        result = dict(getattr(self._local, "work_archives", {}))
        if state is not None:
            from agent_run.sqlite_change_jobs import projection_archives

            result = projection_archives(state, result)
        return result

    def work_attempts(self, state: Mapping[str, Any] | None = None) -> dict[str, dict[str, Any]]:
        archives = self.work_archives(state)
        result = {}
        for path, attempt in getattr(self._local, "work_attempts", {}).items():
            if encode_json((*object_path(path), "semantic_attempt_history")) not in archives:
                continue
            owner: Any = state
            if state is not None:
                for part in object_path(path):
                    owner = owner.get(part) if isinstance(owner, Mapping) else None
                # Resume can explicitly archive the retry aggregate while
                # leaving the completed Attempt in its unread history.
                if isinstance(owner, Mapping) and owner.get("publication_operation_retry") is None:
                    continue
            result[path] = copy_json_tree(attempt)
        return result

    def work_timelines(self) -> dict[str, sqlite_timeline.TimelineScope]:
        return dict(getattr(self._local, "work_timelines", {}))

    def save(self, run_id: str, state: Mapping[str, Any]) -> None:
        if state.keys() - _FIELDS:
            raise ValueError(f"Unsupported Run fields: {sorted(state.keys()-_FIELDS)}")
        with self.atomic():
            c = self._local.connection
            if self._require_run_query(c, run_id):
                raise ValueError("SQLite Run already exists; use object commits to update it")
            c.execute("INSERT INTO runs VALUES (?,NULL,NULL,NULL,NULL,NULL,NULL)", (run_id,))
            work_records.save(c, run_id, state, {}, fields=set(state) & work_records.FIELDS)
            audit.save(c, run_id, state, {}, fields=set(state) & audit.FIELDS)
            if isinstance(state, dict):
                self._attach_readers(run_id, state)
            self._local.work_view = None
            if "action_application_receipt" in state:
                self._observe_application(c, run_id, state)

    def _observe_application(self, c: sqlite3.Connection, run_id: str, state: Mapping[str, Any]) -> None:
        receipt = state.get("action_application_receipt")
        if not isinstance(receipt, Mapping) or receipt.get("run_id") != run_id:
            raise ValueError("Cannot remove or replace the Run application receipt identity")
        sqlite_control.observe_application(c, receipt, str(self.root.resolve()))

    def save_work(
        self,
        run_id: str,
        state: Mapping[str, Any],
        baseline: Mapping[str, Any],
        *,
        timeline_updates: Mapping[str, dict[str, Any]] | None = None,
    ) -> bool:
        with self.atomic():
            c = self._local.connection
            view = getattr(self._local, "work_view", None)
            if (
                not isinstance(view, work_records.WorkBaseline)
                or view.run_id != run_id
                or view.total_changes != c.total_changes
                or not _same(view.baseline, baseline)
            ):
                raise ValueError("SQLite work patch requires its current transaction baseline")
            if state.keys() - _FIELDS:
                raise ValueError("Unsupported Run fields")
            fields = {
                key
                for key in state.keys() | baseline.keys()
                if key not in state or key not in baseline or not _same(state[key], baseline[key])
            }
            scopes = self.work_timelines()
            updates = timeline_updates or {}
            if updates.keys() - scopes.keys() or any(key in state or key in baseline for key in scopes):
                raise ValueError("SQLite work patch cannot replace unread Timeline fields")
            owned = fields & work_records.FIELDS
            work_records.save(c, run_id, state, baseline, fields=owned)
            audit.save(
                c, run_id, state, baseline, fields=(fields & audit.FIELDS) | set(updates), timeline_updates=updates
            )
            gates_changed = False
            if isinstance(state, dict):
                gates_changed, mirrors = self.project_saved_job_gates(run_id, state, fields=owned)
                if gates_changed and "ticket_work_queue" in state:
                    from agent_run.ticket_work import reconcile

                    before = state["ticket_work_queue"]
                    reconcile(state)
                    if not _same(before, state["ticket_work_queue"]):
                        mirrors.add("ticket_work_queue")
                if mirrors:
                    previous = work_records.baseline_for_save(c, run_id, state, mirrors & work_records.FIELDS, None)
                    work_records.save(c, run_id, state, previous, fields=mirrors & work_records.FIELDS)
                    audit.save(c, run_id, state, baseline, fields=mirrors & audit.FIELDS)
            self._local.work_view = None
            if isinstance(state, dict):
                self._attach_readers(run_id, state)
            if state.get("action_application_receipt") != baseline.get("action_application_receipt"):
                self._observe_application(c, run_id, state)
            return gates_changed

    def load_objects(
        self,
        run_id: str,
        *,
        excluded_fields: set[str] | None = None,
        related_tickets: bool = False,
        ticket_numbers: set[int] | None = None,
        scheduler_tickets: bool = False,
        thin_timeline: bool = False,
    ) -> tuple[dict[str, Any], dict[str, int]] | None:
        with self._reading() as c:
            if c is None or not self._require_run_query(c, run_id):
                return None
            excluded = set(excluded_fields or ())
            base, timelines = audit.load(
                c, run_id, fields=set(audit.FIELDS) - excluded, current_only=True, thin_timeline=thin_timeline
            )
            state, positions = work_records.load_objects(
                c,
                run_id,
                base,
                excluded_fields=excluded,
                related_tickets=related_tickets,
                ticket_numbers=ticket_numbers,
                scheduler_tickets=scheduler_tickets,
                max_bytes=self.max_bytes,
            )
            self._local.object_archives, self._local.object_attempts = work_records.object_scope(c, run_id, state)
            state = self._returned(run_id, state)
            self._local.work_timelines = timelines
            if thin_timeline:
                scoped = related_tickets or ticket_numbers is not None or scheduler_tickets
                queried = (work_records.CURRENT_FIELDS | audit.FIELDS) - excluded - sqlite_timeline.FIELDS
                # These current audit views carry only related historical
                # members; their writer still obtains its original baseline.
                queried -= {"resume_audit", "human_response_audit"}
                partial = {"retired_ticket_generations", "pending_ticket_retirements"} if scoped else set()
                if scoped:
                    queried |= {"ticket_jobs", "ticket_agent_invocations"}
                    if not scheduler_tickets:
                        partial |= {"ticket_jobs", "ticket_agent_invocations"}
                self._local.work_view = (
                    work_records.WorkBaseline(
                        run_id, copy_json_tree(state), c.total_changes,
                        frozenset(queried), frozenset(partial), c,
                    )
                    if self.in_transaction else None
                )
            return state, positions

    def load_fields(self, run_id: str, fields: set[str], *, thin_timeline: bool = False) -> dict[str, Any] | None:
        with self._reading() as c:
            if c is None or not self._require_run_query(c, run_id):
                return None
            base, timelines = audit.load(
                c, run_id, fields=fields & audit.FIELDS, current_only=True, thin_timeline=thin_timeline
            )
            state = self._returned(run_id, work_records.load_fields(c, run_id, base, fields))
            if thin_timeline:
                self._local.work_timelines = timelines
                self._local.work_view = (
                    work_records.WorkBaseline(run_id, copy_json_tree(state), c.total_changes)
                    if self.in_transaction else None
                )
            return state

    def load_paths(self, run_id: str, paths: set[tuple[str | int, ...]]) -> dict[str, Any] | None:
        if any(not path or any(not isinstance(part, str) for part in path) for path in paths):
            raise ValueError("Collection path projection requires mapping ancestors")
        with self._reading() as c:
            if c is None or not self._require_run_query(c, run_id):
                return None
            fields = {str(path[0]) for path in paths}
            numbers = {
                int(path[1])
                for path in paths
                if len(path) > 1 and path[0] in {"ticket_jobs", "ticket_agent_invocations"}
            }
            base, _ = audit.load(c, run_id, fields=fields & audit.FIELDS, current_only=True)
            selected = work_records.load_fields(c, run_id, base, fields, ticket_numbers=numbers if numbers else None)
            self._attach_readers(run_id, selected)
            result: dict[str, Any] = {}
            for path in cast(set[tuple[str, ...]], paths):
                source: Any = selected
                target = result
                for part in path[:-1]:
                    if not isinstance(source, dict) or part not in source:
                        break
                    source = source[part]
                    target = target.setdefault(part, {})
                else:
                    if isinstance(source, dict) and path[-1] in source:
                        target[path[-1]] = source[path[-1]]
            return self._returned(run_id, result)

    def save_objects(
        self,
        run_id: str,
        state: Mapping[str, Any],
        *,
        fields: set[str],
        history_positions: Mapping[str, int] | None = None,
        keyed_changes: Mapping[str, set[str]] | None = None,
        archive_scope: Mapping[str, tuple[int, int]] | None = None,
        timeline_updates: Mapping[str, dict[str, Any]] | None = None,
    ) -> None:
        if fields - _FIELDS or state.keys() - _FIELDS:
            raise ValueError("Unsupported Run fields")
        with self.atomic():
            c = self._local.connection
            if not self._require_run_query(c, run_id):
                raise ValueError("SQLite Run does not exist")
            timelines = {}
            if timeline_updates is not None:
                # Reuse the same transaction-local proof as Ticket work. An
                # earlier read, another Run or intervening SQL cannot supply
                # an append baseline, even when its visible facts match.
                baseline = self.current_work_baseline(run_id)
                timelines = self.work_timelines()
                if timeline_updates.keys() - timelines.keys() or any(
                    key in state or key in baseline for key in timelines
                ) or fields & (timelines.keys() - timeline_updates.keys()):
                    raise ValueError("SQLite object patch cannot replace unread Timeline fields")
            owned = fields & work_records.FIELDS
            before = work_records.read_baseline_for_save(
                c, run_id, state, fields - timelines.keys(), keyed_changes, archive_scope,
                getattr(self._local, "work_view", None),
            )
            if before is None:
                before = work_records.baseline_for_save(c, run_id, state, owned, keyed_changes, archive_scope)
                aux, _ = audit.load(c, run_id, fields=(fields & audit.FIELDS) - timelines.keys(), current_only=True)
                before.update(aux)
            work_records.save(c, run_id, state, before, fields=owned, keyed_changes=keyed_changes)
            audit.save(
                c, run_id, state, before,
                fields=(fields & audit.FIELDS) | set(timeline_updates or {}), timeline_updates=timeline_updates,
            )
            if timeline_updates is not None:
                for field, event in timeline_updates.items():
                    timelines[field] = sqlite_timeline.TimelineScope(
                        min(timelines[field].count + 1, sqlite_timeline.limit(field)), copy_json_tree(event)
                    )
                for field in (fields & sqlite_timeline.FIELDS) - timelines.keys():
                    if field in state:
                        events = state[field]
                        timelines[field] = sqlite_timeline.TimelineScope(
                            len(events), copy_json_tree(events[-1]) if events else None
                        )
                self._local.work_timelines = timelines
            self._local.work_view = None
            if isinstance(state, dict):
                self._attach_readers(run_id, state)
            if "action_application_receipt" in fields:
                self._observe_application(c, run_id, state)

    def project_saved_job_gates(
        self,
        run_id: str,
        state: dict[str, Any],
        *,
        fields: set[str],
        keyed_changes: Mapping[str, set[str]] | None = None,
    ) -> tuple[bool, set[str]]:
        from agent_run.sqlite_change_jobs import FIELDS as JOB_FIELDS

        if not fields & JOB_FIELDS:
            return False, set()
        if not self.in_transaction:
            raise RuntimeError("Job gate projection requires the save transaction")
        return work_records.project_saved_gates(
            self._local.connection, run_id, state, fields=fields, keyed_changes=keyed_changes
        )

    def append_array(self, run_id: str, field: str, items: list[Any]) -> None:
        with self.atomic():
            if field == "agent_invocation_history":
                work_records.save(self._local.connection, run_id, {field: items}, {}, fields={field})
            elif field in work_records.FIELDS:
                work_records.append_archive(self._local.connection, run_id, field, items)
            elif field in sqlite_timeline.FIELDS:
                sqlite_timeline.append(self._local.connection, run_id, field, items)
            else:
                raise ValueError("Unsupported append domain")
            self._local.work_view = None

    def has_ticket_supervision_work(self, run_id: str) -> bool:
        from agent_run.sqlite_run_records import has_supervision_work

        with self._reading() as c:
            return c is not None and self._require_run_query(c, run_id) and has_supervision_work(c, run_id)

    def ticket_pause_requested(self, run_id: str, ticket_number: int) -> bool:
        from agent_run.sqlite_run_records import pause_requested

        if type(ticket_number) is not int or ticket_number < 1:
            raise ValueError("Ticket number must be positive")
        with self._reading() as c:
            if c is None or not self._require_run_query(c, run_id):
                raise ValueError("Delivery Run disappeared at Ticket pause boundary")
            return pause_requested(c, run_id, ticket_number)

    def has(self, run_id: str) -> bool:
        with self._reading() as c:
            return c is not None and self._require_run_query(c, run_id)

    def list_ids(self, *, limit: int | None = None) -> list[str]:
        if limit is not None and limit < 0:
            raise ValueError("Run ID listing limit must not be negative")
        with self._reading() as c:
            if c is None:
                return []
            result = []
            query = "SELECT run_id FROM runs ORDER BY run_id" + (" LIMIT ?" if limit is not None else "")
            for row in bounded_query(c, query, (limit + 1,) if limit is not None else ()):
                result.append(row[0])
                if limit is not None and len(result) > limit:
                    raise ValueError("Run ID listing exceeds the requested limit")
            self._check_size({"run_ids": result})
            return result

    def find_ids(self, repository: str, parent_number: int, *, unfinished_only: bool) -> list[str]:
        with self._reading() as c:
            if c is None:
                return []
            query = "SELECT run_id FROM runs WHERE repository=? COLLATE NOCASE AND parent_number=?"
            if unfinished_only:
                query += " AND (status IS NULL OR status NOT IN ('completed','abandoned'))"
            query += " ORDER BY created_at DESC,run_id"
            result = []
            for row in bounded_query(c, query, (repository, parent_number)):
                result.append(row[0])
            self._check_size({"run_ids": result})
            return result

    def load_status(self, run_id: str, *, include_timing: bool = True) -> dict[str, Any] | None:
        from agent_run.sqlite_status_query import load

        with self._reading() as c:
            if c is None or not self._require_run_query(c, run_id):
                return None
            state = load(c, run_id, include_timing=include_timing)
            return self._returned(run_id, state)

    def load_history(self, run_id: str) -> dict[str, Any] | None:
        return self.load(run_id)

    def load_notifications(self, run_id: str, observation: dict[str, Any]) -> dict[str, Any] | None:
        from agent_run.sqlite_notification_query import load

        with self._reading() as c:
            if c is None or not self._require_run_query(c, run_id):
                return None
            return self._returned(run_id, load(c, run_id, observation))
