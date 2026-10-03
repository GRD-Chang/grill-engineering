"""Bounded delivery journal and per-event budgets, independent of business state."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from agent_run.error_safety import bounded_error
from agent_run.executor_host import _process_binding_status

MAX_PENDING = 32
MAX_RECORDS = 256
MAX_JOURNAL_BYTES = 1024 * 1024
MAX_ATTEMPTS = 3
DELIVERY_LIFETIME_SECONDS = 60.0
RETRY_DELAY_SECONDS = 1.0


def notification_path(root: Path, run_id: str) -> Path:
    return root / "notifications" / f"{hashlib.sha256(run_id.encode()).hexdigest()[:32]}.json"


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
        for field in ("seen", "pending", "records", "active", "milestones", "resume_results"):
            if field in value and not isinstance(value[field], list):
                raise ValueError(f"通知记录 {field} 格式错误")
        for item in value.get("pending", []):
            if (not isinstance(item, dict) or not isinstance(item.get("event"), dict)
                    or not isinstance(item["event"].get("id"), str)):
                raise ValueError("通知待发记录格式错误")
        return value
    except (OSError, ValueError) as error:
        return {"unavailable_reason": bounded_error(str(error))}


@contextmanager
def transaction(root: Path, run_id: str) -> Iterator[dict[str, Any]]:
    """Serialize journal claims, never external calls or business mutations."""
    path = notification_path(root, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            document = read_notifications(root, run_id)
            if document.get("unavailable_reason") and "pending" not in document:
                raise ValueError(document["unavailable_reason"])
            original = json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode()
            for key in ("seen", "pending", "records", "active", "milestones", "resume_results"):
                document.setdefault(key, [])
            # A single monotonic sequence avoids reusing an occurrence after
            # the bounded audit/counter window has been trimmed.
            document.setdefault("sequence", sum(document.pop("occurrences", {}).values()))
            yield document
            encoded = json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode()
            if encoded == original:
                return
            if len(encoded) > MAX_JOURNAL_BYTES:
                raise ValueError("通知记录超过容量")
            descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=".notification-")
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(encoded)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(name, path)
                directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            finally:
                Path(name).unlink(missing_ok=True)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def enqueue(document: dict[str, Any], event: dict[str, Any], now: float) -> None:
    document["pending"].append({
        "event": event, "outcome": "pending", "attempts": 0, "attempt_history": [],
        "created_at": now, "deadline_at": now + DELIVERY_LIFETIME_SECONDS,
        "next_attempt_at": now, "exhausted": False,
        "important": bool(event.get("important") or event.get("current") or event.get("resume_outcome") in {"failed", "human_action"}),
    })


def retire(document: dict[str, Any], item: dict[str, Any]) -> None:
    event = item["event"]
    record = {key: value for key, value in item.items() if key not in {"event", "owner", "next_attempt_at"}}
    record.update({key: event.get(key) for key in ("id", "kind", "work_subject", "generation", "slot_id")})
    if event.get("exit_evidence"):
        record["exit_evidence"] = event["exit_evidence"]
    document["records"].append(record)
    document["records"] = document["records"][-MAX_RECORDS:]
    document["pending"].remove(item)


def cancel_item(document: dict[str, Any], item: dict[str, Any], reason: str) -> None:
    item.update(cancelled=True, cancel_reason=reason)
    if item["outcome"] == "pending":
        item.update(outcome="cancelled", reason=reason)
    # Do not turn an actual unknown or failed attempt into a fictional no-send.
    if item["outcome"] != "sending":
        retire(document, item)


def finish_attempt(item: dict[str, Any], result: dict[str, Any], now: float) -> None:
    item.update(result)
    item["attempt_history"][-1].update(result, finished_at=now)
    item["next_attempt_at"] = now + RETRY_DELAY_SECONDS * item["attempts"]


def settle(document: dict[str, Any], now: float) -> None:
    """Recover abandoned claims and retire terminal results without renewing budgets."""
    for item in list(document["pending"]):
        if item["outcome"] == "sending":
            owner = item.get("owner", {})
            if type(owner.get("pid")) is int and _process_binding_status(owner["pid"], owner.get("token")) != "absent":
                continue
            result = {"outcome": "unknown", "reason": "上次发送被中断，送达结果未知"}
            item.update(result)
            if item.get("attempt_history"):
                item["attempt_history"][-1].update(result, observed_at=now)
        # Old incomplete records cannot safely receive a fresh budget.
        if item["outcome"] == "success":
            retire(document, item)
            continue
        if "deadline_at" not in item:
            item.update(exhausted=True, exhaustion_reason="missing_budget")
        elif now >= item["deadline_at"]:
            item.update(exhausted=True, exhaustion_reason="deadline")
        elif item["attempts"] >= MAX_ATTEMPTS and item["outcome"] != "success":
            item.update(exhausted=True, exhaustion_reason="max_attempts")
        if (item.get("cancelled") or item.get("exhausted") or item["outcome"] == "success"
                or (item["outcome"] == "unknown" and not item.get("important"))):
            retire(document, item)
