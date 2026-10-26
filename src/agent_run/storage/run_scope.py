"""Staged Run selection and immutable material access on one transaction.

This module neither opens a database nor grants execution. Storage's protocol,
file-identity, transaction and capacity checks remain the caller's responsibility.
It is not wired into a public producer until all repository consumers are scoped.
There is no SQL rewriting, cached business state or second writable store here.
"""
from dataclasses import dataclass
import hashlib
from pathlib import Path
import sqlite3

from agent_run.contracts.repository import normalize_repository
from agent_run.errors import KernelError
from agent_run.model import wire_json
from agent_run.storage.sqlite import MATERIAL_LIMIT, QUERY_LIMIT, Transaction


@dataclass(frozen=True)
class RunScope:
    run_id: str
    repository: str
    parent_issue: int
    repository_root: str


class RunTransaction(Transaction):
    """One explicit selection; consumers must still scope their domain SQL.

    Only the material interface is migrated in this first bounded slice. The
    inherited query interface deliberately does not guess or rewrite SQL.
    Current Core, CLI, Host and protocol selection do not construct this class.
    """

    def __init__(self, connection: sqlite3.Connection, run_id: str) -> None:
        super().__init__(connection)
        if type(run_id) is not str or not run_id:
            raise KernelError("run_scope_invalid")
        columns = self.execute("PRAGMA table_info(material)").fetchmany(32)
        primary = [row["name"] for row in sorted(columns, key=lambda row: row["pk"]) if row["pk"]]
        if primary != ["run_id", "id"]:
            raise KernelError("run_scope_schema_required")
        self._scope = self._read_scope(run_id)

    @property
    def run_id(self) -> str:
        return self._scope.run_id

    @property
    def scope(self) -> RunScope:
        return self._scope

    def _read_scope(self, run_id: str) -> RunScope:
        rows = self.rows("SELECT id,repository,parent_issue,repository_root FROM runs WHERE id=?", (run_id,), limit=1)
        if not rows:
            raise KernelError("run_scope_missing")
        row = rows[0]
        try:
            repository = normalize_repository(row["repository"])
        except (TypeError, ValueError, AttributeError) as error:
            raise KernelError("run_scope_invalid") from error
        root, parent = row["repository_root"], row["parent_issue"]
        if (type(parent) is not int or parent <= 0 or type(root) is not str
                or not Path(root).is_absolute()):
            raise KernelError("run_scope_invalid")
        return RunScope(run_id, repository, parent, root)

    def _require_scope(self) -> None:
        if self._read_scope(self.run_id) != self.scope:
            raise KernelError("run_scope_changed")

    def require_scope(self) -> RunScope:
        """Revalidate the selected identity for a migrated fact consumer."""
        self._require_scope()
        return self.scope

    def _material_identity(self, subject: str, purpose: str, digest: str) -> str:
        return hashlib.sha256(wire_json([self.run_id, subject, purpose, digest])).hexdigest()

    def put(self, subject: str, purpose: str, body: bytes) -> str:
        self._require_scope()
        if len(body) > MATERIAL_LIMIT:
            raise KernelError("material_limit")
        digest = hashlib.sha256(body).hexdigest()
        identity = self._material_identity(subject, purpose, digest)
        self.execute("INSERT OR IGNORE INTO material(run_id,id,subject,purpose,digest,size,body) VALUES (?,?,?,?,?,?,?)",
                     (self.run_id, identity, subject, purpose, digest, len(body), body))
        if self.get(identity, subject) != body:
            raise KernelError("material_corrupt")
        return identity

    def get(self, identity: str, subject: str) -> bytes:
        if (type(identity) is not str or len(identity) != 64
                or any(character not in "0123456789abcdef" for character in identity)):
            raise KernelError("material_corrupt")
        self._require_scope()
        rows = self.rows("SELECT subject,purpose,digest,size,typeof(body),length(body) FROM material WHERE run_id=? AND id=?",
                         (self.run_id, identity), limit=1)
        if not rows:
            raise KernelError("material_corrupt", identity)
        header = rows[0]
        if (header["subject"] != subject or header[4] != "blob" or header["size"] != header[5]
                or not 0 <= header[5] <= MATERIAL_LIMIT
                or self._material_identity(subject, header["purpose"], header["digest"]) != identity):
            raise KernelError("material_corrupt", identity)
        if self.bytes_read + header[5] > QUERY_LIMIT:
            raise KernelError("query_limit")
        body = self.one("SELECT body FROM material WHERE run_id=? AND id=?", (self.run_id, identity))[0]
        if hashlib.sha256(body).hexdigest() != header["digest"]:
            raise KernelError("material_corrupt", identity)
        return bytes(body)
