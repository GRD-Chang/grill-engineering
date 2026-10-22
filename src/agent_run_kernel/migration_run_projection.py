"""Ephemeral prepared-Run contract, derived only from authenticated source facts.

There is no comparison database or saved current-state copy. Core still creates
the sole candidate. The source row contracts below are also used by its writer.
Every native table/column is classified; new schema requires explicit review.
"""
from __future__ import annotations

from collections import Counter
from contextlib import closing
from dataclasses import asdict
import hashlib
import math
from pathlib import Path
import sqlite3
from typing import Any, Mapping

from .kernel import Kernel
from .migration import MigrationRefused, _created_at_epoch
from .migration_records import record_manifest
from .migration_run import OriginalRunFacts, UnallocatedTicket, WorkedTicket
from .migration_run_install import (attempt_values, attempt_witness, completion_values, imported_invocation,
    original_run_spec, run_head_evidence, subject_proofs, worked_ticket_values, _thread_id)
from .migration_run_proof import ResourceProof, run_import_document
from .model import DeliveryKind, Purpose, wire_json
from .storage import PROTOCOL, SCHEMA, Transaction


# Static column inventory, deliberately independent of the candidate's schema.
_COLUMNS: dict[str, tuple[str, ...]] = {
    'actions': tuple('id run_id kind subject payload_id status receipt_id'.split()),
    'attempts': tuple('id subject generation ordinal role purpose window parent_revision graph_revision revision base_sha candidate_sha candidate_tree expected_tree completion_revision context_id current_invocation result_id status accounted outcome new_thread resume_action'.split()),
    'budgets': tuple('subject purpose window developments_used reviews_used development_limit review_limit review_required current'.split()),
    'checks': tuple('id subject pr_number head_sha result material_id'.split()),
    'completions': tuple('subject generation revision parent_revision graph_revision source_sha merge_sha tree pr_number gate_mode review_attempt development_attempt comment_effect close_effect'.split()),
    'dependencies': tuple('subject predecessor'.split()),
    'dependency_facts': tuple('issue blocker state'.split()),
    'effect_failures': tuple('effect dispatch_serial evidence observed_at'.split()),
    'effects': tuple('id subject operation status executor epoch parent_revision graph_revision revision generation source_ref target_ref before_sha after_sha candidate_sha candidate_tree base_sha base_tree pr_number actor payload_id receipt_id attempts retry_baseline control_action'.split()),
    'final_barriers': tuple('subject generation completion_revision run_sha default_sha evidence'.split()),
    'final_target': tuple('id source_ref target_ref checkout owned_root common_git_dir'.split()),
    'gates': tuple('subject code purpose evidence'.split()),
    'grants': tuple('id subject action pr_number head_sha base_sha review_attempt checks_id revision parent_revision graph_revision completion_revision'.split()),
    'history': tuple('position subject kind identity material_id generation created_at phase purpose status'.split()),
    'hosts': tuple('run_id epoch owner nonce action_id subject request_id unit_name allocated_at dispatched_at status invocation_id cgroup boot_id main_pid start_token exit_status ended_at observation_id usage_ready usage_released'.split()),
    'integration_queue': tuple('subject ready'.split()),
    'integration_snapshots': tuple('subject window allowance_generation source_ref target_ref source_sha target_sha open'.split()),
    'invocation_outputs': tuple('invocation ordinal context_id lease raw_id dispatch_state process_identity exit_code start_evidence allocated_at dispatched_at started_at returned_at exited_at mode requested_thread failure_kind recovery_ready_at'.split()),
    'invocations': tuple('id attempt thread executor epoch status allocated_at deadline_seconds deadline_at predecessor recovery_kind action_id'.split()),
    'issue_facts': tuple('issue revision state labels position'.split()),
    'issue_links': tuple('subject generation source_ref source_sha issue state effect_id'.split()),
    'leases': tuple('id subject invocation executor epoch'.split()),
    'material': tuple('id subject purpose digest size body'.split()),
    'meta': tuple('protocol'.split()),
    'profile_revisions': tuple('id subject material_id created_at'.split()),
    'requirement_versions': tuple('issue revision title body'.split()),
    'resource_cleanup': tuple('subject generation source_ref source_sha checkout workspace_sha owned_root common_git_dir stage state effect_id outcome'.split()),
    'resync_allowances': tuple('subject generation used allowance current'.split()),
    'retired_executors': tuple('run_id owner epoch status'.split()),
    'retired_generations': tuple('subject generation revision source_ref checkout source_sha candidate_sha publication_sha pr_number reason validation_checkout validation_cleanup workspace_sha owned_root common_git_dir'.split()),
    'run_inputs': tuple('run_id subject material_id'.split()),
    'runs': tuple('id repository parent_issue parent_revision graph_revision accepted_parent_revision concurrency policy executor epoch stopped final_action integration_holder next_ready blocked source_sequence observed_parent_revision observed_graph_revision language repository_root created_at profile_revision'.split()),
    'source_observations': tuple('sequence parent_revision graph_revision body'.split()),
    'subjects': tuple('id run_id kind issue revision generation version phase purpose source_ref target_ref base_sha base_tree source_sha candidate_sha candidate_tree integrated_sha integrated_tree expected_tree integration_target completion_revision current_attempt review_attempt publication_attempt publication_sha publication_tree pr_number checks_id grant_id merge_sha gate_mode development_thread ci_failure last_failure checkout workspace_sha resume_purpose repair_source validation_checkout validation_cleanup owned_root common_git_dir refresh_revision policy_id'.split()),
    'threads': tuple('id subject role profile_revision backend_id'.split()),
    'waits': tuple('subject kind scope_id started_at deadline retry_count next_retry_at evidence'.split()),
    'work_queue': tuple('subject ready'.split()),
    'worker_activity': tuple('invocation ordinal identity active observed_at evidence'.split()),
}
_CLOCK = '<non-authorizing mechanical timestamp>'


def _refuse() -> MigrationRefused:
    return MigrationRefused('native_original_cutover_advanced')


class _Projection:
    def __init__(self) -> None:
        self.rows: dict[str, list[dict[str, Any]]] = {table: [] for table in _COLUMNS}
        self.materials: dict[str, dict[str, Any]] = {}
        # Schema/default metadata only. No Run, installer or business transition
        # is executed here, and no candidate value supplies an expectation.
        with closing(sqlite3.connect(':memory:')) as schema:
            schema.row_factory = sqlite3.Row
            schema.executescript(SCHEMA)
            tables = {row[0] for row in schema.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
            if tables != set(_COLUMNS):
                raise MigrationRefused('native_installation_schema_unclassified')
            self.schema = {table: [dict(row) for row in schema.execute(f'PRAGMA table_info("{table}")')]
                           for table in _COLUMNS}
            self.defaults = {}
            for table, info in self.schema.items():
                if tuple(row['name'] for row in info) != _COLUMNS[table]:
                    raise MigrationRefused('native_installation_schema_unclassified')
                self.defaults[table] = {row['name']: None if row['dflt_value'] is None else
                    schema.execute('SELECT ' + row['dflt_value']).fetchone()[0] for row in info}

    def add(self, table: str, **values: Any) -> dict[str, Any]:
        if not set(values) <= set(_COLUMNS[table]):
            raise MigrationRefused('native_installation_schema_unclassified')
        row = {**self.defaults[table], **values}
        if any(info['notnull'] and row[info['name']] is None for info in self.schema[table]):
            raise MigrationRefused('native_installation_contract_incomplete')
        self.rows[table].append(row)
        return row

    def put(self, subject: str, purpose: str, body: bytes) -> str:
        digest = hashlib.sha256(body).hexdigest()
        identity = hashlib.sha256(f'{subject}\0{purpose}\0{digest}'.encode()).hexdigest()
        self.materials[identity] = dict(id=identity, subject=subject, purpose=purpose,
                                      digest=digest, size=len(body), body=body)
        return identity

    def history(self, subject: str, kind: str, identity: str, material_id: str | None,
                created_at: Any, **values: Any) -> None:
        self.add('history', subject=subject, kind=kind, identity=identity, material_id=material_id,
                 generation=1, created_at=created_at, **values)


def _expected(facts: OriginalRunFacts, origin: Mapping[str, str], proof: Mapping[str, Any], proof_sha: str,
              resources: Mapping[str, tuple[ResourceProof, ...]], run_head: str,
              limits: Mapping[Purpose, tuple[int, int | None]]) -> _Projection:
    state = _Projection()
    header = facts.header
    spec = original_run_spec(facts, header.task.workspace, Path(origin['source_db']).parent)
    anchor = min(ticket.owner.subject for ticket in facts.tickets)
    first = facts.tickets[0].owner.subject
    put = state.put
    state.add('meta', protocol=PROTOCOL)
    state.add('runs', id=header.run_id, repository=header.repository, parent_issue=header.parent_number,
        parent_revision=header.parent_revision, graph_revision=header.graph_revision,
        accepted_parent_revision=header.parent_revision, concurrency=header.policy.concurrency,
        policy=wire_json(Kernel._policy_values(header.policy)), language=header.language,
        repository_root=str(header.task.workspace), created_at=header.created_at, source_sequence=1,
        observed_parent_revision=header.parent_revision, observed_graph_revision=header.graph_revision)
    profile_id = put(anchor, 'profile_revision', wire_json({**header.inputs.profiles, 'source': {'operation': 'create'}}))
    state.add('profile_revisions', id=1, subject=anchor, material_id=profile_id, created_at=_CLOCK)
    inputs: dict[str, Any] = {name: dict(getattr(header.inputs, name)) for name in (
        'prompt_resources', 'worker_identity', 'runner_identity', 'notifications', 'provenance')}
    inputs['repository_remote'] = header.inputs.repository_remote
    state.add('run_inputs', run_id=header.run_id, subject=anchor,
              material_id=put(anchor, 'creation_inputs', wire_json(inputs)))
    raw = wire_json({'parent': asdict(header.requirements.parent), 'issues': [
        {**asdict(issue), 'labels': sorted(issue.labels)} for issue in header.requirements.issues]})
    state.add('source_observations', sequence=1, parent_revision=header.parent_revision,
              graph_revision=header.graph_revision, body=raw)
    state.add('requirement_versions', issue=header.parent_number, revision=header.parent_revision,
              title=header.requirements.parent.title, body=header.requirements.parent.body)
    order = list(header.requirements.parent.sub_issue_numbers)
    issues = {issue.number: issue for issue in header.requirements.issues}
    subjects = {ticket.owner.ticket: ticket.owner.subject for ticket in facts.tickets}
    for issue in header.requirements.issues:
        revision = Kernel._fingerprint({'title': issue.title, 'body': issue.body})
        state.add('requirement_versions', issue=issue.number, revision=revision, title=issue.title, body=issue.body)
        state.add('issue_facts', issue=issue.number, revision=revision, state=issue.state.upper(),
                  labels=wire_json(sorted(issue.labels)), position=order.index(issue.number) if issue.number in order else -1)
        for edge in issue.blocked_by:
            state.add('dependency_facts', issue=issue.number, blocker=edge.number, state=edge.state.upper())
            if issue.number in subjects and edge.number in subjects:
                state.add('dependencies', subject=subjects[issue.number], predecessor=subjects[edge.number])
    boot_status = wire_json({'stopped': False, 'blocked': None, 'gates': [], 'attempt_status': None}).decode()
    state.history(first, 'source_refreshed', '1', None, _CLOCK,
                  phase='workspace', purpose='requirements', status=boot_status)
    state.history(anchor, 'v6_original_cutover', origin['nonce'],
        put(anchor, 'v6_original_cutover', wire_json(dict(origin))), _CLOCK,
        phase='workspace', purpose='requirements', status=boot_status)
    from .migration_run_external import verify_run_external
    _, closes = verify_run_external(facts, proof, header.task.workspace)
    seen_threads = set()
    for ticket, delivery in zip(facts.tickets, spec.deliveries):
        subject = ticket.owner.subject
        issue = issues[ticket.owner.ticket]
        revision = Kernel._fingerprint({'title': issue.title, 'body': issue.body})
        job = state.add('subjects', id=subject, run_id=header.run_id, kind='ticket', issue=issue.number,
            revision=revision, phase='workspace', purpose='requirements', source_ref=delivery.source_ref,
            target_ref=delivery.target_ref, base_sha=delivery.base.sha, base_tree=delivery.base.tree,
            source_sha=delivery.base.sha, workspace_sha=delivery.base.sha,
            completion_revision=delivery.completion_revision, checkout=delivery.checkout,
            owned_root=delivery.owned_root, common_git_dir=delivery.common_git_dir)
        if isinstance(ticket, WorkedTicket):
            job.update(worked_ticket_values(facts, ticket, header.task.workspace))
        used = (0, 0) if isinstance(ticket, UnallocatedTicket) else (ticket.budget.developments_used, ticket.budget.reviews_used)
        for purpose, (development_limit, review_limit) in limits.items():
            counts = used if purpose == Purpose.REQUIREMENTS else (0, 0)
            required = purpose == Purpose.REQUIREMENTS or purpose == Purpose.CI and header.policy.ticket_ci_review
            state.add('budgets', subject=subject, purpose=purpose.value, window=1,
                developments_used=counts[0], reviews_used=counts[1], development_limit=development_limit,
                review_limit=review_limit, review_required=int(required))
        state.add('resync_allowances', subject=subject, generation=1, allowance=header.policy.resync_limit)
        if isinstance(ticket, UnallocatedTicket):
            continue
        if ticket.integration is not None:
            state.add('integration_snapshots', subject=subject, window=1, allowance_generation=1,
                source_ref=ticket.source_ref, target_ref='refs/heads/' + header.run_branch,
                source_sha=ticket.base_sha, target_sha=ticket.integration_target, open=0)
        for ordinal, call in enumerate(ticket.calls, start=1):
            thread = _thread_id(call)
            if thread not in seen_threads:
                state.add('threads', id=thread, subject=subject, role=call.binding['role'],
                          profile_revision=1, backend_id=call.thread_id)
                put(subject, 'v6_thread_binding', wire_json(dict(call.binding)))
                seen_threads.add(thread)
            context = put(subject, 'v6_attempt_input_witness', wire_json(
                attempt_witness(facts, ticket, call, revision, delivery.completion_revision)))
            result = put(subject, 'v6_normalized_artifact', wire_json(dict(call.normalized)))
            state.add('attempts', **attempt_values(facts, ticket, call, ordinal,
                revision, delivery.completion_revision, context, result))
            state.history(subject, 'imported_invocation', ':'.join((header.run_id, subject, call.invocation['started_at'])),
                put(subject, 'v6_imported_invocation', wire_json(imported_invocation(call))),
                _created_at_epoch(call.invocation['ended_at']))
        if ticket.kind == 'completed':
            # External/Git validation precedes this pure source-to-row conversion.
            for table, rows in completion_values(facts, ticket, revision, job['base_tree'], closes[subject],
                    lambda purpose, body: put(subject, purpose, body)).items():
                for row in rows:
                    state.add(table, **row)
    final = spec.deliveries[-1]
    state.add('final_target', id=final.key, source_ref=final.source_ref, target_ref=final.target_ref,
              checkout=final.checkout, owned_root=final.owned_root, common_git_dir=final.common_git_dir)
    entries = []
    for item in sorted(subject_proofs(facts, resources), key=lambda item: item.owner.subject):
        subject = item.owner.subject
        material = put(subject, 'v6_subject_import', wire_json(item.document(origin)))
        state.history(subject, 'v6_subject_import', origin['nonce'], material, header.created_at)
        entries.append(dict(subject=subject, generation=1, proof_id=material))
    heads = run_head_evidence(facts, run_head)
    manifest = run_import_document(origin=origin, profile_sha256=facts.profile_sha256,
        external_readproof_sha256=proof_sha, run_ref='refs/heads/' + header.run_branch,
        run_head=run_head, head_evidence=heads, entries=entries)
    state.history(anchor, 'v6_run_import', origin['nonce'], put(anchor, 'v6_run_import', wire_json(manifest)), header.created_at)
    state.history(anchor, 'v6_external_readproof', proof_sha,
                  put(anchor, 'v6_external_readproof', wire_json(dict(proof))), header.created_at)
    archive = record_manifest(facts.snapshot, run_id=header.run_id, profile_document=facts.profile_document,
                             whole_source=True, put=lambda purpose, body: put(anchor, purpose, body))
    state.history(anchor, 'v6_record_import', header.run_id,
                  put(anchor, 'v6_record_manifest', wire_json(archive)), _CLOCK)
    return state


def verify_prepared_installation(native: Kernel, facts: OriginalRunFacts, origin: Mapping[str, str],
        proof: Mapping[str, Any], proof_sha: str, resources: Mapping[str, tuple[ResourceProof, ...]], run_head: str) -> None:
    """Compare the complete installation, not a hash of native database bytes.

    Allowed differences are narrow and non-authorizing: profile/Core/archive
    audit timestamps, history row numbers, SQLite allocation metadata, and
    unreferenced valid immutable material. Exact-origin rollback audit rows are
    additionally allowed; no executor, Action, gate, role result or other
    business change is accepted. All referenced material is exact and validated.
    """
    # Progress is diagnosed before re-reading old Git resources, which native
    # cleanup may already have legitimately retired. This reads no expectation.
    with native.storage.transaction(write=False) as tx:
        run = tx.one('SELECT executor,epoch FROM runs')
        if run['executor'] is not None or run['epoch'] != 0:
            raise _refuse()
    expected = _expected(facts, origin, proof, proof_sha, resources, run_head,
                         native._budget_limits(facts.header.policy, DeliveryKind.TICKET))
    with native.storage.transaction(write=False) as tx:
        if tx.execute('PRAGMA foreign_key_check').fetchone() is not None:
            raise _refuse()
        tables = {row[0] for row in tx.all("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        if tables != set(_COLUMNS):
            raise _refuse()
        for table, columns in _COLUMNS.items():
            if [dict(row) for row in tx.execute(f'PRAGMA table_info("{table}")').fetchall()] != expected.schema[table]:
                raise _refuse()
            if table in {'material', 'history'}:
                continue
            actual = [dict(row) for row in tx.all(f'SELECT * FROM "{table}"')]
            if table == 'profile_revisions':
                for row in actual:
                    if not isinstance(row['created_at'], (int, float)) or not math.isfinite(row['created_at']):
                        raise _refuse()
                    row['created_at'] = _CLOCK
            if Counter(tuple(row[name] for name in columns) for row in actual) != Counter(
                    tuple(row[name] for name in columns) for row in expected.rows[table]):
                raise _refuse()
        materials = {row['id']: dict(row) for row in tx.all('SELECT * FROM material')}
        for identity, row in materials.items():
            body = tx.get(identity, row['subject'])
            digest = hashlib.sha256(body).hexdigest()
            if identity != hashlib.sha256(f"{row['subject']}\0{row['purpose']}\0{digest}".encode()).hexdigest():
                raise _refuse()
        if any(materials.get(identity) != row for identity, row in expected.materials.items()):
            raise _refuse()
        actual_history = [dict(row) for row in tx.all('SELECT * FROM history ORDER BY position')]
        wanted = expected.rows['history']
        clocks = {(row['subject'], row['kind'], row['identity']) for row in wanted if row['created_at'] == _CLOCK}
        rollback = [row for row in actual_history if row['kind'] in {
            'v6_original_rollback_started', 'v6_original_rollback_complete'}]
        if rollback:
            anchor = min(ticket.owner.subject for ticket in facts.tickets)
            rollback_material = expected.put(anchor, 'v6_original_rollback', wire_json(dict(origin)))
            if materials.get(rollback_material) != expected.materials[rollback_material]:
                raise _refuse()
            if [row['kind'] for row in rollback] not in ([
                    'v6_original_rollback_started'], ['v6_original_rollback_started', 'v6_original_rollback_complete']):
                raise _refuse()
            for row in rollback:
                wanted.append(dict(position=None, subject=anchor, kind=row['kind'], identity=origin['nonce'],
                    material_id=rollback_material, generation=1, created_at=_CLOCK, phase=None, purpose=None, status=None))
                clocks.add((anchor, row['kind'], origin['nonce']))
        for row in actual_history:
            row['position'] = None  # allocation numbers carry no source/execution permission
            if (row['subject'], row['kind'], row['identity']) in clocks:
                if not isinstance(row['created_at'], (int, float)) or not math.isfinite(row['created_at']):
                    raise _refuse()
                row['created_at'] = _CLOCK
        columns = _COLUMNS['history']
        if Counter(tuple(row[name] for name in columns) for row in actual_history) != Counter(
                tuple(row[name] for name in columns) for row in wanted):
            raise _refuse()
