"""Post-commit native notification observer using the existing durable outbox.

This observer reads committed kernel facts. It neither calls Kernel nor infers a
new business state from a displayed Status document.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_run import notification_outbox as outbox
from agent_run.error_safety import bounded_error
from agent_run.notification_cards import card
from agent_run.notification_event_facts import event_copy, event_envelope
from agent_run.notification_transport import send
from agent_run.process_binding import _process_start_token

from .query import notification_boundaries
from .storage import Storage, Transaction


CLOSE_TIMEOUT_SECONDS = 60.0
Send = Callable[..., Mapping[str, Any]]


class NativeNotifications:
    """Deliver only committed native events for one exact Run and owned directory."""

    def __init__(self, database: Path, state_root: Path, *, transport: Send = send) -> None:
        if (database.is_symlink() or state_root.is_symlink()
                or not database.is_file() or not state_root.is_dir()
                or database.parent.resolve() != state_root.resolve()):
            raise ValueError("notification state root does not own this database")
        self.database = database
        self.state_root = state_root
        self.transport = transport
        self.storage = Storage(database)
        with self.storage.transaction(write=False) as tx:
            self.run_id, self.binding, self.config, self.state = self._run(tx)
        self.enabled = self.config.get("enabled") is True
        self.condition = threading.Condition()
        self.cancel = threading.Event()
        self.owner = {"pid": os.getpid(), "token": _process_start_token(os.getpid()),
                      "sender": uuid.uuid4().hex}
        self.closing = False
        self.closed = False
        self.failure_reason: str | None = None
        self.thread: threading.Thread | None = None
        self._more = False
        if self.enabled:
            self.observe_committed()
            if self.failure_reason is None:
                self.thread = threading.Thread(target=self._work, name="kernel-notifications", daemon=True)
                self.thread.start()

    @staticmethod
    def _run(tx: Transaction) -> tuple[str, tuple[str, int], dict[str, Any], dict[str, Any]]:
        run = tx.one("SELECT id,repository,parent_issue,parent_revision,language,created_at FROM runs")
        inputs = tx.one("SELECT subject,material_id FROM run_inputs WHERE run_id=?", (run["id"],))
        value = json.loads(tx.get(inputs["material_id"], inputs["subject"]))
        if not isinstance(value, dict) or not isinstance(value.get("notifications"), dict):
            raise ValueError("notification configuration is not a frozen Run input")
        config = value["notifications"]
        if config.get("enabled") is True and run["language"] not in {"zh", "en"}:
            raise ValueError("enabled notification requires a known Run language")
        title = tx.rows("SELECT title FROM requirement_versions WHERE issue=? AND revision=?",
                        (run["parent_issue"], run["parent_revision"]))
        state = {"run_id": run["id"], "repository": run["repository"],
                 "parent": {"number": run["parent_issue"], "title": title[0][0] if title else ""},
                 "language": run["language"], "created_at": run["created_at"]}
        return run["id"], (run["repository"], run["parent_issue"]), config, state

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
            history = tx.rows("SELECT position,subject,kind,identity,generation,created_at,material_id "
                              "FROM history WHERE position>? ORDER BY position LIMIT 16", (after,), limit=16)
            events: list[dict[str, Any]] = []
            for row in history:
                event = self._event(tx, row, state)
                if event is not None:
                    events.append(event)
            return events, len(history) == 16, history[-1]["position"] if history else after

    def _event(self, tx: Transaction, row: Any, state: dict[str, Any]) -> dict[str, Any] | None:
        subject = tx.one("SELECT id,kind,issue,revision FROM subjects WHERE id=?", (row["subject"],))
        if row["kind"] == "acceptance_promoted":
            promotion = json.loads(tx.get(row["material_id"], row["subject"]))
            review = tx.one("SELECT * FROM attempts WHERE id=? AND subject=? AND generation=? AND role='review'",
                            (row["identity"], row["subject"], row["generation"]))
            if (subject["kind"] == "ticket" or promotion["review_attempt"] != review["id"]
                    or review["outcome"] != "pass" or review["result_id"] is None):
                raise ValueError("formal acceptance notification has no original Review")
            acceptance_key = [row["generation"], promotion["promoted_sha"], review["base_sha"],
                              review["expected_tree"], review["revision"], review["graph_revision"]]
            return event_envelope(state, "acceptance_passed", acceptance_key,
                                  event_copy(state, "parent_acceptance_passed" if subject["kind"] == "parent_only"
                                             else "acceptance_passed"), "green",
                                  summary=event_copy(state, "acceptance_passed_summary"),
                                  attempt_id=review["id"], round=review["ordinal"],
                                  phase=event_copy(state, "role_review"), execution_timing_complete=False)
        if row["kind"] == "effect_consumed" and self.config.get("mode") == "detailed":
            original = tx.rows("SELECT payload_id FROM effects WHERE id=? AND subject=? AND generation=? "
                               "AND operation='pr_write' AND status='confirmed' AND receipt_id=?",
                               (row["identity"], row["subject"], row["generation"], row["material_id"]))
            if original:
                intent = json.loads(tx.get(original[0]["payload_id"], row["subject"]))
                # An edit or an absent prior field is not a creation fact.
                if "prior" not in intent or intent["prior"] is not None:
                    return None
                receipt = json.loads(tx.get(row["material_id"], row["subject"]))
                pr = receipt.get("pr")
                if (receipt.get("disposition") != "target" or not isinstance(pr, dict)
                        or pr.get("repository") != self.binding[0]
                        or type(pr.get("number")) is not int or pr["number"] <= 0):
                    return None
                number = pr["number"]
                return event_envelope(state, "pr_created", number,
                                      event_copy(state, "pr_created", number=number), "green",
                                      summary=event_copy(state, "pr_summary"),
                                      url=f"https://github.com/{pr['repository']}/pull/{number}")
        if row["kind"] == "worker_started":
            first = tx.one("SELECT min(position) FROM history WHERE subject=? AND kind='worker_started'",
                           (row["subject"],))[0]
            if first != row["position"] or subject["kind"] not in {"ticket", "parent_only"}:
                return None
            attempt = tx.rows("SELECT a.id,a.role,o.started_at FROM invocations i "
                              "JOIN attempts a ON a.id=i.attempt "
                              "JOIN invocation_outputs o ON o.invocation=i.id "
                              "WHERE i.id=? AND a.subject=? AND o.started_at IS NOT NULL "
                              "ORDER BY o.ordinal", (row["identity"], row["subject"]))
            if not attempt or attempt[0]["role"] != "development":
                return None
            kind = "ticket_started" if subject["kind"] == "ticket" else "parent_started"
            title = tx.rows("SELECT title FROM requirement_versions WHERE issue=? AND revision=?",
                            (subject["issue"], subject["revision"]))
            name = title[0][0] if title else f"#{subject['issue']}"
            identity = (f"ticket:{subject['issue']}" if kind == "ticket_started"
                        else f"parent-only:{self.run_id}")
            facts = ({"task_number": subject["issue"], "task_title": name,
                      "url": f"https://github.com/{self.binding[0]}/issues/{subject['issue']}"}
                     if kind == "ticket_started" else {})
            return event_envelope(state, kind, identity, event_copy(state, kind, title=name),
                                  attempt_id=attempt[0]["id"],
                                  started_at=datetime.fromtimestamp(attempt[0]["started_at"], UTC).isoformat(),
                                  **facts)
        if row["kind"] == "effect_consumed" and subject["kind"] == "ticket":
            completed = tx.rows("SELECT generation FROM completions WHERE subject=? AND generation=? "
                                "AND close_effect=?", (row["subject"], row["generation"], row["identity"]))
            if not completed:
                return None
            title = tx.rows("SELECT title FROM requirement_versions WHERE issue=? AND revision=?",
                            (subject["issue"], subject["revision"]))
            name = title[0][0] if title else f"#{subject['issue']}"
            # The old ticket_jobs map keyed numbers as strings; preserve the
            # card identity across a native continuation of that Ticket.
            return event_envelope(state, "ticket_completed", [str(subject["issue"]), row["generation"]],
                                  event_copy(state, "ticket_completed", title=name), "green",
                                  task_number=subject["issue"], task_title=name,
                                  url=f"https://github.com/{self.binding[0]}/issues/{subject['issue']}")
        return None

    @staticmethod
    def _gate_slot(subject: str | None, generation: int | None, reason: str) -> str:
        return hashlib.sha256(json.dumps([subject, generation, reason]).encode()).hexdigest()

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
            view = notification_boundaries(tx, self.database)
            highwater = view["highwater"]
            if cursor < highwater:
                rows = tx.rows("SELECT position,subject,generation,phase,status FROM history "
                               "WHERE position>? AND position<=? ORDER BY position LIMIT 128",
                               (cursor, highwater), limit=128)
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
        current = {key: value["sequence"] for key, value in slots.items()}
        for item in list(document["pending"]):
            event = item["event"]
            if (event.get("kind") == "boundary" and event.get("current")
                    and (event.get("slot_id") not in actions or event.get("slot_id") not in current
                         or event.get("occurrence") != current[event["slot_id"]])):
                outbox.cancel_item(document, item, "recorded operator gate changed")
        document["active"] = list(actions)
        if not caught_up:
            return True
        for key, action in actions.items():
            occurrence = current[key]
            if slots[key].get("enqueued") or len(document["pending"]) >= outbox.MAX_PENDING:
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
            outbox.enqueue(document, event, time.time())
            slots[key]["enqueued"] = True
        return False

    def observe_committed(self) -> None:
        """Read only committed facts; claim no transport while a kernel lock is held."""
        if not self.enabled or self.closed or self.cancel.is_set():
            return
        try:
            for _ in range(64):
                with outbox.transaction(self.state_root, self.run_id) as document:
                    outbox.settle(document, time.time())
                    if not document.get("native_run_start"):
                        journal = outbox.notification_path(self.state_root, self.run_id)
                        if journal.exists() or journal.is_symlink():
                            raise ValueError("existing notification journal has no native receipt")
                        started = datetime.fromtimestamp(self.state["created_at"], UTC).isoformat()
                        event = event_envelope(self.state, "run_start", self.run_id,
                                               event_copy(self.state, "run_start"), started_at=started)
                        outbox.enqueue(document, event, time.time())
                        document["native_run_start"] = True
                    boundary_more = self._boundaries(document)
                    full = len(document["pending"]) > outbox.MAX_PENDING - 17
                    cursor = document.get("native_cursor", 0)
                    if type(cursor) is not int or cursor < 0:
                        raise ValueError("invalid native notification cursor")
                    events, more, position = (([], True, cursor) if full or boundary_more else self._page(cursor))
                    for event in events:
                        outbox.enqueue(document, event, time.time())
                    document["native_cursor"] = position
                with self.condition:
                    self.condition.notify_all()
                self._more = more
                if not more or full or boundary_more:
                    return
        except Exception as error:
            self.failure_reason = bounded_error(str(error))
            self.cancel.set()
            print(f"飞书通知不可用（开发继续）：{self.failure_reason}", file=sys.stderr)
            with self.condition:
                self.condition.notify_all()

    def _work(self) -> None:
        active_event_id: str | None = None
        while not self.cancel.is_set():
            try:
                # Each claim/retry rereads committed facts. A business commit
                # can outlive its callback (for example, after a Host crash).
                self.observe_committed()
                if self.cancel.is_set():
                    return
                with self.condition:
                    with outbox.transaction(self.state_root, self.run_id) as document:
                        outbox.settle(document, time.time())
                        pending = document["pending"]
                        inflight = any(item["outcome"] == "sending" for item in pending)
                        item = None if inflight else next((item for item in pending if not item.get("cancelled")
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
                            deadline_at = float(item["deadline_at"])
                        else:
                            event = None
                            deadline_at = 0.0
                            wait = min((max(0.0, min(entry["next_attempt_at"], entry["deadline_at"]) - time.time())
                                        for entry in pending), default=0.1)
                    if event is None:
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
                    try:
                        result = self.transport(self.config, card(event), identity, self.cancel,
                                                timeout_seconds=max(0.0, deadline_at - time.time()))
                    except Exception as error:
                        result = {"outcome": "unknown", "reason": bounded_error(str(error))}
                with outbox.transaction(self.state_root, self.run_id) as document:
                    claim = next((entry for entry in document["pending"] if entry["event"]["id"] == event["id"]
                                  and entry.get("owner") == self.owner), None)
                    if claim is not None:
                        outbox.finish_attempt(claim, dict(result), time.time())
                        outbox.settle(document, time.time())
                active_event_id = None
            except Exception as error:
                self.failure_reason = bounded_error(str(error))
                print(f"飞书通知不可用（开发继续）：{self.failure_reason}", file=sys.stderr)
                if active_event_id is not None:
                    try:
                        with outbox.transaction(self.state_root, self.run_id) as document:
                            claim = next((entry for entry in document["pending"]
                                          if entry["event"]["id"] == active_event_id
                                          and entry.get("owner") == self.owner), None)
                            if claim is not None:
                                outbox.finish_attempt(claim, {"outcome": "unknown",
                                                                   "reason": self.failure_reason}, time.time())
                                outbox.settle(document, time.time())
                    except Exception:
                        pass
                return

    def close(self, *, drain: bool = True) -> None:
        if not self.enabled or self.closed:
            return
        with self.condition:
            self.closing = True
            if not drain:
                self.cancel.set()
            self.condition.notify_all()
        if self.thread is not None and drain:
            self.thread.join(timeout=CLOSE_TIMEOUT_SECONDS)
        self.cancel.set()
        with self.condition:
            self.condition.notify_all()
        if self.thread is not None:
            self.thread.join(timeout=2)
        self.closed = True
