"""Immutable import facts for one original Run and its exact Subject set.

These records describe source ownership. They do not authorize execution or
replace the source fence, Task admission, or Core's resource checks.
"""
from __future__ import annotations

from dataclasses import dataclass
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
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


def completed_oracle(facts: ImportedRunFacts) -> bool:
    """The old sealed Completion input, distinct from a current external proof."""
    return (facts.source.external_readproof is None and facts.source.scope == 'single-ticket'
        and len(facts.subjects) == 1 and facts.subjects[0].kind == 'completed'
        and {'oracle:github', 'oracle:observation', 'oracle:report'} <= dict(facts.source.oracle_documents).keys())


def copied_resource_proof(facts: ImportedRunFacts) -> dict[str, Any] | None:
    """Private copied idle/returned ownership, distinct from original authority."""
    if facts.source.mode != 'copied':
        return None
    if facts.subjects and all(subject.kind == 'unallocated' for subject in facts.subjects):
        root = Path(facts.spec.repository_root or '')
        # Old offline conversions can share the source Git. They retain their
        # ordinary workspace contract; they grant no private-copy adoption.
        if facts.source.snapshot.source.is_relative_to(root) or facts.source.snapshot.path.is_relative_to(root):
            return None
        if facts.source.scope not in ('single-ticket', 'selected-graph') or not root.is_absolute():
            raise MigrationRefused('copied_idle_scope')
        entries: list[dict[str, Any]] = []
        for subject in sorted(facts.subjects, key=lambda item: item.subject):
            if subject.generation != 1 or len(subject.resources) != 1:
                raise MigrationRefused('copied_idle_scope')
            resource = subject.resources[0]
            if (resource.owner != subject.record or subject.record.table != 'run_graph_members'
                    or resource.disposition != 'new-unused' or resource.expected_oid is not None
                    or resource.checkout is None or resource.owned_root is None or resource.workspace_oid is None
                    or resource.common_git_dir != root / '.git'
                    or (resource.before_head, resource.checkout_status) not in
                        ((None, None), ((resource.workspace_oid + '\n').encode('ascii'), ()))):
                raise MigrationRefused('copied_idle_scope')
            entries.append(dict(subject=subject.subject, generation=1,
                source_record={'table': resource.owner.table, 'key': list(resource.owner.key)},
                disposition=resource.disposition, source_ref=resource.source_ref, expected_oid=None,
                checkout=str(resource.checkout), owned_root=str(resource.owned_root),
                workspace_oid=resource.workspace_oid,
                before_head=None if resource.before_head is None else resource.before_head.decode('ascii'),
                checkout_status=None if resource.checkout_status is None else list(resource.checkout_status)))
        body: dict[str, Any] = dict(format='copied-idle-resources/1', source_sha256=facts.source.snapshot.sha256,
            run_id=facts.spec.run_id, repository_root=str(root), common_git_dir=str(root / '.git'), resources=entries)
        return {**body, 'copy_uuid': hashlib.sha256(wire_json(body)).hexdigest()[:32]}
    if len(facts.subjects) != 1 or facts.subjects[0].kind != 'returned':
        return None
    subject = facts.subjects[0]
    if (facts.source.scope != 'single-ticket' or subject.generation != 1 or len(subject.resources) != 1
            or subject.historical_base is None or facts.spec.repository_root is None):
        raise MigrationRefused('copied_returned_scope')
    resource = subject.resources[0]
    if resource.checkout is None or resource.owned_root is None:
        raise MigrationRefused('copied_returned_scope')
    body = dict(format='copied-returned-resource/1', source_sha256=facts.source.snapshot.sha256,
        run_id=facts.spec.run_id, subject=subject.subject, generation=subject.generation,
        source_record={'table': subject.record.table, 'key': list(subject.record.key)},
        repository_root=facts.spec.repository_root, checkout=str(resource.checkout), owned_root=str(resource.owned_root),
        common_git_dir=str(resource.common_git_dir), source_ref=resource.source_ref,
        expected_oid=subject.historical_base.sha,
        initial_head=subject.historical_base.sha if subject.candidate is None else subject.candidate.sha)
    # This is a private-copy identity, never the original clone's UUID. The
    # transport must create the corresponding physical marker after its CAS.
    return {**body, 'copy_uuid': hashlib.sha256(wire_json(body)).hexdigest()[:32]}


def _completed_oracle_documents(facts: ImportedRunFacts, origin: Mapping[str, str] | None
        ) -> list[tuple[str, str | None, str, str, bytes]]:
    """Retain the existing audit wire directly from sealed SQL and oracle bytes."""
    from .migration_active import _wire_row
    from .migration_conversion import _one
    subject = facts.subjects[0]
    completion, candidate = subject.completion, subject.candidate
    if (completion is None or candidate is None or facts.source.profile_document is None
            or subject.record.table != 'work_jobs' or any(r.cleanup_pending for r in subject.resources)):
        raise MigrationRefused('source_import_completion')
    documents = dict(facts.source.oracle_documents)
    profile = json.loads(facts.source.profile_document)
    with closing(sqlite3.connect(facts.source.snapshot.path.as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
        db.row_factory = sqlite3.Row
        job = _wire_row(dict(_one(db, 'SELECT * FROM work_jobs WHERE run_id=? AND job_id=?', subject.record.key)))
        cleanup = _wire_row(dict(_one(db, 'SELECT * FROM run_cleanup_items WHERE run_id=? AND branch=?',
            (facts.spec.run_id, job['value_ticket_branch']))))
        physical = db.execute('SELECT count(*) FROM work_invocations WHERE run_id=?', (facts.spec.run_id,)).fetchone()[0]
    body = {
        'source_sha256': facts.source.snapshot.sha256,
        'profile_sha256': hashlib.sha256(facts.source.profile_document).hexdigest(),
        'github_sha256': hashlib.sha256(documents['oracle:github']).hexdigest(),
        'observation_sha256': hashlib.sha256(documents['oracle:observation']).hexdigest(),
        'original_report_sha256': hashlib.sha256(documents['oracle:report']).hexdigest(),
        'old_backend': json.loads(documents['oracle:observation']).get('backend'),
        'old_physical_invocations': physical, 'old_profile_bindings': len(profile['bindings']),
        'old_completed_job_revision': job['revision'], 'copied_repository_root': facts.spec.repository_root,
        'resource_mode': 'existing-verified' if facts.source.mode == 'original' else 'copy',
        'candidate_sha': candidate.sha, 'publication_sha': completion.publication.sha,
        'integrated_sha': completion.integrated.sha, 'old_job': job, 'old_cleanup': cleanup,
        'external_readproof_sha256': None,
    }
    result: list[tuple[str, str | None, str, str, bytes]] = [(subject.subject, 'v6_completed_import',
        facts.spec.run_id, 'v6_completed_import_identity', wire_json(body))]
    if origin is not None:
        from .legacy_admission import read_source_fence, source_fence
        receipt = read_source_fence(source_fence(dict(origin)))
        if (facts.source.mode != 'original' or facts.source.task is None
                or receipt != {**receipt, 'source_db': str(facts.source.snapshot.source),
                    'source_sha256': facts.source.snapshot.sha256, 'run_id': facts.spec.run_id,
                    'source_uuid': facts.source.checkout_uuid, 'task_fingerprint': facts.source.task.fingerprint,
                    'target_db': str(facts.source.snapshot.source.parent / facts.spec.run_id / 'kernel.sqlite3')}
                or facts.spec.repository_root != str(facts.source.task.workspace)):
            raise MigrationRefused('source_origin_provenance')
        result.append((subject.subject, 'v6_local_ref_ownership', facts.run_resource.source_ref,
            'v6_local_ref_ownership', wire_json({'origin_nonce': origin['nonce'],
                'source_ref': facts.run_resource.source_ref, 'expected_oid': completion.integrated.sha,
                'source_record': {'table': 'run_root', 'key': [facts.spec.run_id]}})))
    return result


def imported_proof_documents(facts: ImportedRunFacts, origin: Mapping[str, str] | None, *, tagged: bool = True,
        record_manifest: bytes
        ) -> list[tuple[str, str | None, str, str, bytes]]:
    """Pure source-to-wire records shared by installation and strict comparison.

    These are immutable evidence, never native dispatch authority. No candidate
    or writer is called to derive an expectation.
    """
    anchor = min(item.subject for item in facts.subjects)
    archive = (anchor, 'v6_record_import', facts.spec.run_id, 'v6_record_manifest', record_manifest)
    manifest_digest = hashlib.sha256(record_manifest).hexdigest()
    manifest_id = hashlib.sha256(f'{anchor}\0v6_record_manifest\0{manifest_digest}'.encode()).hexdigest()
    auxiliary: list[tuple[str, str | None, str, str, bytes]] = []
    for name, document in facts.source.oracle_documents:
        if name != 'source:work_job_candidate_commit_intent':
            continue  # SQL evidence bytes are retained by the complete record archive.
        intent = json.loads(document)
        owners = [item for item in facts.subjects if item.record.table == 'work_jobs'
            and item.record.key == (intent['run_id'], intent['job_id'])]
        if (len(owners) != 1 or intent.get('value_kind') != 'candidate_commit'
                or owners[0].kind != 'returned' or not owners[0].attempts
                or owners[0].attempts[-1].role.value != 'development'):
            raise MigrationRefused('source_candidate_intent')
        auxiliary.append((owners[0].subject, None, name, 'v6_candidate_commit_intent', document))
    if completed_oracle(facts):
        completed = _completed_oracle_documents(facts, origin)
        auxiliary.extend([completed[0], archive, *completed[1:]])
        if origin is not None:
            return auxiliary  # No fabricated v6_run_import/current external readproof.
    if origin is None:
        import_identity: dict[str, Any] = {
            'source_sha256': facts.source.snapshot.sha256, 'source_protocol': facts.source.snapshot.source_protocol,
            'source_mode': facts.source.mode, 'scope': facts.source.scope}
        if facts.source.mode == 'copied':
            import_identity['record_manifest'] = manifest_id
            resource = copied_resource_proof(facts)
            if resource is not None:
                import_identity['local_resources'] = resource
        elif not completed_oracle(facts):
            auxiliary.append(archive)
        return auxiliary + [(anchor, None, facts.source.snapshot.sha256, 'v6_import_identity', wire_json(import_identity))]
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
        (anchor, 'v6_external_readproof', external_sha, 'v6_external_readproof', facts.source.external_readproof), archive])
    return documents
