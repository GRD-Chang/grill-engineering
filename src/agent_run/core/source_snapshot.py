"""Shared source snapshot persistence, without delivery transitions or I/O."""
from dataclasses import asdict, dataclass
import hashlib
from typing import Any

from agent_run.errors import KernelError
from agent_run.model import Requirements, wire_json
from agent_run.storage.sqlite import Transaction
from agent_run.storage.run_scope import run_statement


def fingerprint(value: Any) -> str:
    return "sha256:" + hashlib.sha256(wire_json(value)).hexdigest()


def source_revisions(requirements: Requirements) -> tuple[str, str]:
    parent = requirements.parent
    members = sorted(parent.sub_issue_numbers)
    return (fingerprint({"title": parent.title, "body": parent.body}),
            fingerprint({"tickets": members, "dependencies": {
                str(issue.number): sorted(blocker.number for blocker in issue.blocked_by)
                for issue in requirements.issues if issue.number in members}}))


@dataclass(frozen=True)
class SourceSnapshot:
    sequence: int
    parent_revision: str
    graph_revision: str
    graph_problem: str | None


def record_source(tx: Transaction, source: Requirements, *, initial: bool = False,
                  readonly: bool = False) -> SourceSnapshot | bool:
    parent_revision, graph_revision = source_revisions(source)
    order = list(source.parent.sub_issue_numbers)
    if not source.parent.sub_issue_order_reliable:
        order.sort()
    raw = wire_json({"parent": asdict(source.parent), "issues": [
        {**asdict(issue), "labels": sorted(issue.labels)} for issue in source.issues]})
    if len(raw) > 4 * 1024 * 1024:
        raise KernelError("source_limit")
    previous = tx.rows(*run_statement(tx, 'SELECT sequence,body FROM source_observations WHERE run_id=? ORDER BY sequence DESC LIMIT 1',
                   (), run_last=True))
    if previous and previous[0]["body"] == raw:
        return True
    if readonly:
        return False
    tx.execute(*run_statement(tx, 'INSERT INTO source_observations(parent_revision,graph_revision,body,run_id) VALUES (?,?,?,?)',
        (parent_revision, graph_revision, raw), run_last=True))
    sequence = tx.one("SELECT last_insert_rowid()")[0]
    tx.execute(*run_statement(tx, 'INSERT OR IGNORE INTO requirement_versions(issue,revision,title,body,run_id) VALUES (?,?,?,?,?)',
        (source.parent.number, parent_revision, source.parent.title, source.parent.body), run_last=True))
    tx.execute(*run_statement(tx, 'DELETE FROM issue_facts WHERE run_id=?',
        (), run_last=True))
    tx.execute(*run_statement(tx, 'DELETE FROM dependency_facts WHERE run_id=?',
        (), run_last=True))
    by_number = {issue.number: issue for issue in source.issues}
    for issue in source.issues:
        revision = fingerprint({"title": issue.title, "body": issue.body})
        tx.execute(*run_statement(tx, 'INSERT OR IGNORE INTO requirement_versions(issue,revision,title,body,run_id) VALUES (?,?,?,?,?)',
            (issue.number, revision, issue.title, issue.body), run_last=True))
        tx.execute(*run_statement(tx, 'INSERT INTO issue_facts(issue,revision,state,labels,position,run_id) VALUES (?,?,?,?,?,?)',
            (issue.number, revision, issue.state.upper(), wire_json(sorted(issue.labels)), order.index(issue.number) if issue.number in order else -1), run_last=True))
        for blocker in issue.blocked_by:
            tx.execute(*run_statement(tx, 'INSERT INTO dependency_facts(issue,blocker,state,run_id) VALUES (?,?,?,?)',
                (issue.number, blocker.number, blocker.state.upper()), run_last=True))
    tx.execute(*run_statement(tx, 'UPDATE runs SET source_sequence=?,observed_parent_revision=?,observed_graph_revision=? WHERE id=?',
        (sequence, parent_revision, graph_revision), run_last=True))
    if initial:
        initial_jobs = {row["issue"]: row["id"] for row in tx.all(*run_statement(tx, "SELECT id,issue FROM subjects WHERE kind='ticket' AND run_id=?",
                                                               (), run_last=True))}
        for issue in source.issues:
            for blocker in issue.blocked_by:
                if issue.number in initial_jobs and blocker.number in initial_jobs:
                    tx.execute(*run_statement(tx, 'INSERT INTO dependencies(subject,predecessor,run_id) VALUES (?,?,?)',
                        (initial_jobs[issue.number], initial_jobs[blocker.number]), run_last=True))
    graph_problem = "missing_ticket" if any(number not in by_number for number in order) else None
    edges = {number: {blocker.number for blocker in by_number[number].blocked_by if blocker.number in order}
             for number in order if number in by_number}
    remaining = set(edges)
    while remaining:
        ready = {number for number in remaining if not (edges[number] & remaining)}
        if not ready:
            graph_problem = "dependency_cycle"
            break
        remaining -= ready
    return SourceSnapshot(sequence, parent_revision, graph_revision, graph_problem)
