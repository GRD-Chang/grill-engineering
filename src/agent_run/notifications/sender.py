"""Post-commit native notification observer using the existing durable outbox.

This observer reads committed kernel facts and frozen operation bindings. It
never advances Core or infers business state from a displayed Status document.
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
from time import monotonic
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any, Literal

from agent_run.storage.run_scope import run_statement
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
from agent_run.core.kernel import Kernel
from agent_run.adapters.remote import PhysicalAPI
from agent_run.adapters.auth.credential_lifecycle import WORKER_CREDENTIAL_PROVIDER_OPERATION_TIMEOUT_SECONDS
from agent_run.queries.read_facts import material, read_boundaries
from agent_run.storage.sqlite import Storage, Transaction
from agent_run.storage.routing import open_run


CLOSE_TIMEOUT_SECONDS = 60.0
Send = Callable[..., Mapping[str, Any]]


@dataclass
class CurrentCall:
    """One physical operation; keep its receipt until original claim settlement."""

    kind: Literal["feishu", "status"]
    claim: tuple[str, int] | None = None
    thread: threading.Thread | None = None
    cancel: threading.Event = field(default_factory=threading.Event)
    result: tuple[dict[str, Any], float, Exception | None] | None = None
    settlement_deferred: bool = False


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
                 storage: Storage) -> None:
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
        self._storage_options: dict[str, Any] = {"storage": self.storage}
        with self.storage.transaction(write=False) as tx:
            self.run_id, self.binding, self.config, self.state = self._run(tx)
        self.feishu_enabled = self.config.get("enabled") is True
        self.status = StatusComments(state_root,
            read_projection=partial(project_status, database, storage=self.storage),
            read_binding=Kernel._from_storage(self.storage).operation_binding)
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
        self.condition = threading.Condition()
        self.owner = {"pid": os.getpid(), "token": _process_start_token(os.getpid()),
                      "sender": uuid.uuid4().hex}
        self.failure_reason: str | None = None
        self.thread: threading.Thread | None = None
        self._more = False
        self._mode: Literal["running", "drain", "cancel", "cancel_after_drain"] = "running"
        self._refresh_dirty = False
        self._call: CurrentCall | None = None
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
            history = tx.rows(*run_statement(tx, 'SELECT run_id,position,subject,kind,identity,generation,created_at,material_id,phase,purpose FROM history WHERE run_id=? AND position>? ORDER BY position LIMIT 16', (after,)), limit=16)
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
                original = tx.one(*run_statement(tx, "SELECT * FROM history WHERE run_id=? AND kind='effect_consumed' AND identity=? AND material_id=?", (closure["effect_id"], closure["receipt_id"])))
                timing = execution_facts(tx, dict(original), whole_run=True)
                total = timing.pop("execution_seconds", None)
                terminal.update(timing, total_seconds=total)
            highwater = view["highwater"]
            if cursor < highwater:
                rows = tx.rows(*run_statement(tx, 'SELECT position,subject,generation,phase,status FROM history WHERE run_id=? AND position>? AND position<=? ORDER BY position LIMIT 128', (cursor, highwater)), limit=128)
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
                gate_history = tx.rows(*run_statement(tx, "SELECT h.* FROM history h JOIN gates g ON g.run_id=h.run_id AND g.subject=h.subject AND g.code=h.identity AND g.evidence IS h.material_id WHERE h.run_id=? AND h.kind='paused' AND h.subject=? AND h.generation=? AND h.identity=? ORDER BY h.position DESC LIMIT 1", (action["subject"], action.get("generation"), action["reason"])))
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

    @property
    def closed(self) -> bool:
        call = self._call
        return ((self.thread is None or not self.thread.is_alive())
                and (call is None or call.thread is None or not call.thread.is_alive()))

    def observe_committed(self) -> None:
        """Merge a wake only; the notification owner reads committed facts."""
        with self.condition:
            if not self.enabled or self._mode in {"cancel", "cancel_after_drain"}:
                return
            self._refresh_dirty = True
            if self.status_enabled:
                self._status_dirty = True
            self.condition.notify_all()

    def _send_status(self, call: CurrentCall) -> None:
        """Keep the existing Status intent/readback protocol in this physical slot."""
        for subject in status_subjects(self.database, **self._storage_options):
            if call.cancel.is_set():
                break
            # Readback may settle an older sent intent before applying current copy.
            for opportunity in range(2):
                if opportunity == 0:
                    result, fingerprint = self.status.deliver_current(subject,
                        previous=self._status_seen.get(subject), api=self.status_api, cancelled=call.cancel.is_set)
                else:
                    result = self.status.deliver(subject, api=self.status_api, cancelled=call.cancel.is_set)
                if call.cancel.is_set():
                    break  # Preserve the original intent and retry display after important delivery.
                if result != "confirmed":
                    break
            if call.cancel.is_set():
                break
            self._status_seen[subject] = fingerprint
        if call.cancel.is_set():
            with self.condition:
                self._status_dirty = True
        self.status_failure_reason = None

    def _wake_pending(self, document: dict[str, Any]) -> None:
        """Only projected important work can interrupt the current Status call."""
        pending = document["pending"]
        if self.exit_notice is not None:
            pending = [item for item in pending if item["event"]["id"] == self.exit_event_id]
        with self.condition:
            call = self._call
            if (call is not None and call.kind == "status"
                    and any(item.get("important") and not item.get("cancelled") for item in pending)):
                call.cancel.set()
                self._status_dirty = True
            self.condition.notify_all()

    def _pause(self, error: Exception) -> None:
        reason = bounded_error(str(error))
        with self.condition:
            self.failure_reason = reason
            if self._call is not None and self._call.kind == "feishu":
                self._call.cancel.set()
            self.condition.notify_all()
        # The worker owns fault receipts and recovery. Never hold the producer
        # condition while waiting for the journal or its durability operations.
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
            if event['kind'] == 'executor_exit' and event['exit_evidence'].get('backend') == 'foreground':
                # Recovery after retirement uses the original exit window,
                # including a crash before this first outbox insertion.
                now = float(event['exit_evidence']['ended_at'])
                prior = document.get('executor_exit')
                if prior is None or prior['epoch'] < event['exit_evidence']['epoch']:
                    document['executor_exit'] = dict(event['exit_evidence'])
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
        self._wake_pending(document)
        self._more = False
        return True

    def _observe_committed(self) -> bool:
        """Read only committed facts; claim no transport while a kernel lock is held."""
        if not self.enabled or self._mode in {"cancel", "cancel_after_drain"}:
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
                self._wake_pending(document)
                self._more = more
                if not more or full or boundary_more:
                    return True
            return True
        except Exception as error:
            self._pause(error)
            return False

    def _send_one(self, event: dict[str, Any], deadline_at: float, cancel: threading.Event) -> tuple[Mapping[str, Any], Exception | None]:
        """Use the original binding, cancellation and absolute event deadline."""
        result: Mapping[str, Any]
        try:
            with self.storage.transaction(write=False) as tx:
                if not self._same_binding(*self._run(tx)):
                    raise ValueError("notification Run or recipient binding changed")
        except Exception as error:
            result = {"outcome": "failed", "reason": bounded_error(str(error))}
        else:
            try:
                current = event["kind"] != "executor_exit" or self._exit_current(event)
            except Exception as error:
                # No physical request began. The owner pauses and persists
                # this original claim as known unsent before any recovery.
                return {"outcome": "failed", "reason": "journal claim interrupted before transport", "not_sent": True}, error
            if not current:
                result = {"outcome": "failed", "reason": "original Host exit no longer current", "not_sent": True}
            else:
                try:
                    options: dict[str, Any] = {"timeout_seconds": max(0.0, deadline_at - time.time())}
                    if event["kind"] == "executor_exit":
                        options["is_current"] = lambda: self._exit_current(event)
                    identity = hashlib.sha256(event["id"].encode()).hexdigest()[:32]
                    result = self.transport(self.config, card(event), identity, cancel, **options)
                except Exception as error:
                    from agent_run.queries.host_exit import HostExitUnavailable
                    if event["kind"] == "executor_exit" and isinstance(error, HostExitUnavailable):
                        result = {"outcome": "failed", "reason": bounded_error(str(error)), "not_sent": True}
                    else:
                        result = {"outcome": "unknown", "reason": bounded_error(str(error))}
        return result, None

    def _deliver(self, call: CurrentCall, event: dict[str, Any] | None, deadline_at: float) -> None:
        """Publish a physical result without surrendering the original slot."""
        fault = None
        result: dict[str, Any] = {}
        try:
            if call.kind == "status":
                self._send_status(call)
            else:
                assert event is not None
                returned, fault = self._send_one(event, deadline_at, call.cancel)
                result = dict(returned)
        except BaseException as error:
            if call.kind == "status":
                self.status_failure_reason = "status_sender_failed"
                print("Agent Run Status unavailable; business continues", file=sys.stderr)
            else:
                result = {"outcome": "unknown", "reason": bounded_error(str(error))}
        returned_at = time.time()
        with self.condition:
            call.result = result, returned_at, fault
            self.condition.notify_all()

    def _start_call(self, call: CurrentCall, event: dict[str, Any] | None = None,
                    deadline_at: float = 0.0) -> None:
        with self.condition:
            if self._mode in {"cancel", "cancel_after_drain"}:
                call.cancel.set()
                call.result = ({"outcome": "failed", "reason": "journal claim interrupted before transport",
                                "not_sent": True}, time.time(), None)
                return
            call.thread = threading.Thread(target=self._deliver, args=(call, event, deadline_at),
                                           name="kernel-notification-call", daemon=True)
        call.thread.start()

    def _work(self) -> None:
        """Own Feishu projection/recovery; one cancellable physical call at a time."""
        last_kind = "feishu"  # Give both ordinary Feishu and Status bounded opportunities.
        try:
            while True:
                try:
                    with self.condition:
                        call = self._call
                        running = call is not None and call.result is None
                        fresh = call is not None and call.result is not None and not call.settlement_deferred
                        stopping = self._mode in {"cancel", "cancel_after_drain"}
                        if stopping and running:
                            self.condition.wait()
                            continue
                        if not stopping and self.failure_reason is not None:
                            if running or not self._refresh_dirty and not fresh:
                                if self._mode == "drain" and not running:
                                    return
                                self.condition.wait()
                                continue
                        refresh = self._refresh_dirty
                        self._refresh_dirty = False  # New producer wakes during IO remain pending.
                        recovering = self.failure_reason is not None
                    if call is not None and call.result is not None:
                        if call.thread is not None and call.thread.ident is not None:
                            call.thread.join()
                        result, returned_at, fault = call.result
                        call.settlement_deferred = True
                        if fresh and fault is not None:
                            self._pause(fault)
                            recovering, refresh = True, False
                        if call.claim is not None:
                            self._finish_result(call.claim, result, returned_at)
                        last_kind = call.kind
                        with self.condition:
                            self._call = None  # Only after original ACK/known-unsent settlement.
                    if stopping:
                        return
                    if recovering and not refresh:
                        continue  # A completion is no permission to recover from a projection fault.
                    if refresh or not running:
                        if not self._observe_committed():
                            continue
                    with self.condition:
                        if self._mode in {"cancel", "cancel_after_drain"}:
                            continue
                        if running:
                            self.condition.wait_for(lambda: self._mode in {"cancel", "cancel_after_drain"}
                                                    or self._call is None or self._call.result is not None
                                                    or self._refresh_dirty)
                            continue
                    # Every bounded healthy projection gets a scheduling opportunity,
                    # even if later producer wakes remain pending.
                    event = None
                    deadline_at = 0.0
                    pending: list[dict[str, Any]] = []
                    status_due = self._status_dirty
                    if recovering and not self.feishu_enabled:
                        with outbox.transaction(self.state_root, self.run_id) as document:
                            with self.condition:
                                if self._mode in {"cancel", "cancel_after_drain"}:
                                    return
                            document.pop("unavailable_reason", None)
                    if self.feishu_enabled:
                        validity = self._exit_validity()
                        with outbox.transaction(self.state_root, self.run_id) as document:
                            with self.condition:
                                if self._mode in {"cancel", "cancel_after_drain"}:
                                    return
                            document.pop("unavailable_reason", None)
                            outbox.settle(document, time.time())
                            self._cancel_exit_notices(document, validity)
                            all_pending = document["pending"]
                            pending = [item for item in all_pending
                                       if self.exit_notice is None or item["event"]["id"] == self.exit_event_id]
                            inflight = any(item["outcome"] == "sending" for item in all_pending)
                            important = any(item.get("important") and not item.get("cancelled") for item in pending)
                            status_due = status_due and not important
                            item = None if inflight else next((item for item in sorted(pending, key=lambda entry: not entry.get("important")) if not item.get("cancelled")
                                                                and (item["event"]["kind"] != "executor_exit"
                                                                     or validity.get(item["event"]["id"]) == (item["event"], True))
                                                                and not (document.get("native_boundary_catching_up")
                                                                         and item["event"].get("current")
                                                                         and item["event"].get("kind") == "boundary")
                                                                and item["next_attempt_at"] <= time.time()), None)
                            if item is not None and (not status_due or last_kind == "status"):
                                item.update(owner=self.owner, outcome="sending")
                                item["attempts"] += 1
                                item["attempt_history"].append({"number": item["attempts"],
                                                                "started_at": time.time(), "outcome": "sending"})
                                event = dict(item["event"])
                                deadline_at = float(item["deadline_at"])
                                with self.condition:
                                    self._call = CurrentCall("feishu", (event["id"], item["attempts"]))
                            wait = min((max(0.0, min(entry["next_attempt_at"], entry["deadline_at"]) - time.time())
                                        for entry in pending), default=0.1)
                    with self.condition:
                        # Mirror the successful journal recovery even if close
                        # raced its commit. Cancellation remains irreversible.
                        self.failure_reason = None
                        if self._mode in {"cancel", "cancel_after_drain"}:
                            if self._call is not None:
                                self._call.result = ({"outcome": "failed", "reason": "journal claim interrupted before transport",
                                                      "not_sent": True}, time.time(), None)
                            continue
                        if event is None and status_due:
                            self._status_dirty = False
                            self._call = CurrentCall("status")
                        call = self._call
                        if call is None:
                            if self._mode == "drain" and not pending and not self._more:
                                return
                            if self._refresh_dirty:
                                continue
                            self.condition.wait(timeout=(max(0.01, min(wait, 0.1))
                                                         if pending or self._more else None))
                            continue
                    self._start_call(call, event, deadline_at)
                except Exception as error:
                    call = self._call
                    if call is not None and call.result is None and (call.thread is None or call.thread.ident is None):
                        call.settlement_deferred = True  # No physical completion grants a fresh settlement.
                        call.result = ({"outcome": "failed", "reason": "journal claim interrupted before transport",
                                        "not_sent": True}, time.time(), None)
                    self._pause(error)
                    if self._mode in {"cancel", "cancel_after_drain"}:
                        return  # One final settlement opportunity, never spin on unavailable IO.
        finally:
            self._finalize_fault()

    def _finalize_fault(self) -> None:
        """Best effort owned cleanup; close never reenters a blocked journal."""
        if self._mode not in {"drain", "cancel_after_drain"} or self.failure_reason is None:
            return
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

    def _finish_result(self, claim: tuple[str, int], result: dict[str, Any], returned_at: float) -> None:
        with outbox.transaction(self.state_root, self.run_id) as document:
            item = next((entry for entry in document["pending"] if entry["event"]["id"] == claim[0]
                         and entry.get("owner") == self.owner and entry["attempts"] == claim[1]), None)
            if item is not None:
                not_sent = result.get("not_sent") is True
                outbox.finish_attempt(item, {key: value for key, value in result.items() if key != "not_sent"}, returned_at)
                if not_sent:
                    item["attempt_history"][-1]["not_sent"] = True
                outbox.settle(document, time.time())

    def close(self, *, drain: bool = True, timeout_seconds: float = CLOSE_TIMEOUT_SECONDS) -> None:
        if not self.enabled or self.closed:
            return
        with self.condition:
            if drain and self._mode == "running":
                self._mode = "drain"
                self._refresh_dirty = True
                self._status_dirty = self.status_enabled
            elif not drain:
                self._mode = "cancel_after_drain" if self._mode in {"drain", "cancel_after_drain"} else "cancel"
                if self._call is not None:
                    self._call.cancel.set()
            self.condition.notify_all()
        if self.thread is not None and drain:
            self.thread.join(timeout=max(0.0, min(timeout_seconds, CLOSE_TIMEOUT_SECONDS)))
        with self.condition:
            self._mode = "cancel_after_drain" if self._mode in {"drain", "cancel_after_drain"} else "cancel"
            if self._call is not None:
                self._call.cancel.set()
            self.condition.notify_all()
        if self.thread is not None:
            grace = 2 * WORKER_CREDENTIAL_PROVIDER_OPERATION_TIMEOUT_SECONDS + 5 if self.status_enabled else 2
            cleanup_deadline = monotonic() + grace
            self.thread.join(timeout=grace)
            call = self._call
            if call is not None and call.thread is not None and call.thread.ident is not None:
                call.thread.join(timeout=max(0.0, cleanup_deadline - monotonic()))
        if not self.closed:
            self.status_failure_reason = "status_sender_cleanup_unproven"
            print("Notification sender cleanup unproven", file=sys.stderr)


def notify_committed(database: Path, *, timeout_seconds: float = 1.0, storage: Storage) -> None:
    """One finite post-commit opportunity through the existing sender."""
    observer: NativeNotifications | None = None
    close_returned = False
    options: dict[str, Any] = {"storage": storage}
    try:
        observer = NativeNotifications(database, database.parent, **options)
        observer.close(timeout_seconds=timeout_seconds)
        close_returned = True
    except Exception:
        print("Notification observation unavailable; business continues", file=sys.stderr)
    finally:
        # A normal close already spent its cleanup allowance and reported any
        # unproven exit. Only an interrupted close needs the cancellation fallback.
        if observer is not None and not close_returned and not observer.closed:
            observer.close(drain=False)


def deliver_host_exit_notice(database: Path, state_root: Path, notice: Mapping[str, Any], *, storage: Storage) -> None:
    """Drain one exact Host-exit receipt through the existing bounded sender."""
    options: dict[str, Any] = {"storage": storage}
    observer = NativeNotifications(database, state_root, exit_notice=notice, **options)
    observer.close()
