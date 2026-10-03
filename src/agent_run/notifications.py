"""Run-owned asynchronous delivery with persistent, finite retry budgets."""
from __future__ import annotations

import hashlib
import os
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from agent_run.error_safety import bounded_error
from agent_run.executor_host import _process_start_token
from agent_run.notification_cards import card
from agent_run.notification_events import events, recovery_events
from agent_run import notification_outbox as outbox
from agent_run.notification_outbox import notification_path as notification_path, read_notifications as read_notifications
from agent_run.notification_transport import send

CLOSE_TIMEOUT_SECONDS = 60.0


class Notifications:
    """Finite delivery for Executors and host exits, sharing claims and budgets.

    ``load_state`` must be a lock-free read of the authoritative Run snapshot.
    A host exit observer can use this same entrypoint without business writes.
    """

    def __init__(self, root: Path, state: dict[str, Any], *,
                 load_state: Callable[[], dict[str, Any] | None] | None = None) -> None:
        self.root = root
        self.run_id = str(state["run_id"])
        self.binding = (state.get("repository"), (state.get("parent") or {}).get("number"))
        config = state.get("notifications", {})
        self.config = dict(config) if isinstance(config, dict) else {}
        self.enabled = isinstance(self.config, dict) and self.config.get("enabled") is True
        self.load_state = load_state
        self.condition = threading.Condition()
        self.cancel = threading.Event()
        self.thread: threading.Thread | None = None
        self.closing = False
        self.closed = False
        self.failure_reason: str | None = None
        self.recovery_requested = False
        self.owner = {"pid": os.getpid(), "token": _process_start_token(os.getpid()), "sender": uuid.uuid4().hex}
        self.document: dict[str, Any] = {}
        self.state = state
        if not self.enabled:
            if isinstance(self.config, dict) and self.config.get("unavailable_reason"):
                print(f"飞书通知不可用（开发继续）：{self.config['unavailable_reason']}", file=sys.stderr)
            return
        self.observe(state, recovering=True)
        self.thread = threading.Thread(target=self._work, name="agent-run-notifications")
        try:
            self.thread.start()
        except RuntimeError as error:
            self.thread = None
            self._unavailable(error)

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        with self.condition, outbox.transaction(self.root, self.run_id) as document:
            self.document = document
            yield
            self.condition.notify_all()

    def _current(self, fallback: dict[str, Any]) -> dict[str, Any]:
        state = self.load_state() if self.load_state else fallback
        if (state is None or state.get("run_id") != self.run_id
                or (state.get("repository"), (state.get("parent") or {}).get("number")) != self.binding
                or state.get("notifications", {}) != self.config):
            raise ValueError("无法核验通知所属 Run 或接收人绑定")
        return state

    def observe(self, state: dict[str, Any], *, recovering: bool = False) -> None:
        if not self.enabled or self.closed or state.get("run_id") != self.run_id:
            return
        try:
            with self._transaction():
                self._observe(self._current(state), recovering=recovering)
        except Exception as error:
            self._unavailable(error)

    def _observe(self, state: dict[str, Any], *, recovering: bool = False) -> None:
        from agent_run.executor_exit import exit_event

        self.state = state
        projected = events(state) + exit_event(self.root, state, self.document)
        now = time.time()
        document = self.document
        if self.failure_reason is None:
            document.pop("unavailable_reason", None)
        else:
            # The worker acknowledges this only after the cancelled transport
            # has returned. A successful read alone cannot clear its failure.
            self.recovery_requested = True
        outbox.settle(document, now)
        seen = set(document["seen"]) | set(document["milestones"])
        active = set(document["active"])
        current = {item["id"] for item in projected if item.get("current")}
        ids = {item["id"] for item in projected}
        pending = document["pending"]
        had_pending = bool(pending)
        for item in list(pending):
            event = item["event"]
            if ((event.get("current") and event.get("slot_id") not in current)
                    or (item.get("important") and not event.get("current") and event["id"] not in ids)):
                outbox.cancel_item(document, item, "no_longer_current")
        added = False
        for event in projected:
            identity = event["id"]
            resume_identity = (f"{event['resume_id']}:{event.get('resume_outcome')}"
                               if event.get("resume_id") else None)
            if event.get("current"):
                if identity in active and (resume_identity is None or resume_identity in document["resume_results"]):
                    continue
                for item in list(pending):
                    if item["event"].get("slot_id") == identity:
                        outbox.cancel_item(document, item, "superseded")
                document["sequence"] += 1
                event = {**event, "slot_id": identity, "id": f"{identity}:{document['sequence']}"}
            elif identity in seen or (resume_identity and resume_identity in document["resume_results"]):
                continue
            outbox.enqueue(document, event, now)
            if resume_identity and resume_identity not in document["resume_results"]:
                document["resume_results"].append(resume_identity)
            if event["kind"] in {"ticket_started", "parent_started", "acceptance_started", "acceptance_repair"}:
                document["milestones"].append(identity)
            added = True
        document["seen"] = list(dict.fromkeys(
            identity for event in projected for identity in [event["id"], *event.get("supersedes", [])]))
        document["active"] = sorted(current)
        document["resume_results"] = document["resume_results"][-outbox.MAX_RECORDS:]
        milestone_limit = len((state.get("ticket_graph") or {}).get("tickets") or {}) + 2
        document["milestones"] = document["milestones"][-milestone_limit:]
        # Compress obsolete ordinary progress without manufacturing a new
        # identity or resetting the retained event's attempts/deadline.
        ordinary = [item for item in pending if not item["important"] and item["outcome"] != "sending"]
        if ordinary and ((recovering and had_pending) or len(pending) > outbox.MAX_PENDING or (
                added and any(item["outcome"] == "failed" for item in ordinary))):
            keep = {event["id"] for event in recovery_events(state, [item["event"] for item in ordinary])}
            for item in ordinary:
                if item["event"]["id"] not in keep:
                    outbox.cancel_item(document, item, "obsolete_progress")
        limit = outbox.MAX_PENDING + len(current)
        while len(pending) > limit:
            item = next((entry for entry in pending if entry["outcome"] != "sending"
                         and not entry["event"].get("current")), None)
            if item is None:
                break
            outbox.cancel_item(document, item, "capacity")

    def _unavailable(self, error: Exception, *, release_claim: bool = False) -> None:
        reason = bounded_error(str(error))
        print(f"飞书通知不可用（开发继续）：{reason}", file=sys.stderr)
        with self.condition:
            self.failure_reason = reason
            self.recovery_requested = False
            self.cancel.set()
            try:
                with self._transaction():
                    self.document["unavailable_reason"] = reason
                    for item in self.document["pending"]:
                        # Observation failure cannot release a live transport.
                        if release_claim and item.get("outcome") == "sending" and item.get("owner") == self.owner:
                            outbox.finish_attempt(item, {"outcome": "unknown", "reason": reason}, time.time())
            except Exception:
                self.document["unavailable_reason"] = reason
            self.condition.notify_all()

    def _work(self) -> None:
        while True:
            try:
                self._deliver()
            except Exception as error:
                self._unavailable(error, release_claim=True)
            with self.condition:
                # Park until a business observation or normal close can prove
                # recovery. No polling, new thread, or renewed event budget.
                while self.failure_reason and not self.recovery_requested and not self.closing:
                    self.condition.wait()
                if self.closed or not self.failure_reason or not self.recovery_requested:
                    return
                self.failure_reason = None
                self.recovery_requested = False
                self.cancel.clear()

    def _deliver(self) -> None:
        while not self.cancel.is_set():
            with self._transaction():
                if self.cancel.is_set():
                    return
                self._observe(self._current(self.state))
                outbox.settle(self.document, time.time())
                pending = self.document["pending"]
                inflight = any(item["outcome"] == "sending" for item in pending)
                available = None if inflight else next((item for item in pending
                    if not item.get("cancelled") and item["next_attempt_at"] <= time.time()), None)
                if available is not None:
                    available.update(owner=self.owner, outcome="sending")
                    available["attempts"] += 1
                    available["attempt_history"].append({"number": available["attempts"],
                        "started_at": time.time(), "outcome": "sending"})
                elif self.closing and not pending:
                    return
                wait_seconds = (0.1 if inflight else min(
                    (max(0.0, min(item["next_attempt_at"], item["deadline_at"]) - time.time())
                     for item in pending), default=0.1))
            if available is None:
                with self.condition:
                    if self.closing and not self.document["pending"]:
                        return
                    self.condition.wait(timeout=wait_seconds if self.document["pending"] else None)
                continue
            identity = hashlib.sha256(available["event"]["id"].encode()).hexdigest()[:32]
            options: dict[str, Any] = {}
            if available["event"]["kind"] == "executor_exit":
                from agent_run.executor_exit import exit_event

                def is_current() -> bool:
                    return bool(exit_event(self.root, self._current(self.state),
                                           {"executor_exit": available["event"]["exit_evidence"]}))

                options["is_current"] = is_current
            result = send(self.config, card(available["event"]), identity, self.cancel,
                          timeout_seconds=max(0.0, available["deadline_at"] - time.time()), **options)
            with self._transaction():
                item = next((entry for entry in self.document["pending"]
                             if entry["event"]["id"] == available["event"]["id"]
                             and entry.get("owner") == self.owner), None)
                if item is not None:
                    outbox.finish_attempt(item, dict(result), time.time())
                    item["attempt_history"][-1]["cancelled"] = self.cancel.is_set()
                    if result["outcome"] != "success":
                        print(f"飞书通知未确认送达（开发继续）：{result['reason']}", file=sys.stderr)
                    outbox.settle(self.document, time.time())

    def close(self) -> None:
        if not self.enabled or self.closed:
            return
        with self.condition:
            self.closing = True
            if self.failure_reason:
                self.observe(self.state)
            self.condition.notify_all()
        # Normal exit grants the outbox a complete, bounded delivery lifetime.
        # The transport observes cancellation and reaps its process tree.
        if self.thread is not None:
            self.thread.join(timeout=CLOSE_TIMEOUT_SECONDS)
        with self.condition:
            self.closed = True
            self.cancel.set()
            self.condition.notify_all()
        if self.thread is not None:
            self.thread.join(timeout=2)
        if self.failure_reason:
            try:
                with self._transaction():
                    self.document["unavailable_reason"] = self.failure_reason
                    for item in self.document["pending"]:
                        if item["outcome"] != "sending":
                            item["delivery_error"] = self.failure_reason
                            if item["outcome"] == "pending":
                                item.update(outcome="failed", reason=self.failure_reason)
                    # A healthy overlapping sender can still use the original
                    # remaining budget; this sender cannot retire its work.
                    outbox.settle(self.document, time.time())
            except Exception as error:
                self._unavailable(error)
