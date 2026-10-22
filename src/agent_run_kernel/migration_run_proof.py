"""Immutable import facts for one original Run and its exact Subject set.

These records describe source ownership. They do not authorize execution or
replace the source fence, Task admission, or Core's resource checks.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
from typing import Any, Mapping

from .migration import MigrationRefused
from .migration_run_facts import SourceKey
from .model import wire_json
from .migration_facts import ImportedRunFacts, ImportedResource, SourceRecord


@dataclass(frozen=True)
class ResourceProof:
    layout: Mapping[str, Any]
    checkout_index_sha256: str
    source_record: SourceKey

    def document(self) -> dict[str, Any]:
        return {'layout': dict(self.layout),
                'checkout_index_sha256': self.checkout_index_sha256,
                'source_record': self.source_record.document()}


def run_import_document(*, origin: Mapping[str, Any], profile_sha256: str,
        external_readproof_sha256: str, run_ref: str, run_head: str,
        head_evidence: tuple[SourceKey | SourceRecord, ...], entries: list[dict[str, Any]],
        source_scope: str | None = None) -> dict[str, Any]:
    run_id = origin['run_id']
    document = {
        'origin_nonce': origin['nonce'], 'source_sha256': origin['source_sha256'],
        'profile_sha256': profile_sha256, 'external_readproof_sha256': external_readproof_sha256,
        'resource_mode': 'existing-verified',
        'run_record': SourceKey('run_root', (run_id,)).document(),
        'run_ref_ownership': {'source_ref': run_ref, 'expected_oid': run_head,
            'source_record': SourceKey('run_root', (run_id,)).document(),
            'head_evidence': [{'table': key.table, 'key': list(key.key)} for key in head_evidence]},
        'subjects': entries,
    }
    if source_scope is not None:
        if source_scope not in {'single-ticket', 'whole-run'}:
            raise MigrationRefused('source_import_scope')
        document['source_scope'] = source_scope
    return document


def imported_resource_layout(resource: ImportedResource, origin: Mapping[str, str]) -> dict[str, Any]:
    if (resource.kind != 'ticket-workspace' or resource.disposition != 'existing'
            or resource.checkout is None or resource.owned_root is None or resource.expected_oid is None
            or resource.before_head is None
            or (resource.checkout_archive_sha256 is None) != (resource.checkout_index_sha256 is None)):
        raise MigrationRefused('source_import_resource')
    return {'version': 1, 'nonce': origin['nonce'], 'source_sha256': origin['source_sha256'],
        'source_uuid': origin['source_uuid'], 'repository_root': str(resource.common_git_dir.parent),
        'checkout': str(resource.checkout), 'owned_root': str(resource.owned_root),
        'common_git_dir': str(resource.common_git_dir), 'source_ref': resource.source_ref,
        'expected_oid': resource.expected_oid, 'before_head': resource.before_head.decode('utf-8'),
        'checkout_archive_sha256': resource.checkout_archive_sha256}


def imported_proof_documents(facts: ImportedRunFacts, origin: Mapping[str, str] | None, *, tagged: bool = True
        ) -> list[tuple[str, str | None, str, str, bytes]]:
    """Pure source-to-wire records shared by installation and strict comparison.

    These are immutable evidence, never native dispatch authority. No candidate
    or writer is called to derive an expectation.
    """
    anchor = min(item.subject for item in facts.subjects)
    auxiliary: list[tuple[str, str | None, str, str, bytes]] = []
    for name, document in facts.source.oracle_documents:
        if name != 'source:work_job_candidate_commit_intent':
            continue  # SQL evidence bytes are retained by the complete record archive.
        import json
        intent = json.loads(document)
        owners = [item for item in facts.subjects if item.record.table == 'work_jobs'
            and item.record.key == (intent['run_id'], intent['job_id'])]
        if (len(owners) != 1 or intent.get('value_kind') != 'candidate_commit'
                or owners[0].kind != 'returned' or not owners[0].attempts
                or owners[0].attempts[-1].role.value != 'development'):
            raise MigrationRefused('source_candidate_intent')
        auxiliary.append((owners[0].subject, None, name, 'v6_candidate_commit_intent', document))
    if origin is None:
        return auxiliary + [(anchor, None, facts.source.snapshot.sha256, 'v6_import_identity', wire_json({
            'source_sha256': facts.source.snapshot.sha256, 'source_protocol': facts.source.snapshot.source_protocol,
            'source_mode': facts.source.mode, 'scope': facts.source.scope}))]
    if facts.source.profile_document is None or facts.source.external_readproof is None:
        raise MigrationRefused('source_external_readback_required')
    entries = []
    documents: list[tuple[str, str | None, str, str, bytes]] = auxiliary
    def record(value: SourceRecord) -> dict[str, Any]:
        return {'table': value.table, 'key': list(value.key)}
    for subject in sorted(facts.subjects, key=lambda item: item.subject):
        budget = next((value for value in subject.budgets if value.current and value.purpose.value == 'requirements'), None)
        resources = [{'layout': imported_resource_layout(resource, origin),
            'checkout_index_sha256': resource.checkout_index_sha256, 'source_record': record(resource.owner)}
            for resource in subject.resources if resource.disposition == 'existing']
        body = wire_json({'origin_nonce': origin['nonce'], 'source_sha256': facts.source.snapshot.sha256,
            'subject': subject.subject, 'generation': subject.generation, 'kind': subject.kind,
            'source_record': record(subject.record), 'aliases': [record(value) for value in subject.aliases],
            'historical_base': None if subject.historical_base is None else subject.historical_base.sha,
            'budget_owner': None if budget is None else record(budget.owner),
            'current_attempt': next((call.record.key[-1] for call in subject.attempts if call.pending), None),
            'resources': resources})
        digest = hashlib.sha256(body).hexdigest()
        identity = hashlib.sha256(f'{subject.subject}\0v6_subject_import\0{digest}'.encode()).hexdigest()
        entries.append(dict(subject=subject.subject, generation=subject.generation, proof_id=identity))
        documents.append((subject.subject, 'v6_subject_import', origin['nonce'], 'v6_subject_import', body))
    if facts.run_resource.expected_oid is None:
        raise MigrationRefused('source_run_head_unproven')
    external_sha = hashlib.sha256(facts.source.external_readproof).hexdigest()
    run = run_import_document(origin=origin, profile_sha256=hashlib.sha256(facts.source.profile_document).hexdigest(),
        external_readproof_sha256=external_sha, run_ref=facts.run_resource.source_ref,
        run_head=facts.run_resource.expected_oid, head_evidence=facts.run_resource.head_evidence, entries=entries,
        source_scope=facts.source.scope if tagged else None)
    documents.extend([(anchor, 'v6_run_import', origin['nonce'], 'v6_run_import', wire_json(run)),
        (anchor, 'v6_external_readproof', external_sha, 'v6_external_readproof', facts.source.external_readproof)])
    return documents
