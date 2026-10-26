"""Status bindings live in the existing notification journal, outside business state."""
from copy import deepcopy
from pathlib import Path
from typing import Any, cast
import re
import uuid

from agent_run.contracts.status_comment import StatusProjection
from agent_run.queries.status_comment import project_status
from agent_run.storage import notification_outbox as outbox


class StatusComments:
    def __init__(self, database: Path) -> None:
        self.database = database.absolute()

    @staticmethod
    def _entry(document: dict[str, Any], projection: StatusProjection) -> dict[str, Any]:
        section = document.setdefault("remote_status", {"version": 1, "bindings": {}})
        if not isinstance(section, dict) or section.get("version") != 1 or not isinstance(section.get("bindings"), dict):
            raise ValueError("Status journal version or shape is invalid")
        target = projection.target
        entries = section["bindings"]
        if target.key not in entries:
            entries[target.key] = {"target": target.identity, "marker": f"<!-- agent-run-status:{target.key}:{uuid.uuid4().hex} -->",
                                   "comment_id": None, "confirmed_body": None, "intent": None}
        entry = entries[target.key]
        if (not isinstance(entry, dict) or entry.get("target") != target.identity
                or not isinstance(entry.get("marker"), str)
                or re.fullmatch(r"<!-- agent-run-status:" + target.key + r":[a-f0-9]{32} -->", entry["marker"]) is None):
            raise ValueError("Status binding changed")
        identifier, confirmed, intent = entry.get("comment_id"), entry.get("confirmed_body"), entry.get("intent")
        if ((identifier is not None and (type(identifier) is not int or identifier <= 0))
                or (confirmed is not None and (not isinstance(confirmed, str) or len(confirmed.encode()) > 9216
                                              or not confirmed.startswith(entry["marker"] + "\n")))
                or (identifier is None) != (confirmed is None)):
            raise ValueError("Status confirmed identity is invalid")
        if intent is not None and (not isinstance(intent, dict)
                or set(intent) != {"id", "comment_id", "before", "body", "sent"}
                or not isinstance(intent["id"], str) or re.fullmatch(r"[a-f0-9]{32}", intent["id"]) is None
                or intent["comment_id"] != identifier or intent["before"] != confirmed
                or type(intent["sent"]) is not bool or not isinstance(intent["body"], str)
                or not intent["body"].startswith(entry["marker"] + "\n") or len(intent["body"].encode()) > 9216):
            raise ValueError("Status intent is invalid")
        return entry

    def stage(self, subject: str) -> dict[str, Any] | None:
        projection = project_status(self.database, subject)
        with outbox.transaction(self.database.parent, projection.target.run_id) as document:
            entry = self._entry(document, projection)
            body = entry["marker"] + "\n" + projection.body
            intent = entry["intent"]
            if intent is not None and intent["sent"]:
                return cast(dict[str, Any], deepcopy(intent))  # Never coalesce away an unresolved physical write.
            if intent is not None and intent["body"] == body:
                return cast(dict[str, Any], deepcopy(intent))
            if entry["confirmed_body"] == body:
                return None
            # Only undispatched intermediate display states may be discarded.
            intent = {"id": uuid.uuid4().hex, "comment_id": entry["comment_id"], "before": entry["confirmed_body"],
                      "body": body, "sent": False}
            entry["intent"] = intent
            return cast(dict[str, Any], deepcopy(intent))
