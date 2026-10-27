"""Exact Run-owned continuation records; policy stays with the original owner."""
import sqlite3
from typing import Any, Sequence, cast

from agent_run.storage.run_scope import RunTransaction, run_statement, transaction_scope
from agent_run.storage.sqlite import Transaction


class AttemptRecords:
    """Select exact records; continuation policy remains in its existing owner."""

    def __init__(self, transaction: Transaction) -> None:
        self.transaction = transaction
        transaction_scope(transaction)

    def _selection(self, statement: str, values: Sequence[Any]) -> tuple[str, Sequence[Any]]:
        return run_statement(self.transaction, statement, values)

    def _one(self, statement: str, values: Sequence[Any]) -> sqlite3.Row:
        sql, parameters = self._selection(statement, values)
        return self.transaction.one(sql, parameters)

    def attempt(self, identity: str) -> sqlite3.Row:
        return self._one("SELECT * FROM attempts WHERE run_id=? AND id=?", (identity,))

    def thread(self, identity: str) -> sqlite3.Row:
        return self._one("SELECT * FROM threads WHERE run_id=? AND id=?", (identity,))

    def subject_kind(self, subject: str) -> sqlite3.Row:
        return self._one("SELECT kind FROM subjects WHERE run_id=? AND id=?", (subject,))

    def profile_revision(self, revision: int) -> sqlite3.Row:
        return self._one("SELECT subject,material_id FROM profile_revisions WHERE run_id=? AND id=?", (revision,))

    def has_invocation(self, attempt: str) -> bool:
        sql, values = self._selection("SELECT id FROM invocations WHERE run_id=? AND attempt=? LIMIT 1", (attempt,))
        return bool(self.transaction.rows(sql, values))

    def bind_opening(self, attempt: str, thread: str, opening: str, context: str) -> None:
        run = transaction_scope(self.transaction).run_id
        self.transaction.execute("UPDATE attempts SET thread_id=?,opening_id=?,context_id=? WHERE run_id=? AND id=?",
                                 (thread, opening, context, run, attempt))

    def invocation_lineage(self, attempt: str) -> list[sqlite3.Row]:
        sql, values = self._selection("SELECT id,thread,predecessor,recovery_kind,deadline_at FROM invocations WHERE run_id=? AND attempt=?", (attempt,))
        return self.transaction.all(sql, values)

    def latest_output(self, invocation: str, attempt: str) -> sqlite3.Row:
        return self._one("SELECT i.*,o.* FROM invocations i JOIN invocation_outputs o ON o.run_id=i.run_id AND o.invocation=i.id "
            "WHERE i.run_id=? AND i.id=? AND i.attempt=? ORDER BY ordinal DESC LIMIT 1", (invocation, attempt))

    def recovery_counts(self, attempt: str) -> sqlite3.Row:
        return self._one("SELECT coalesce(sum(recovery_kind='ordinary'),0),coalesce(sum(recovery_kind='capacity'),0) "
            "FROM invocations WHERE run_id=? AND attempt=?", (attempt,))

    def output_ordinal(self, attempt: str) -> int:
        return cast(int, self._one("SELECT coalesce(max(o.ordinal),0) FROM invocation_outputs o JOIN invocations i ON i.run_id=o.run_id AND i.id=o.invocation "
            "WHERE i.run_id=? AND i.attempt=?", (attempt,))[0])
