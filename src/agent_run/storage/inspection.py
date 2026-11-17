"""Snapshot-only Host and Worker bindings, without execution or mutation access."""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Mapping

from agent_run.errors import KernelError
from agent_run.model import Execution, HostLaunch, HostObservation, Role
from agent_run.storage.run_scope import run_statement, transaction_scope
from agent_run.storage.sqlite import Transaction


@dataclass(frozen=True)
class WorkerBinding:
    subject: str
    role: Role
    attempt_id: str
    invocation_id: str
    output_ordinal: int
    execution: Execution
    output: Mapping[str, Any]
    thread: Mapping[str, Any]
    process: Mapping[str, Any] | None


@dataclass(frozen=True)
class InspectionBindings:
    """Captured facts only; physical observers cannot reopen the database."""
    database: Path
    current: tuple[str, str | None, int]
    request: HostLaunch | None
    host: Mapping[str, Any] | None
    usage: HostObservation | None
    domain: Mapping[str, Any] | None
    workers: tuple[WorkerBinding, ...]


def capture(tx: Transaction, database: Path, *, request: HostLaunch | None = None) -> InspectionBindings:
    scope = transaction_scope(tx)
    run = tx.one(*run_statement(tx, 'SELECT id,executor,epoch FROM runs WHERE id=?', (), run_last=True))
    current = (run['id'], run['executor'], run['epoch'])
    selected = current if request is None else (request.run_id, request.owner, request.epoch)
    if selected[0] != scope.run_id:
        raise KernelError('host_binding_changed')
    hosts = tx.rows('SELECT * FROM hosts WHERE run_id=? AND owner=? AND epoch=?', selected)
    if not hosts:
        return InspectionBindings(database.resolve(), current, None, None, None, None, ())
    if len(hosts) != 1:
        raise KernelError('host_binding_changed')
    host = dict(hosts[0])
    value = json.loads(tx.get(host['request_id'], host['subject']))
    captured_request = HostLaunch(value['run_id'], value['epoch'], value['owner'], value['nonce'], value['unit_name'],
                         tuple(value['command']), Path(value['environment_carrier']), value['backend'])
    if request is not None and captured_request != request:
        raise KernelError('host_binding_changed')
    request = captured_request
    if ((request.run_id, request.owner, request.epoch) != selected
            or (request.nonce, request.backend, request.unit_name) != (host['nonce'], host['backend'], host['unit_name'])):
        raise KernelError('host_binding_changed')
    receipts = tx.rows(*run_statement(tx, "SELECT material_id FROM history WHERE subject=? AND identity=? "
        "AND kind IN ('host_usage_pending','host_usage_ready','host_usage_released') AND run_id=? ORDER BY rowid DESC LIMIT 1",
        (host['subject'], host['nonce']), run_last=True))
    usage = None if not receipts else HostObservation(**json.loads(tx.get(receipts[0][0], host['subject'])))
    domain = None if host['domain_id'] is None else json.loads(tx.get(host['domain_id'], host['subject']))
    workers = []
    for item in tx.rows('SELECT i.id,max(o.ordinal) AS ordinal FROM subjects s '
            'JOIN attempts a ON a.run_id=s.run_id AND a.id=s.current_attempt AND a.generation=s.generation '
            'JOIN invocations i ON i.run_id=a.run_id AND i.id=a.current_invocation '
            'JOIN invocation_outputs o ON o.run_id=i.run_id AND o.invocation=i.id '
            'WHERE s.run_id=? AND i.executor=? AND i.epoch=? GROUP BY i.id ORDER BY s.id', selected):
        row = dict(tx.one(*run_statement(tx, 'SELECT i.*,o.*,a.subject,a.role,a.purpose,a.generation,'
            'a.current_invocation FROM invocations i JOIN invocation_outputs o ON o.run_id=i.run_id AND o.invocation=i.id '
            'JOIN attempts a ON a.run_id=i.run_id AND a.id=i.attempt WHERE i.id=? AND o.ordinal=? AND i.run_id=?',
            (item['id'], item['ordinal']), run_last=True)))
        thread = dict(tx.one(*run_statement(tx, 'SELECT * FROM threads WHERE id=? AND run_id=?', (row['thread'],), run_last=True)))
        job = tx.one(*run_statement(tx, 'SELECT kind FROM subjects WHERE id=? AND run_id=?', (row['subject'],), run_last=True))
        role_matches = (thread['role'] == row['role'] or row['role'] == 'publication'
                        and job['kind'] != 'final_run' and thread['role'] == 'development')
        if (thread['subject'] != row['subject'] or not role_matches
                or row['current_invocation'] != item['id'] or row['run_id'] != scope.run_id):
            raise KernelError('invocation_binding')
        process = None if row['process_identity'] is None else json.loads(tx.get(row['process_identity'], row['subject']))
        workers.append(WorkerBinding(row['subject'], Role(row['role']), row['attempt'], item['id'], item['ordinal'],
            Execution(scope.run_id, row['executor'], row['epoch']), row, thread, process))
    return InspectionBindings(database.resolve(), current, request, host, usage, domain, tuple(workers))
