"""Post-commit native notification observer using the existing durable outbox.

This observer reads committed kernel facts. It neither calls Kernel nor infers a
new business state from a displayed Status document.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_run.storage.run_scope import selected_statement
from agent_run.contracts.repository import normalize_repository
from agent_run.storage import notification_outbox as outbox
from agent_run.errors import bounded_error, KernelError
from agent_run.notifications.cards import card
from agent_run.notifications.envelope import event_copy, event_envelope
from agent_run.adapters.notification_transport import send
from agent_run.runtime.process_identity import _process_start_token

from agent_run.queries.execution import execution_facts
from agent_run.notifications.event_projection import completion_context, event_state, project_event
from agent_run.queries.resume_facts import resume_facts
from agent_run.notifications.progress import obsolete_progress
from agent_run.notifications.status_comment import StatusComments
from agent_run.queries.status_comment import project_status, status_subjects
from agent_run.adapters.remote import PhysicalAPI
from agent_run.adapters.auth.credential_lifecycle import WORKER_CREDENTIAL_PROVIDER_OPERATION_TIMEOUT_SECONDS
from agent_run.queries.read_facts import material, read_boundaries
from agent_run.storage.sqlite import Storage, Transaction
from agent_run.storage.routing import open_run


CLOSE_TIMEOUT_SECONDS = 60.0
Send = Callable[..., Mapping[str, Any]]


def _pull_request_identity(url: object) -> tuple[str, int] | None:
    match = re.fullmatch(r"https://github[.]com/([^/]+/[^/]+)/pull/([1-9][0-9]*)", url) if isinstance(url, str) else None
    if match is None:
        return None
    try:
        return normalize_repository(match[1]), int(match[2])
    except ValueError:
        return None


class NativeNotifications:
    """Deliver only committed native events for one exact Run and owned directory."""

    def __init__(self, database: Path, state_root: Path, *, transport: Send = send,
                 exit_notice: Mapping[str, Any] | None = None, status_api: PhysicalAPI | None = None,
                 storage: Storage | None = None) -> None:
        if (database.is_symlink() or state_root.is_symlink()
                or not database.is_file() or not state_root.is_dir()
                or database.parent.resolve() != state_root.resolve()):
            raise ValueError("notification state root does not own this database")
        self.database = database
        self.state_root = state_root
        self.transport = transport
        self.exit_notice = dict(exit_notice) if exit_notice is not None else None
        self.exit_event_id: str | None = None
        self.storage = open_run(database, storage=storage)
        self._storage_options: dict[str, Any] = {} if storage is None else {"storage": self.storage}
        with self.storage.transaction(write=False) as tx:
            self.run_id, self.binding, self.config, self.state = self._run(tx)
        self.feishu_enabled = self.config.get("enabled") is True
        self.status = StatusComments(database, **self._storage_options)
        self.status_api = status_api
        self.status_failure_reason: str | None = None
        self._status_seen: dict[str, tuple[str, str]] = {}
        try:
            status_subjects(database, **self._storage_options)
            self.status_enabled = True
        except (OSError, ValueError, KernelError, sqlite3.Error):
            self.status_enabled = False
        self.enabled = self.feishu_enabled or self.status_enabled
        self._status_dirty = self.status_enabled
        self._status_cancel = threading.Event()
        self._status_preempt = threading.Event()
        self.condition = threading.Condition()
        self.cancel = threading.Event()
        self.owner = {"pid": os.getpid(), "token": _process_start_token(os.getpid()),
                      "sender": uuid.uuid4().hex}
        self.closing = False
        self.closed = False
        self.failure_reason: str | None = None
        self.thread: threading.Thread | None = None
        self._more = False
        self._shutdown = False
        self._recovery_requested = False
        self._fault_serial = 0
        self._pending_serial = 0  # Ordered by the existing journal transaction lock.
        self._woken_serial = 0
        if self.enabled:
            self.observe_committed()
            self.thread = threading.Thread(target=self._work, name="kernel-notifications", daemon=True)
            self.thread.start()

    @staticmethod
    def _run(tx: Transaction) -> tuple[str, tuple[str, int], dict[str, Any], dict[str, Any]]:
        state = event_state(tx)
        inputs = tx.one("SELECT subject,material_id FROM run_inputs WHERE run_id=?", (state["run_id"],))
        value = json.loads(tx.get(inputs["material_id"], inputs["subject"]))
        if not isinstance(value, dict) or not isinstance(value.get("notifications"), dict):
            raise ValueError("notification configuration is not a frozen Run input")
        config = value["notifications"]
        if config.get("enabled") is True and state["language"] not in {"zh", "en"}:
            raise ValueError("enabled notification requires a known Run language")
        return state["run_id"], (state["repository"], state["parent"]["number"]), config, state

    def _same_binding(self, run_id: str, binding: tuple[str, int],
                      config: dict[str, Any], state: dict[str, Any]) -> bool:
        # A refreshed Parent title is current card copy, not a recipient or
        # Run identity change. The frozen notification inputs remain exact.
        return ((run_id, binding, config, state["language"], state["created_at"])
                == (self.run_id, self.binding, self.config,
                    self.state["language"], self.state["created_at"]))

    def _page(self, after: int) -> tuple[list[dict[str, Any]], bool, int]:
        with self.storage.transaction(write=False) as tx:
            run_id, binding, config, state = self._run(tx)
            if not self._same_binding(run_id, binding, config, state):
                raise ValueError("notification Run or recipient binding changed")
            history = tx.rows(*selected_statement(tx, 'SELECT position,subject,kind,identity,generation,created_at,material_id,phase,purpose FROM history WHERE position>? ORDER BY position LIMIT 16',
                          'SELECT position,subject,kind,identity,generation,created_at,material_id,phase,purpose FROM history WHERE run_id=? AND position>? ORDER BY position LIMIT 16', (after,)), limit=16)
            events: list[dict[str, Any]] = []
            for row in history:
                events.extend(projected.content for projected in project_event(
                    tx, dict(row), state, self.config.get("mode", "concise")) if projected.visible)
            return events, len(history) == 16, history[-1]["position"] if history else after

    @staticmethod
    def _gate_slot(subject: str | None, generation: int | None, reason: str) -> str:
        return hashlib.sha256(json.dumps([subject, generation, reason]).encode()).hexdigest()

    def _terminal_event(self, tx: Transaction, view: dict[str, Any]) -> dict[str, Any] | None:
        """Describe a committed lifecycle result, without granting completion."""
        if not view["is_terminal"]:
            return None
        if view["status"] == "abandoned":
            action = view["abandonment"]
            if action is None or action["status"] != "complete":
                raise ValueError("abandoned notification has no completed original Action")
            return event_envelope(self.state, "boundary", ["abandoned", action["action"]],
                                  event_copy(self.state, "abandoned"), "yellow",
                                  current=True, terminal=True, status_code="abandoned",
                                  execution_timing_complete=False)
        if view["status"] != "completed":
            return None
        closures = view["closures"]
        if len(closures) != 1 or material(tx, closures[0]["receipt_id"], closures[0]["subject"]).get("disposition") != "target":
            raise ValueError("completed notification has no unique original close receipt")
        closed = closures[0]
        number = closed["pr_number"]
        if type(number) is not int or number <= 0:
            raise ValueError("completed notification has no original Pull Request")
        cleanup = view["delivery_cleanup"]
        pending = cleanup is not None and cleanup["status"] == "cleanup_pending"
        status = "completed_cleanup" if pending else "completed"
        summary = event_copy(self.state, "separator").join((
            event_copy(self.state, "merged", number=number), event_copy(self.state, "closed")))
        if pending:
            summary += event_copy(self.state, "cleanup_summary")
        return event_envelope(self.state, "boundary", [status, closed["effect_id"], closed["receipt_id"]],
                              event_copy(self.state, "cleanup" if pending else "completed"),
                              "yellow" if pending else "green", current=True, terminal=True,
                              status_code="completed", summary=summary,
                              url=f"https://github.com/{self.binding[0]}/pull/{number}",
                              execution_timing_complete=False, **completion_context(view, self.state))

    def _boundaries(self, document: dict[str, Any]) -> bool:
        """Track notification occurrences, not an alternate writable Run state.

        Replaying original committed gate snapshots detects leave/reentry even
        between observer polls. Only gates still current in this same snapshot
        receive cards; old pending occurrences retain their original budgets.
        """
        slots = document.setdefault("native_boundary_slots", {})
        cursor = document.get("native_boundary_cursor", 0)
        if not isinstance(slots, dict) or type(cursor) is not int or cursor < 0:
            raise ValueError("invalid native boundary receipt")

        def replace_scope(subject: str | None, generation: int | None, reasons: list[str]) -> None:
            desired = {self._gate_slot(subject, generation, reason) for reason in reasons}
            for key, value in list(slots.items()):
                if value["subject"] == subject and key not in desired:
                    del slots[key]
            for key in desired:
                if key not in slots:
                    document["sequence"] += 1
                    slots[key] = {"subject": subject, "generation": generation,
                                  "sequence": document["sequence"]}

        with self.storage.transaction(write=False) as tx:
            if not self._same_binding(*self._run(tx)):
                raise ValueError("notification Run or recipient binding changed")
            view = read_boundaries(tx, time.time())
            terminal = self._terminal_event(tx, view)
            if terminal is not None and view["status"] == "completed":
                closure = view["closures"][0]
                original = tx.one(*selected_statement(tx, "SELECT * FROM history WHERE kind='effect_consumed' AND identity=? AND material_id=?",
                               "SELECT * FROM history WHERE run_id=? AND kind='effect_consumed' AND identity=? AND material_id=?", (closure["effect_id"], closure["receipt_id"])))
                timing = execution_facts(tx, dict(original), whole_run=True)
                total = timing.pop("execution_seconds", None)
                terminal.update(timing, total_seconds=total)
            highwater = view["highwater"]
            if cursor < highwater:
                rows = tx.rows(*selected_statement(tx, 'SELECT position,subject,generation,phase,status FROM history WHERE position>? AND position<=? ORDER BY position LIMIT 128',
                           'SELECT position,subject,generation,phase,status FROM history WHERE run_id=? AND position>? AND position<=? ORDER BY position LIMIT 128', (cursor, highwater)), limit=128)
                if not rows:
                    raise ValueError("notification history cursor has no committed continuation")
                for row in rows:
                    if row["status"] is None:
                        continue
                    status = json.loads(row["status"])
                    global_reason = "operator_stopped" if status["stopped"] else status["blocked"]
                    replace_scope(None, None, [global_reason] if global_reason else [])
                    if view["generations"].get(row["subject"]) == row["generation"]:
                        reasons = list(status["gates"])
                        if row["phase"] == "approval":
                            reasons.append("final_approval")
                        replace_scope(row["subject"], row["generation"], reasons)
                cursor = rows[-1]["position"]
            actions = {self._gate_slot(item["subject"], item.get("generation"), item["reason"]): item
                       for item in view["actions"]}
            resumes: dict[str, dict[str, Any]] = {}
            for key, action in actions.items():
                if action["subject"] is None:
                    continue
                gate_history = tx.rows(*selected_statement(tx, "SELECT h.* FROM history h JOIN gates g ON g.subject=h.subject AND g.code=h.identity AND g.evidence IS h.material_id WHERE h.kind='paused' AND h.subject=? AND h.generation=? AND h.identity=? ORDER BY h.position DESC LIMIT 1",
                                   "SELECT h.* FROM history h JOIN gates g ON g.run_id=h.run_id AND g.subject=h.subject AND g.code=h.identity AND g.evidence IS h.material_id WHERE h.run_id=? AND h.kind='paused' AND h.subject=? AND h.generation=? AND h.identity=? ORDER BY h.position DESC LIMIT 1", (action["subject"], action.get("generation"), action["reason"])))
                if gate_history:
                    resumes[key] = resume_facts(tx, dict(gate_history[0]))
            caught_up = cursor == highwater
            # Never send an intermediate historical gate as a current card.
            # Each call reads at most 128 snapshots, then persists its cursor.
            if caught_up:
                for key in list(slots):
                    if key not in actions:
                        del slots[key]
                for key, action in actions.items():
                    if key not in slots:
                        document["sequence"] += 1
                        slots[key] = {"subject": action["subject"], "generation": action.get("generation"),
                                      "sequence": document["sequence"]}
            document["native_boundary_cursor"] = cursor
            document["native_boundary_catching_up"] = not caught_up
            self._compress_progress(tx, document, view, added=highwater > document.get("native_cursor", 0))
        current = {key: value["sequence"] for key, value in slots.items()}
        for item in list(document["pending"]):
            event = item["event"]
            if event.get("terminal"):
                if terminal is None or event["id"] != terminal["id"]:
                    outbox.cancel_item(document, item, "recorded lifecycle result changed")
                continue
            if (event.get("kind") == "boundary" and event.get("current")
                    and (event.get("slot_id") not in actions or event.get("slot_id") not in current
                         or event.get("occurrence") != current[event["slot_id"]])):
                outbox.cancel_item(document, item, "recorded operator gate changed")
        document["active"] = list(actions)
        if not caught_up:
            return True
        for key, action in actions.items():
            occurrence = current[key]
            if slots[key].get("enqueued"):
                continue
            reason = action["reason"]
            title_key = ("stopped" if reason == "operator_stopped" else "approval"
                         if reason == "final_approval" else "human_title"
                         if reason == "human_blocker" else "execution_failed"
                         if reason == "execution_failed" else "budget" if "budget" in reason else "timeout"
                         if reason == "supervision_timeout" else "requeue" if reason == "requeue_required"
                         else "human_title")
            event = event_envelope(self.state, "boundary", [key, occurrence],
                                   event_copy(self.state, title_key),
                                   "red" if reason in {"execution_failed", "supervision_timeout"} else "yellow",
                                   current=True,
                                   slot_id=key, occurrence=occurrence, work_subject=action["subject"],
                                   generation=action.get("generation"), object=action["object"],
                                   scope=action["scope"],
                                   summary="; ".join(str(value) for value in action["reasons"]),
                                   next_step=action["next_action"])
            if reason == "final_approval":
                owner = next(job for job in view["subjects"] if job["subject"] == action["subject"])
                number = owner["pr_number"]
                if type(number) is int and number > 0:
                    event.update(approval_pr=number,
                                 url=f"https://github.com/{self.state['repository']}/pull/{number}")
            facts = resumes.get(key)
            if facts:
                event.update(facts)
                event["title"] = event_copy(self.state, "resume_" + facts["resume_outcome"])
                event["color"] = "red" if facts["resume_outcome"] == "failed" else "yellow"
                event["summary"] = (event_copy(self.state, "resume_" + facts["resume_outcome"])
                                    + event_copy(self.state, "separator") + event["summary"])
            outbox.enqueue(document, event, time.time())
            slots[key]["enqueued"] = True
        if terminal is not None:
            receipts = document.setdefault("native_terminal_receipts", [])
            if not isinstance(receipts, list) or any(not isinstance(key, str) for key in receipts):
                raise ValueError("invalid native terminal notification receipts")
            if terminal["id"] not in receipts:
                outbox.enqueue(document, terminal, time.time())
                receipts.append(terminal["id"])
                del receipts[:-outbox.MAX_RECORDS]
        return False

    def _compress_progress(self, tx: Transaction, document: dict[str, Any], view: dict[str, Any], *, added: bool) -> None:
        """Retire delivery backlog from this exact read snapshot, preserving results."""
        ordinary = [item for item in document["pending"] if not item.get("important")
                    and not item["event"].get("current") and not item["event"].get("terminal")
                    and item["event"]["kind"] != "executor_exit" and item["outcome"] != "sending"]
        pressure = sum(not item["event"].get("current") for item in document["pending"]) > outbox.MAX_PENDING - 17
        if (document.get("unavailable_reason") or pressure
                or added and any(item["outcome"] in {"failed", "unknown"} for item in ordinary)):
            obsolete = obsolete_progress(tx, [item["event"] for item in ordinary],
                                         self.config.get("mode", "concise"),
                                         current_boundary=bool(view["actions"] or view["is_terminal"] or view["abandonment"] is not None))
            for item in ordinary:
                if item["event"]["id"] in obsolete:
                    outbox.cancel_item(document, item, "original progress no longer current")
            # Capacity is a delivery policy, not evidence that an unknown
            # source became obsolete. Keep its original result and budget.
            if pressure:
                for item in ordinary:
                    if sum(not entry["event"].get("current") for entry in document["pending"]) <= outbox.MAX_PENDING - 17:
                        break
                    if item in document["pending"]:
                        outbox.cancel_item(document, item, "pending capacity")

    @staticmethod
    def _coalesce_final_pr(document: dict[str, Any]) -> None:
        # Only an already-enqueued current approval card with the exact PR identity
        # replaces an unsent creation card. Sending/unknown payloads keep their
        # original identity and delivery budget.
        approvals = {(item["event"]["work_subject"], item["event"]["generation"], identity)
                     for item in document["pending"] if item["event"]["kind"] == "boundary" and item["event"].get("current")
                     and type(item["event"].get("approval_pr")) is int
                     and (identity := _pull_request_identity(item["event"].get("url"))) is not None
                     and identity[1] == item["event"]["approval_pr"]
                     and not item.get("cancelled") and not item.get("exhausted")}
        for item in list(document["pending"]):
            event = item["event"]
            identity = _pull_request_identity(event.get("url"))
            if (event["kind"] == "pr_created" and item["attempts"] == 0 and item["outcome"] == "pending"
                    and identity is not None
                    and (event.get("work_subject"), event.get("generation"), identity) in approvals):
                outbox.cancel_item(document, item, "included in current Final PR approval")

    def observe_committed(self) -> None:
        """Observe healthy facts, or request one sender-owned fault recovery."""
        if not self.enabled or self.closed or self._shutdown:
            return
        with self.condition:
            if self.status_enabled:
                self._status_dirty = True
                self.condition.notify_all()
            if self.failure_reason is not None:
                self._recovery_requested = True
                self.condition.notify_all()
                return
        if self.feishu_enabled:
            self._observe_committed()

    def _send_status(self) -> None:
        """Coalesce display only; never infer business progress or schedule work."""
        for subject in status_subjects(self.database, **self._storage_options):
            if self._shutdown or self._status_cancel.is_set():
                return
            projection = project_status(self.database, subject, **self._storage_options)
            fingerprint = (projection.target.key, hashlib.sha256(projection.body.encode()).hexdigest())
            if self._status_seen.get(subject) == fingerprint:
                continue
            # A successful readback may settle an older sent intent. One more
            # opportunity can then apply current copy, without replaying it.
            for _ in range(2):
                result = self.status.deliver(subject, api=self.status_api,
                    cancelled=lambda: self._status_cancel.is_set() or self._status_preempt.is_set())
                if self._status_preempt.is_set():
                    # A queued important notification keeps its original
                    # window. Interrupted Status retains its intent for later
                    # readback, and is not cached as already observed.
                    with self.condition:
                        self._status_dirty = True
                    return
                if result != "confirmed":
                    break
            self._status_seen[subject] = fingerprint
        self.status_failure_reason = None

    def _wake_pending(self, document: dict[str, Any], serial: int) -> None:
        """Give existing important Feishu claims priority over display reads."""
        pending = document["pending"]
        if self.exit_notice is not None:
            pending = [item for item in pending if item["event"]["id"] == self.exit_event_id]
        with self.condition:
            if serial < self._woken_serial:
                return  # A newer committed snapshot already owns the signal.
            self._woken_serial = serial
            if any(item.get("important") and not item.get("cancelled") for item in pending):
                self._status_preempt.set()
            else:
                self._status_preempt.clear()
            self.condition.notify_all()

    def _pause(self, error: Exception) -> None:
        reason = bounded_error(str(error))
        # Every journal fault write/clear follows condition -> outbox order.
        # A late report or recovery cannot overwrite a newer fault receipt.
        with self.condition:
            self.failure_reason = reason
            self._fault_serial += 1
            self.cancel.set()
            self.condition.notify_all()
            try:
                existed = outbox.notification_path(self.state_root, self.run_id).exists()
                with outbox.transaction(self.state_root, self.run_id) as document:
                    if (existed and not document.get("native_run_start")
                            and document.get("native_binding") != [self.run_id, *self.binding]):
                        return
                    if not existed:
                        document["native_binding"] = [self.run_id, *self.binding]
                    document["unavailable_reason"] = reason
                    document["delivery_error"] = {"reason": reason, "observed_at": time.time()}
            except Exception:
                pass  # Best effort after the original transaction rolled back.
        print(event_copy(self.state, "unavailable_development_continues", reason=reason), file=sys.stderr)

    @staticmethod
    def _enqueue_history(document: dict[str, Any], event: dict[str, Any], now: float) -> None:
        """Observe one fact again without assigning it another delivery budget."""
        event_id = event["id"]
        pending = next((item for item in document["pending"] if item["event"]["id"] == event_id), None)
        if pending is not None:
            # Once a physical attempt exists, its payload and idempotency key
            # stay paired. An unsent planned card may become its active view.
            if pending["attempts"] == 0 and pending["outcome"] == "pending":
                pending["event"] = event
        elif (event_id in document["seen"] or event_id in document["milestones"]
              or any(item["id"] == event_id for item in document["records"])):
            return
        else:
            outbox.enqueue(document, event, now)
            document["seen"].append(event_id)
            del document["seen"][:-outbox.MAX_RECORDS]
        # These identities are bounded by the Run's fixed subject set, and
        # survive rolling result history just like the original first nodes.
        if (event["kind"] in {"parent_started", "ticket_started", "acceptance_started", "acceptance_repair"}
                and event_id not in document["milestones"]):
            document["milestones"].append(event_id)

    def _exit_validity(self) -> dict[str, tuple[dict[str, Any], bool]]:
        # Startup Host identity may require a read-only manager call. Keep it
        # outside both the journal claim and condition locks.
        document = outbox.read_notifications(self.state_root, self.run_id)
        return {item["event"]["id"]: (item["event"], self._exit_current(item["event"]))
                for item in document.get("pending", []) if item["event"]["kind"] == "executor_exit"}

    @staticmethod
    def _cancel_exit_notices(document: dict[str, Any],
                             validity: dict[str, tuple[dict[str, Any], bool]]) -> None:
        for item in list(document["pending"]):
            event = item["event"]
            if validity.get(event["id"]) == (event, False):
                outbox.cancel_item(document, item, "original Host exit no longer current")

    def _exit_current(self, event: Mapping[str, Any]) -> bool:
        from agent_run.queries.host_exit import host_exit_current
        evidence = event.get("exit_evidence")
        return isinstance(evidence, dict) and host_exit_current(self.database, evidence, **self._storage_options)

    def _observe_exit_notice(self) -> bool:
        from agent_run.notifications.event_projection import host_exit_event
        assert self.exit_notice is not None
        event = host_exit_event(self.database, self.exit_notice, **self._storage_options)
        validity = self._exit_validity()
        with outbox.transaction(self.state_root, self.run_id) as document:
            binding = [self.run_id, *self.binding]
            if (document.get("native_binding") not in (None, binding)
                    or (not document.get("native_run_start") and document.get("native_binding") is None
                        and any(document[key] for key in ("pending", "records", "seen")))):
                raise ValueError("existing notification journal has no native receipt")
            document["native_binding"] = binding
            outbox.settle(document, time.time())
            self._cancel_exit_notices(document, validity)
            if event is not None:
                self.exit_event_id = event["id"]
                if (event["id"] not in document["seen"]
                        and not any(item["event"]["id"] == event["id"] for item in document["pending"])
                        and not any(item["id"] == event["id"] for item in document["records"])):
                    prior = document.get("executor_exit")
                    if prior is not None and (not isinstance(prior, dict) or prior.get("run_id") != self.run_id
                                              or type(prior.get("epoch")) is not int):
                        raise ValueError("invalid original Host exit receipt")
                    if prior is None or prior["epoch"] < self.exit_notice["epoch"]:
                        document["executor_exit"] = dict(self.exit_notice)
                    outbox.enqueue(document, event, time.time())
                    if sum(not entry["event"].get("current") for entry in document["pending"]) > outbox.MAX_PENDING:
                        item = document["pending"][-1]
                        item.update(outcome="failed", reason="notification queue full before delivery",
                                    exhausted=True, exhaustion_reason="pending_capacity")
                        outbox.retire(document, item)
                    document["seen"].append(event["id"])
                    del document["seen"][:-outbox.MAX_RECORDS]
            self._pending_serial += 1
            serial = self._pending_serial
        self._wake_pending(document, serial)
        self._more = False
        return True

    def _observe_committed(self) -> bool:
        """Read only committed facts; claim no transport while a kernel lock is held."""
        if not self.enabled or self.closed or self._shutdown:
            return False
        if not self.feishu_enabled:
            return True
        try:
            if self.exit_notice is not None:
                return self._observe_exit_notice()
            for _ in range(64):
                validity = self._exit_validity()
                with outbox.transaction(self.state_root, self.run_id) as document:
                    outbox.settle(document, time.time())
                    self._cancel_exit_notices(document, validity)
                    if not document.get("native_run_start"):
                        journal = outbox.notification_path(self.state_root, self.run_id)
                        if ((journal.exists() or journal.is_symlink())
                                and document.get("native_binding") != [self.run_id, *self.binding]):
                            raise ValueError("existing notification journal has no native receipt")
                        document["native_binding"] = [self.run_id, *self.binding]
                        started = datetime.fromtimestamp(self.state["created_at"], UTC).isoformat()
                        event = event_envelope(self.state, "run_start", self.run_id,
                                               event_copy(self.state, "run_start"), started_at=started)
                        outbox.enqueue(document, event, time.time())
                        document["native_run_start"] = True
                    boundary_more = self._boundaries(document)
                    full = sum(not item["event"].get("current") for item in document["pending"]) > outbox.MAX_PENDING - 17
                    cursor = document.get("native_cursor", 0)
                    if type(cursor) is not int or cursor < 0:
                        raise ValueError("invalid native notification cursor")
                    events, more, position = (([], True, cursor) if full or boundary_more else self._page(cursor))
                    for event in events:
                        self._enqueue_history(document, event, time.time())
                    document["native_cursor"] = position
                    self._coalesce_final_pr(document)
                    if events and any(item["outcome"] in {"failed", "unknown"} and not item.get("important")
                                      for item in document["pending"]):
                        with self.storage.transaction(write=False) as tx:
                            if not self._same_binding(*self._run(tx)):
                                raise ValueError("notification Run or recipient binding changed")
                            self._compress_progress(tx, document, read_boundaries(tx, time.time()), added=True)
                    self._pending_serial += 1
                    serial = self._pending_serial
                self._wake_pending(document, serial)
                self._more = more
                if not more or full or boundary_more:
                    return True
            return True
        except Exception as error:
            self._pause(error)
            return False

    def _work(self) -> None:
        active_event_id: str | None = None
        active_result: dict[str, Any] | None = None
        active_attempt: int | None = None
        returned_at: float | None = None
        while not self._shutdown:
            try:
                with self.condition:
                    status_dirty = self._status_dirty and not self._status_preempt.is_set()
                    if status_dirty:
                        self._status_dirty = False
                if status_dirty:
                    try:
                        self._send_status()
                    except Exception:
                        # This is the asynchronous channel boundary, not a
                        # success classification in the public Status port.
                        self.status_failure_reason = "status_sender_failed"
                        print("Agent Run Status unavailable; business continues", file=sys.stderr)
                if not self.feishu_enabled:
                    with self.condition:
                        if self._shutdown:
                            return
                        if self._status_dirty:
                            continue
                        if self.closing:
                            return
                        self.condition.wait()
                    continue
                with self.condition:
                    while (self.failure_reason is not None and not self._recovery_requested
                           and not self.closing and not self._shutdown):
                        self.condition.wait()
                    if self._shutdown:
                        return
                    if self._status_dirty and not self._status_preempt.is_set():
                        continue
                    recovering = self.failure_reason is not None
                    if recovering and not self._recovery_requested:
                        return  # A normal close already made its one recovery attempt.
                    serial = self._fault_serial
                    self._recovery_requested = False
                # A returned physical result survives journal failure in this
                # one active slot. Persist it before recovering or claiming.
                if active_event_id is not None:
                    if active_result is not None:
                        assert returned_at is not None
                        self._finish_result(active_event_id, active_result, returned_at)
                    else:
                        # The claim may have reached os.replace before a final
                        # fsync error. No transport call occurred in this path.
                        assert active_attempt is not None and returned_at is not None
                        self._finish_not_sent(active_event_id, active_attempt, returned_at)
                    active_event_id, active_result, active_attempt, returned_at = None, None, None, None
                if not self._observe_committed():
                    continue
                validity = self._exit_validity()
                with self.condition:
                    if self._shutdown:
                        return
                    if serial != self._fault_serial:
                        continue
                    if not recovering and (self.failure_reason is not None or self.cancel.is_set()):
                        continue
                    with outbox.transaction(self.state_root, self.run_id) as document:
                        document.pop("unavailable_reason", None)
                        outbox.settle(document, time.time())
                        self._cancel_exit_notices(document, validity)
                        all_pending = document["pending"]
                        pending = [item for item in all_pending
                                   if self.exit_notice is None or item["event"]["id"] == self.exit_event_id]
                        inflight = any(item["outcome"] == "sending" for item in all_pending)
                        item = None if inflight else next((item for item in sorted(pending, key=lambda entry: not entry.get("important")) if not item.get("cancelled")
                                                            and (item["event"]["kind"] != "executor_exit"
                                                                 or validity.get(item["event"]["id"]) == (item["event"], True))
                                                            and not (document.get("native_boundary_catching_up")
                                                                     and item["event"].get("current")
                                                                     and item["event"].get("kind") == "boundary")
                                                            and item["next_attempt_at"] <= time.time()), None)
                        if item is not None:
                            item.update(owner=self.owner, outcome="sending")
                            item["attempts"] += 1
                            item["attempt_history"].append({"number": item["attempts"],
                                                            "started_at": time.time(), "outcome": "sending"})
                            event = dict(item["event"])
                            active_event_id = event["id"]
                            active_attempt = item["attempts"]
                            deadline_at = float(item["deadline_at"])
                        else:
                            event = None
                            deadline_at = 0.0
                            wait = min((max(0.0, min(entry["next_attempt_at"], entry["deadline_at"]) - time.time())
                                        for entry in pending), default=0.1)
                    self.failure_reason = None
                    self.cancel.clear()
                    if event is None:
                        if self._status_dirty and not self._status_preempt.is_set():
                            continue
                        if self.closing and not pending and not self._more:
                            return
                        self.condition.wait(timeout=(max(0.01, min(wait, 0.1))
                                                     if pending or self._more else None))
                        continue
                identity = hashlib.sha256(event["id"].encode()).hexdigest()[:32]
                result: Mapping[str, Any]
                try:
                    with self.storage.transaction(write=False) as tx:
                        if not self._same_binding(*self._run(tx)):
                            raise ValueError("notification Run or recipient binding changed")
                except Exception as error:
                    result = {"outcome": "failed", "reason": bounded_error(str(error))}
                else:
                    if event["kind"] == "executor_exit" and not self._exit_current(event):
                        result = {"outcome": "failed", "reason": "original Host exit no longer current",
                                  "not_sent": True}
                    else:
                        try:
                            options: dict[str, Any] = {"timeout_seconds": max(0.0, deadline_at - time.time())}
                            if event["kind"] == "executor_exit":
                                exit_event = event
                                options["is_current"] = lambda: self._exit_current(exit_event)
                            result = self.transport(self.config, card(event), identity, self.cancel, **options)
                        except Exception as error:
                            from agent_run.queries.host_exit import HostExitUnavailable
                            if event["kind"] == "executor_exit" and isinstance(error, HostExitUnavailable):
                                result = {"outcome": "failed", "reason": bounded_error(str(error)), "not_sent": True}
                            else:
                                result = {"outcome": "unknown", "reason": bounded_error(str(error))}
                returned_at = time.time()
                active_result = dict(result)
                assert active_event_id is not None
                self._finish_result(active_event_id, active_result, returned_at)
                active_event_id, active_result, active_attempt, returned_at = None, None, None, None
            except Exception as error:
                # Keep an exact uncertain journal claim; it is known unsent,
                # unlike an external request whose ACK was actually lost.
                if active_event_id is not None and returned_at is None:
                    returned_at = time.time()
                self._pause(error)

    def _finish_result(self, event_id: str, result: dict[str, Any], returned_at: float) -> None:
        with outbox.transaction(self.state_root, self.run_id) as document:
            claim = next((entry for entry in document["pending"] if entry["event"]["id"] == event_id
                          and entry.get("owner") == self.owner), None)
            if claim is not None:
                not_sent = result.get("not_sent") is True
                outbox.finish_attempt(claim, {key: value for key, value in result.items() if key != "not_sent"}, returned_at)
                if not_sent:
                    claim["attempt_history"][-1]["not_sent"] = True
                outbox.settle(document, time.time())

    def _finish_not_sent(self, event_id: str, attempt: int, failed_at: float) -> None:
        with outbox.transaction(self.state_root, self.run_id) as document:
            claim = next((entry for entry in document["pending"] if entry["event"]["id"] == event_id
                          and entry.get("owner") == self.owner and entry["attempts"] == attempt
                          and entry["outcome"] == "sending"), None)
            if claim is not None:
                outbox.finish_attempt(claim, {"outcome": "failed",
                                             "reason": "journal claim interrupted before transport"}, failed_at)
                claim["attempt_history"][-1]["not_sent"] = True
                outbox.settle(document, time.time())

    def close(self, *, drain: bool = True, timeout_seconds: float = CLOSE_TIMEOUT_SECONDS) -> None:
        if not self.enabled or self.closed:
            return
        with self.condition:
            self.closing = True
            if drain:
                self._recovery_requested = True
                self._status_dirty = self.status_enabled
            else:
                self._shutdown = True
                self.cancel.set()
                self._status_cancel.set()
            self.condition.notify_all()
        if self.thread is not None and drain:
            self.thread.join(timeout=max(0.0, min(timeout_seconds, CLOSE_TIMEOUT_SECONDS)))
        with self.condition:
            self._shutdown = True
            self.cancel.set()
            self._status_cancel.set()
            self.condition.notify_all()
        if self.thread is not None:
            # Existing provider preparation has at most signing + HTTP work;
            # allow its own bounded cleanup after cancelling physical commands.
            grace = 2 * WORKER_CREDENTIAL_PROVIDER_OPERATION_TIMEOUT_SECONDS + 5 if self.status_enabled else 2
            self.thread.join(timeout=grace)
        if drain and self.failure_reason is not None:
            try:
                with outbox.transaction(self.state_root, self.run_id) as document:
                    if (document.get("native_run_start")
                            or document.get("native_binding") == [self.run_id, *self.binding]):
                        for item in document["pending"]:
                            if item["attempts"] == 0:
                                item.update(outcome="failed", reason=self.failure_reason)
                        document["unavailable_reason"] = self.failure_reason
                        document["delivery_error"] = {"reason": self.failure_reason, "observed_at": time.time()}
            except Exception:
                pass
        self.closed = self.thread is None or not self.thread.is_alive()
        if not self.closed:
            self.status_failure_reason = "status_sender_cleanup_unproven"
            print("Notification sender cleanup unproven", file=sys.stderr)


def notify_committed(database: Path, *, timeout_seconds: float = 1.0, storage: Storage | None = None) -> None:
    """One finite post-commit opportunity through the existing sender."""
    observer: NativeNotifications | None = None
    options: dict[str, Any] = {} if storage is None else {"storage": storage}
    try:
        observer = NativeNotifications(database, database.parent, **options)
        observer.close(timeout_seconds=timeout_seconds)
    except Exception:
        print("Notification observation unavailable; business continues", file=sys.stderr)
    finally:
        if observer is not None and not observer.closed:
            observer.close(drain=False)


def deliver_host_exit_notice(database: Path, state_root: Path, notice: Mapping[str, Any], *, storage: Storage | None = None) -> None:
    """Drain one exact Host-exit receipt through the existing bounded sender."""
    options: dict[str, Any] = {} if storage is None else {"storage": storage}
    observer = NativeNotifications(database, state_root, exit_notice=notice, **options)
    observer.close()
