"""Bounded, Run-owned notification delivery; queries never start a sender."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sys
import tempfile
import threading
import uuid
from contextlib import contextmanager
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from agent_run.error_safety import bounded_error
from agent_run.executor_host import _process_binding_status, _process_start_token
from agent_run.notification_cards import card
from agent_run.notification_events import events, recovery_event
from agent_run.notification_transport import send

MAX_PENDING = 32
MAX_RECORDS = 256
MAX_JOURNAL_BYTES = 1024 * 1024


def notification_path(root: Path, run_id: str) -> Path:
    # Do not allow external identifiers to select a filesystem path.
    name = hashlib.sha256(run_id.encode()).hexdigest()[:32]
    return root / "notifications" / f"{name}.json"


def read_notifications(root: Path, run_id: str) -> dict[str, Any]:
    path = notification_path(root, run_id)
    try:
        if not path.exists():
            return {}
        with path.open("rb") as stream:
            raw = stream.read(MAX_JOURNAL_BYTES + 1)
        if len(raw) > MAX_JOURNAL_BYTES:
            raise ValueError("通知记录超过容量")
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("通知记录格式错误")
        for field in ("seen", "pending", "records", "active"):
            if field in value and not isinstance(value[field], list):
                raise ValueError(f"通知记录 {field} 格式错误")
        if not isinstance(value.get("occurrences", {}), dict):
            raise ValueError("通知 occurrence 记录格式错误")
        for item in value.get("pending", []):
            if not isinstance(item, dict) or not isinstance(item.get("event"), dict) or not isinstance(item["event"].get("id"), str):
                raise ValueError("通知待发记录格式错误")
        return value
    except (OSError, ValueError) as error:
        return {"unavailable_reason": bounded_error(str(error))}


class Notifications:
    """One finite sender per Executor, with a bounded durable outbox.

    The business state remains authoritative. Seen identities cover the current
    history window, so trimming the sent audit does not replay old history.
    In-flight outcomes survive interruption as unknown and are not blindly retried.
    """

    def __init__(self, root: Path, state: dict[str, Any]) -> None:
        self.root = root
        self.run_id = str(state["run_id"])
        self.config = state.get("notifications", {})
        self.enabled = isinstance(self.config, dict) and self.config.get("enabled") is True
        self.condition = threading.Condition()
        self.cancel = threading.Event()
        self.thread: threading.Thread | None = None
        self.closing = False
        self.version = 0
        self.epoch = uuid.uuid4().hex
        self.owner = {"pid": os.getpid(), "token": _process_start_token(os.getpid())}
        self.document: dict[str, Any] = {}
        if not self.enabled:
            if isinstance(self.config, dict) and self.config.get("unavailable_reason"):
                print(f"飞书通知不可用（开发继续）：{self.config['unavailable_reason']}", file=sys.stderr)
            return
        self.observe(state, recovering=True)
        if not self.document.get("unavailable_reason"):
            self.thread = threading.Thread(target=self._work, name="agent-run-notifications")
            try:
                self.thread.start()
            except RuntimeError as error:
                self.thread = None
                self._unavailable(error)

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        # Control and retiring Executors can briefly overlap. Serialize only
        # local journal transactions, never hold this lock across CLI calls.
        with self.condition:
            path = notification_path(self.root, self.run_id).with_suffix(".lock")
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a+") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                try:
                    self.document = read_notifications(self.root, self.run_id)
                    for key in ("seen", "pending", "records", "active"):
                        self.document.setdefault(key, [])
                    self.document.setdefault("occurrences", {})
                    self.recovered = False
                    for item in self.document["pending"]:
                        owner = item.get("owner", {})
                        if item.get("outcome") == "sending" and (
                            type(owner.get("pid")) is not int or _process_binding_status(
                                owner["pid"], owner.get("token")
                            ) == "absent"
                        ):
                            self.recovered = True
                            item["outcome"] = "unknown"
                            item["reason"] = "上次发送被中断，送达结果未知；不自动重复投递"
                    yield
                finally:
                    fcntl.flock(lock, fcntl.LOCK_UN)

    def _save(self) -> None:
        path = notification_path(self.root, self.run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(self.document, ensure_ascii=False, separators=(",", ":"))
        if len(encoded.encode()) > MAX_JOURNAL_BYTES:
            raise ValueError("通知记录超过容量")
        descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=".notification-")
        try:
            with os.fdopen(descriptor, "w") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, path)
        finally:
            Path(name).unlink(missing_ok=True)

    def observe(self, state: dict[str, Any], *, recovering: bool = False) -> None:
        if not self.enabled or state.get("run_id") != self.run_id:
            return
        try:
            with self._transaction():
                projected = events(state)
                seen = set(self.document["seen"])
                active = set(self.document["active"])
                current = {item["id"] for item in projected if item.get("current")}
                pending = self.document["pending"]
                had_pending = bool(pending)
                added = False
                for item in projected:
                    identity = item["id"]
                    if item.get("current"):
                        if identity in active:
                            continue
                        counts = self.document["occurrences"]
                        counts[identity] = counts.get(identity, 0) + 1
                        item = {**item, "id": f"{identity}:{counts[identity]}"}
                    elif identity in seen:
                        continue
                    pending.append({"event": item, "outcome": "pending", "attempts": 0})
                    added = True
                self.document["seen"] = [item["id"] for item in projected]
                self.document["active"] = sorted(current)
                # Only recurring boundary identities need counters.
                self.document["occurrences"] = dict(list(self.document["occurrences"].items())[-MAX_RECORDS:])
                retryable = [item for item in pending if item["outcome"] not in {"sending", "unknown"}]
                stale = (added or recovering) and (
                    (recovering and had_pending)
                    or any(item["outcome"] == "failed" for item in retryable)
                )
                if retryable and (stale or len(pending) > MAX_PENDING):
                    summary = recovery_event(state, [item["event"] for item in retryable])
                    digest = hashlib.sha256(json.dumps([item["event"]["id"] for item in retryable]).encode()).hexdigest()
                    summary["id"] = f"{self.run_id}:recovery:{digest}"
                    pending[:] = [item for item in pending if item not in retryable]
                    pending.append({"event": summary, "outcome": "pending", "attempts": 0})
                # Unknown outcomes remain queryable, bounded, and never resubmitted.
                while len(pending) > MAX_PENDING:
                    retired = next((item for item in pending if item["outcome"] == "unknown"), None)
                    if retired is None:
                        break
                    pending.remove(retired)
                    self._record(retired)
                if added or recovering:
                    self.version += 1
                self._save()
                self.condition.notify_all()
        except Exception as error:
            self._unavailable(error)

    def _record(self, item: dict[str, Any]) -> None:
        self.document["records"].append({
            "id": item["event"]["id"], "outcome": item["outcome"],
            "attempts": item["attempts"], "reason": item.get("reason"),
        })
        self.document["records"] = self.document["records"][-MAX_RECORDS:]

    def _unavailable(self, error: Exception) -> None:
        reason = bounded_error(str(error))
        print(f"飞书通知不可用（开发继续）：{reason}", file=sys.stderr)
        try:
            with self._transaction():
                self.document["unavailable_reason"] = reason
                for item in self.document["pending"]:
                    if item.get("outcome") == "sending" and item.get("owner") == self.owner:
                        item.update(outcome="unknown", reason=reason)
                self._save()
        except Exception:
            self.document["unavailable_reason"] = reason

    def _work(self) -> None:
        try:
            self._deliver()
        except Exception as error:
            self._unavailable(error)

    def _deliver(self) -> None:
        while not self.cancel.is_set():
            with self._transaction():
                version = f"{self.epoch}:{self.version}"
                inflight = any(item["outcome"] == "sending" for item in self.document["pending"])
                available = None if inflight else next((item for item in self.document["pending"]
                                  if item["outcome"] not in {"unknown", "sending"}
                                  and item.get("version") != version), None)
                if available is not None:
                    available["version"] = version
                    available["owner"] = self.owner
                    available["outcome"] = "sending"
                if available is not None or self.recovered:
                    self._save()
            if available is None:
                with self.condition:
                    if self.closing and not inflight and version == f"{self.epoch}:{self.version}":
                        return
                    # The other control Executor can finish an in-flight send.
                    if version == f"{self.epoch}:{self.version}":
                        self.condition.wait(timeout=0.1 if inflight else None)
                continue
            identity = hashlib.sha256(available["event"]["id"].encode()).hexdigest()[:32]
            attempts = 0
            for _ in range(3):
                result = send(self.config, card(available["event"]), identity, self.cancel)
                attempts += 1
                if result["outcome"] != "failed" or self.cancel.is_set():
                    break
            with self._transaction():
                item = next((entry for entry in self.document["pending"]
                             if entry["event"]["id"] == available["event"]["id"]), None)
                if item is not None:
                    item.update(result)
                    item["attempts"] += attempts
                    if result["outcome"] != "success":
                        print(f"飞书通知未确认送达（开发继续）：{result['reason']}", file=sys.stderr)
                    if item["outcome"] == "success":
                        self.document["pending"].remove(item)
                        self._record(item)
                    self._save()
                self.condition.notify_all()

    def close(self) -> None:
        if self.thread is None:
            return
        with self.condition:
            self.closing = True
            self.condition.notify_all()
        # Give fast deliveries a small drain window; slow external work is cancelled.
        self.thread.join(timeout=2)
        self.cancel.set()
        with self.condition:
            self.condition.notify_all()
        self.thread.join(timeout=2)
