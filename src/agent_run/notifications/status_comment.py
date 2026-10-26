"""Status bindings live in the existing notification journal, outside business state."""
from copy import deepcopy
from pathlib import Path
from collections.abc import Callable
import os
import sqlite3
import time
from typing import Any, cast
import re
import uuid

from agent_run.contracts.status_comment import StatusProjection
from agent_run.contracts.write_authority import WriteAuthority
from agent_run.core.kernel import Kernel
from agent_run.adapters.remote import EffectReadError, PhysicalAPI, RemoteEffects
from agent_run.adapters.auth.app import GitHubCredentialError
from agent_run.errors import GitHubReadError, KernelError
from agent_run.runtime.process_identity import _process_binding_status, _process_start_token
from agent_run.queries.status_comment import project_status
from agent_run.storage import notification_outbox as outbox
from agent_run.storage.routing import open_run
from agent_run.storage.sqlite import Storage


class StatusComments:
    def __init__(self, database: Path, *, storage: Storage | None = None) -> None:
        self.database = database.absolute()
        self._storage = open_run(self.database, storage=storage) if storage is not None else None

    def _projection(self, subject: str) -> StatusProjection:
        return (project_status(self.database, subject) if self._storage is None else
                project_status(self.database, subject, storage=self._storage))

    def _kernel(self) -> Kernel:
        return Kernel(self.database) if self._storage is None else Kernel._from_storage(self._storage)

    @staticmethod
    def _entry(document: dict[str, Any], projection: StatusProjection) -> dict[str, Any]:
        section = document.setdefault("remote_status", {"version": 1, "bindings": {}})
        if not isinstance(section, dict) or type(section.get("version")) is not int or section.get("version") != 1 or not isinstance(section.get("bindings"), dict):
            raise ValueError("Status journal version or shape is invalid")
        target = projection.target
        binding = [target.run_id, target.repository, target.parent_issue]
        if (document.get("native_binding") not in (None, binding)
                or document.get("native_binding") is None and any(document[key] for key in ("pending", "records", "seen"))):
            raise ValueError("Status notification Run binding changed")
        document["native_binding"] = binding
        entries = section["bindings"]
        if target.key not in entries:
            entries[target.key] = {"target": target.identity, "marker": f"<!-- agent-run-status:{target.key}:{uuid.uuid4().hex} -->",
                                   "comment_id": None, "confirmed_body": None, "intent": None}
        entry = entries[target.key]
        if (not isinstance(entry, dict) or not {"target", "marker", "comment_id", "confirmed_body", "intent"} <= entry.keys()
                or entry.get("target") != target.identity
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
        result = entry.get("write_result")
        if result is not None and (not isinstance(result, dict) or set(result) != {"intent", "returncode"}
                or not isinstance(result["intent"], str) or re.fullmatch(r"[a-f0-9]{32}", result["intent"]) is None
                or type(result["returncode"]) is not int):
            raise ValueError("Status write result is invalid")
        owner = entry.get("claim")
        if owner is not None and (not isinstance(owner, dict) or set(owner) != {"pid", "token", "id"}
                or type(owner["pid"]) is not int or owner["pid"] <= 0
                or not isinstance(owner["token"], str) or not owner["token"]
                or not isinstance(owner["id"], str) or re.fullmatch(r"[a-f0-9]{32}", owner["id"]) is None):
            raise ValueError("Status sender identity is invalid")
        return entry

    def stage(self, subject: str) -> dict[str, Any] | None:
        projection = self._projection(subject)
        with outbox.transaction(self.database.parent, projection.target.run_id) as document:
            entry = self._entry(document, projection)
            if entry.get("failure") is not None:
                raise ValueError("Status delivery paused")
            owner = entry.get("claim")
            if owner:
                if _process_binding_status(owner["pid"], owner["token"]) != "absent":
                    return cast(dict[str, Any] | None, deepcopy(entry["intent"]))
                entry.pop("claim")  # Only a proved-absent sender relinquishes preparation.

            return deepcopy(self._stage_entry(entry, projection))

    @staticmethod
    def _stage_entry(entry: dict[str, Any], projection: StatusProjection) -> dict[str, Any] | None:
        if entry.get("failure") is not None:
            raise ValueError("Status delivery paused")
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

    def deliver(self, subject: str, *, api: PhysicalAPI | None = None,
                cancelled: Callable[[], bool] = lambda: False) -> str:
        """One bounded opportunity; the existing sender supplies scheduling/cancellation.

        A sent intent is only reconciled, never blindly replayed. No background
        thread, business transition, process grant or second outbox is created.
        """
        projection, claim = None, None
        deadline = time.monotonic() + outbox.DELIVERY_LIFETIME_SECONDS
        def stopped() -> bool:
            return cancelled() or time.monotonic() >= deadline
        try:
            intent = self.stage(subject)
            if intent is None:
                return "unchanged"
            projection = self._projection(subject)
            target = projection.target
            with outbox.transaction(self.database.parent, target.run_id) as document:
                entry = self._entry(document, projection)
                owner = entry.get("claim")
                if owner and _process_binding_status(owner["pid"], owner["token"]) != "absent":
                    return "busy"
                if entry.get("failure") is not None:
                    raise ValueError("Status delivery paused")
                current_intent = entry["intent"]
                if current_intent is None:
                    current_intent = self._stage_entry(entry, projection)
                if current_intent is None:
                    return "unchanged"
                claim = {"pid": os.getpid(), "token": _process_start_token(os.getpid()), "id": uuid.uuid4().hex}
                entry["claim"] = claim
                intent = deepcopy(current_intent)
                marker = entry["marker"]
            binding = self._kernel().operation_binding()
            remote = RemoteEffects(binding.repository_root, binding.repository,
                                   runner_identity=binding.inputs.runner_identity, api=api, cancelled=stopped)
            def admit() -> None:
                # Slow transport/credential preparation and remote reads are
                # already finished. No remote I/O follows this final check.
                current = self._projection(subject)
                if (stopped() or current != projection or intent["body"] != marker + "\n" + current.body
                        or self._kernel().operation_binding() != binding):
                    raise ValueError("Status authority changed")
                with outbox.transaction(self.database.parent, target.run_id) as document:
                    active = self._entry(document, current)
                    if active.get("claim") != claim or active["intent"] != intent or intent["sent"]:
                        raise ValueError("Status intent changed")
                    active["intent"]["sent"] = True  # Durable BEFORE physical dispatch; never a success claim.
            observed = remote.read_status_comment(target, marker, intent, stopped)
            if observed["disposition"] == "prior" and not intent["sent"]:
                try:
                    observed = remote.write_status_comment(target, marker, intent, WriteAuthority(lambda: admit), stopped)
                except EffectReadError as error:
                    observed = {"disposition": "unknown", "write_returncode": error.write_returncode}
            with outbox.transaction(self.database.parent, target.run_id) as document:
                entry = self._entry(document, projection)
                if entry.get("claim") != claim or entry["intent"]["id"] != intent["id"]:
                    raise ValueError("Status claim changed")
                if "write_returncode" in observed:
                    entry["write_result"] = {"intent": intent["id"], "returncode": observed["write_returncode"]}
                if observed["disposition"] == "target":
                    entry.update(comment_id=observed["comment_id"], confirmed_body=intent["body"], intent=None)
                    entry.pop("diagnostic", None)
                    entry["receipt"] = {"intent": intent["id"], "comment_id": observed["comment_id"],
                                        "write_result": entry.get("write_result") if entry.get("write_result", {}).get("intent") == intent["id"] else None}
                    return "confirmed"
                if observed["disposition"] == "third":
                    entry["failure"] = "foreign_comment_or_publication"
                    return "foreign"
                return "unknown"
        except GitHubCredentialError as error:
            if projection is not None:
                try:
                    with outbox.transaction(self.database.parent, projection.target.run_id) as document:
                        entry = self._entry(document, projection)
                        entry["diagnostic"] = {"code": "github_credential_unavailable", "http_status": error.http_status}
                except (OSError, ValueError, KernelError):
                    pass
            return "unavailable"
        except (OSError, ValueError, KernelError, GitHubReadError, sqlite3.Error):
            # Preserve any sent intent. Arbitrary remote diagnostics, credentials
            # and response bodies must never enter this projection journal/log.
            return "unavailable"
        finally:
            if projection is not None and claim is not None:
                try:
                    with outbox.transaction(self.database.parent, projection.target.run_id) as document:
                        entry = self._entry(document, projection)
                        if entry.get("claim") == claim:
                            entry.pop("claim")
                except (OSError, ValueError, KernelError):
                    pass  # Damaged display state remains fail-closed, never repaired by overwriting it.
