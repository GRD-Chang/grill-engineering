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
import json
import math
from pathlib import Path
import sqlite3
from typing import Any, Mapping

from .kernel import Kernel
from .migration import MigrationRefused, _created_at_epoch
from .migration_records import record_manifest
from .model import DeliveryKind, Purpose, Requirements, wire_json
from .storage import PROTOCOL, SCHEMA, Transaction
from .migration_facts import ImportedRunFacts
from .migration_run_install import (imported_subject_values, imported_attempt_values, imported_witness,
    imported_thread, imported_execution, imported_completion_rows, import_object)
from .migration_run_proof import imported_proof_documents


# Static column inventory, deliberately independent of the candidate's schema.
_COLUMNS: dict[str, tuple[str, ...]] = {
    'actions': tuple('id run_id kind subject payload_id status receipt_id'.split()),
    'attempts': tuple('id subject generation ordinal role purpose window parent_revision graph_revision revision base_sha candidate_sha candidate_tree expected_tree completion_revision context_id current_invocation result_id status accounted outcome new_thread resume_action thread_id opening_id'.split()),
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
# Prepared candidates have the Core-created schema, with no DDL changes
# permitted. Root-page numbers are physical allocation, not schema semantics.
_SCHEMA_OBJECTS = 'SELECT type,name,tbl_name,sql FROM sqlite_schema ORDER BY type,name'
# This is a closed exception list, not an assumption that missing foreign-key
# references imply inert data. Core also discovers material by subject/purpose
# (fixture_evidence), and migration can discover v6_import_identity likewise.
# Every other material must belong to the source-derived installation below.
# In particular, Opening, its provenance and prior raw/process/start evidence
# are authoritative material, never inert exceptions. This importer has no
# certified Opening producer: its Attempt basis stays double-NULL, so that
# entire extra material closure remains absent from the expected installation.
_UNREFERENCED_MATERIAL = {
    'inspection_note': 'diagnostic-only; no runtime or migration selector',
    'profile_revision': 'only consumed through exact profile_revisions and Thread bindings',
}


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
            self.objects = [dict(row) for row in schema.execute(_SCHEMA_OBJECTS)]
            self.schema = {table: [dict(row) for row in schema.execute(f'PRAGMA table_xinfo("{table}")')]
                           for table in _COLUMNS}
            self.defaults = {}
            for table, info in self.schema.items():
                if tuple(row['name'] for row in info) != _COLUMNS[table]:
                    raise MigrationRefused('native_installation_schema_unclassified')
                self.defaults[table] = {row['name']: None if row['dflt_value'] is None else
                    schema.execute('SELECT ' + row['dflt_value']).fetchone()[0] for row in info}

    def verify_schema(self, tx: Transaction) -> None:
        # Include executable objects, constraints and indexes, not just visible
        # table columns. SQLite's own canonical stored DDL is compared exactly;
        # an equivalent rewrite still requires explicit contract review.
        if [dict(row) for row in tx.all(_SCHEMA_OBJECTS)] != self.objects:
            raise _refuse()
        for table in _COLUMNS:
            actual = [dict(row) for row in tx.execute(f'PRAGMA table_xinfo("{table}")').fetchall()]
            if actual != self.schema[table]:
                raise _refuse()

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


def _compare_prepared(native: Kernel, expected: _Projection, origin: Mapping[str, str] | None, anchor: str) -> None:
    """The same closed schema/row/material comparison for every fact source."""
    with native.storage.transaction(write=False) as tx:
        expected.verify_schema(tx)
        if tx.execute('PRAGMA foreign_key_check').fetchone() is not None:
            raise _refuse()
        for table, columns in _COLUMNS.items():
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
            if origin is None:
                raise _refuse()
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
        # All authoritative references and their complete bodies are exact.
        # Only these explicitly reviewed, reference-only/diagnostic purposes
        # can have additional rows; unknown or purpose-discovered inputs cannot.
        if any(identity not in expected.materials and row['purpose'] not in _UNREFERENCED_MATERIAL
               for identity, row in materials.items()):
            raise _refuse()
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


def _source_document(requirements: Requirements) -> bytes:
    return wire_json({'parent': asdict(requirements.parent), 'issues': [
        {**asdict(issue), 'labels': sorted(issue.labels)} for issue in requirements.issues]})


def _expected_imported(state: _Projection, facts: ImportedRunFacts, origin: Mapping[str, str] | None,
        limits: Mapping[str, Mapping[Purpose, tuple[int, int | None]]], *, tagged: bool = True) -> None:
    """Compile source expectations without constructing or reading a candidate."""
    spec = facts.spec
    if spec.inputs is None:
        raise MigrationRefused('source_creation_inputs')
    anchor = min(item.subject for item in facts.subjects)
    first = next(d.key for d in spec.deliveries if d.kind == DeliveryKind.TICKET)
    initial = _source_document(facts.requirements)
    latest = _source_document(facts.observed_requirements)
    sequence = 1 if initial == latest else 2
    state.add('meta', protocol=PROTOCOL)
    state.add('runs', id=spec.run_id, repository=spec.repository, parent_issue=spec.parent_issue,
        parent_revision=spec.parent_revision, graph_revision=spec.graph_revision,
        accepted_parent_revision=spec.parent_revision, concurrency=spec.policy.concurrency,
        policy=wire_json(Kernel._policy_values(spec.policy)), language=spec.language,
        repository_root=spec.repository_root, created_at=facts.created_at, source_sequence=sequence,
        observed_parent_revision=spec.parent_revision, observed_graph_revision=spec.graph_revision)
    profile = state.put(anchor, 'profile_revision', wire_json({**spec.inputs.profiles, 'source': {'operation': 'create'}}))
    state.add('profile_revisions', id=1, subject=anchor, material_id=profile, created_at=_CLOCK)
    inputs: dict[str, Any] = {name: dict(getattr(spec.inputs, name)) for name in (
        'prompt_resources', 'worker_identity', 'runner_identity', 'notifications', 'provenance')}
    inputs['repository_remote'] = spec.inputs.repository_remote
    state.add('run_inputs', run_id=spec.run_id, subject=anchor,
        material_id=state.put(anchor, 'creation_inputs', wire_json(inputs)))
    observations = [facts.requirements] if sequence == 1 else [facts.requirements, facts.observed_requirements]
    versions: dict[tuple[int, str], tuple[str, str]] = {}
    for index, source in enumerate(observations, 1):
        state.add('source_observations', sequence=index, parent_revision=spec.parent_revision,
            graph_revision=spec.graph_revision, body=_source_document(source))
        versions[(source.parent.number, spec.parent_revision)] = (source.parent.title, source.parent.body)
        for issue in source.issues:
            revision = Kernel._fingerprint({'title': issue.title, 'body': issue.body})
            versions[(issue.number, revision)] = (issue.title, issue.body)
    for (issue_number, revision), (title, body) in versions.items():
        state.add('requirement_versions', issue=issue_number, revision=revision, title=title, body=body)
    order = list(facts.observed_requirements.parent.sub_issue_numbers)
    if not facts.observed_requirements.parent.sub_issue_order_reliable:
        order.sort()
    identities = {item.issue: item.subject for item in facts.subjects}
    for issue in facts.observed_requirements.issues:
        revision = Kernel._fingerprint({'title': issue.title, 'body': issue.body})
        state.add('issue_facts', issue=issue.number, revision=revision, state=issue.state.upper(),
            labels=wire_json(sorted(issue.labels)), position=order.index(issue.number) if issue.number in order else -1)
        for edge in issue.blocked_by:
            state.add('dependency_facts', issue=issue.number, blocker=edge.number, state=edge.state.upper())
    for issue in facts.requirements.issues:
        for edge in issue.blocked_by:
            if issue.number in identities and edge.number in identities:
                state.add('dependencies', subject=identities[issue.number], predecessor=identities[edge.number])
    boot = wire_json({'stopped': False, 'blocked': None, 'gates': [], 'attempt_status': None}).decode()
    deliveries = {value.key: value for value in spec.deliveries if value.kind == DeliveryKind.TICKET}
    def initial_phase(key: str) -> str:
        return 'workspace' if deliveries[key].owned_root else 'development'
    if facts.source.scope == 'whole-run':
        state.history(first, 'source_refreshed', '1', None, _CLOCK,
            phase=initial_phase(first), purpose='requirements', status=boot)
    if origin is not None:
        state.history(anchor, 'v6_original_cutover', origin['nonce'],
            state.put(anchor, 'v6_original_cutover', wire_json(dict(origin))), _CLOCK,
            phase=initial_phase(anchor), purpose='requirements', status=boot)
    issues = {item.number: item for item in facts.requirements.issues}
    jobs = {}
    for subject in facts.subjects:
        delivery = deliveries[subject.subject]
        issue = issues[subject.issue]
        revision = Kernel._fingerprint({'title': issue.title, 'body': issue.body})
        job = state.add('subjects', id=subject.subject, run_id=spec.run_id, kind='ticket', issue=subject.issue,
            revision=revision, phase=initial_phase(subject.subject), purpose='requirements', source_ref=delivery.source_ref,
            target_ref=delivery.target_ref, base_sha=delivery.base.sha, base_tree=delivery.base.tree,
            source_sha=delivery.base.sha, workspace_sha=delivery.base.sha, completion_revision=delivery.completion_revision,
            checkout=delivery.checkout, owned_root=delivery.owned_root, common_git_dir=delivery.common_git_dir)
        job.update(imported_subject_values(facts, subject))
        jobs[subject.subject] = job
        used = {budget.purpose: budget for budget in subject.budgets}
        for purpose, (development_limit, review_limit) in limits[subject.subject].items():
            budget = used.get(purpose)
            required = purpose == Purpose.REQUIREMENTS or purpose == Purpose.CI and spec.policy.ticket_ci_review
            state.add('budgets', subject=subject.subject, purpose=purpose.value, window=1,
                developments_used=0 if budget is None else budget.developments_used,
                reviews_used=0 if budget is None else budget.reviews_used, development_limit=development_limit,
                review_limit=review_limit, review_required=int(required))
        state.add('resync_allowances', subject=subject.subject, generation=subject.generation, allowance=spec.policy.resync_limit)
        if subject.integration is not None:
            integration = import_object(subject.integration)
            state.add('integration_snapshots', subject=subject.subject, window=1, allowance_generation=1,
                source_ref=delivery.source_ref, target_ref=delivery.target_ref,
                source_sha=integration['source_sha'], target_sha=integration['target_sha'], open=0)
        seen = set()
        for ordinal, call in enumerate(subject.attempts, 1):
            thread = imported_thread(call)
            if thread is not None and thread not in seen:
                assert call.execution is not None
                binding = import_object(call.execution.binding)
                state.add('threads', id=thread, subject=subject.subject, role=binding['role'], profile_revision=1,
                    backend_id=call.execution.thread_id)
                state.put(subject.subject, 'v6_thread_binding', call.execution.binding)
                seen.add(thread)
            context = state.put(subject.subject, 'v6_attempt_input_witness', wire_json(
                imported_witness(facts, subject, call, revision, delivery.completion_revision)))
            result = state.put(subject.subject, 'v6_normalized_artifact', call.normalized_artifact)
            state.add('attempts', **imported_attempt_values(facts, subject, call, ordinal,
                revision, delivery.completion_revision, context, result))
            if call.execution is not None:
                invocation = import_object(call.execution.invocation)
                state.history(subject.subject, 'imported_invocation', ':'.join((spec.run_id, subject.subject, invocation['started_at'])),
                    state.put(subject.subject, 'v6_imported_invocation', wire_json(imported_execution(call))),
                    _created_at_epoch(invocation['ended_at']))
        if subject.completion is not None:
            for table, rows in imported_completion_rows(facts, subject, revision,
                    lambda purpose, body: state.put(subject.subject, purpose, body)).items():
                for row in rows:
                    state.add(table, **row)
        for resource in subject.resources:
            if resource.cleanup_pending:
                state.add('resource_cleanup', subject=subject.subject, generation=subject.generation,
                    source_ref=resource.source_ref, source_sha=resource.expected_oid, checkout=str(resource.checkout),
                    workspace_sha=resource.workspace_oid, owned_root=str(resource.owned_root), common_git_dir=str(resource.common_git_dir))
    for event in facts.history:
        state.history(event.subject, event.kind, event.identity, state.put(event.subject, 'v6_timeline_event', event.body), event.created_at)
    for proof_subject, proof_kind, proof_identity, proof_purpose, proof_body in imported_proof_documents(facts, origin, tagged=tagged):
        material = state.put(proof_subject, proof_purpose, proof_body)
        if proof_kind is not None:
            state.history(proof_subject, proof_kind, proof_identity, material, facts.created_at)
    final = next(d for d in spec.deliveries if d.kind == DeliveryKind.FINAL_RUN)
    state.add('final_target', id=final.key, source_ref=final.source_ref, target_ref=final.target_ref,
        checkout=final.checkout, owned_root=final.owned_root, common_git_dir=final.common_git_dir)
    archive = record_manifest(facts.source.snapshot, run_id=spec.run_id,
        profile_document=facts.source.profile_document, whole_source=facts.source.mode == 'original',
        put=lambda purpose, body: state.put(anchor, purpose, body))
    state.history(anchor, 'v6_record_import', spec.run_id, state.put(anchor, 'v6_record_manifest', wire_json(archive)), _CLOCK)
    if sequence == 2:
        owner = jobs[first]
        state.history(first, 'source_refreshed', '2', None, _CLOCK, phase=owner['phase'], purpose=owner['purpose'],
            status=wire_json({'stopped': False, 'blocked': None, 'gates': [],
                'attempt_status': 'returned' if owner['current_attempt'] is not None else None}).decode())


def verify_imported_installation(native: Kernel, facts: ImportedRunFacts, *, origin: Mapping[str, str] | None = None) -> None:
    expected = _Projection()
    tagged = True
    with native.storage.transaction(write=False) as tx:
        expected.verify_schema(tx)
        run = tx.one('SELECT executor,epoch FROM runs')
        if run['executor'] is not None or run['epoch'] != 0:
            raise _refuse()
        if origin is not None and facts.source.scope == 'whole-run':
            # A historical untagged WholeRun manifest is a wire spelling only.
            # Its values are never expectations: derive every byte/row from
            # the sealed source and retain the complete schema/material check.
            entries = tx.rows("SELECT subject,material_id FROM history WHERE kind='v6_run_import'")
            if len(entries) == 1:
                manifest = json.loads(tx.get(entries[0]['material_id'], entries[0]['subject']))
                tagged = not (isinstance(manifest, dict) and set(manifest) == {
                    'origin_nonce', 'source_sha256', 'profile_sha256', 'external_readproof_sha256',
                    'resource_mode', 'run_record', 'run_ref_ownership', 'subjects'})
    limits = {delivery.key: native._budget_limits(facts.spec.policy, delivery.kind)
              for delivery in facts.spec.deliveries if delivery.kind == DeliveryKind.TICKET}
    _expected_imported(expected, facts, origin, limits, tagged=tagged)
    _compare_prepared(native, expected, origin, min(item.subject for item in facts.subjects))
