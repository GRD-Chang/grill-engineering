"""Immutable import facts for one original Run and its exact Subject set.

These records describe source ownership. They do not authorize execution or
replace the source fence, Task admission, or Core's resource checks.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping

from .migration import MigrationRefused
from .migration_run_facts import BudgetFacts, SourceKey, TicketOwner
from .model import wire_json
from .storage import Transaction


@dataclass(frozen=True)
class ResourceProof:
    layout: Mapping[str, Any]
    checkout_index_sha256: str
    source_record: SourceKey

    def document(self) -> dict[str, Any]:
        return {'layout': dict(self.layout),
                'checkout_index_sha256': self.checkout_index_sha256,
                'source_record': self.source_record.document()}


@dataclass(frozen=True)
class SubjectProof:
    owner: TicketOwner
    kind: Literal['completed', 'returned', 'unallocated']
    historical_base: str | None
    budget: BudgetFacts | None
    current_attempt: str | None
    resources: tuple[ResourceProof, ...]

    def document(self, origin: Mapping[str, Any]) -> dict[str, Any]:
        return {
            'origin_nonce': origin['nonce'], 'source_sha256': origin['source_sha256'],
            'subject': self.owner.subject, 'generation': self.owner.generation,
            'kind': self.kind,
            'source_record': (self.owner.job or self.owner.graph_record).document(),
            'aliases': [key.document() for key in self.owner.aliases],
            'historical_base': self.historical_base,
            'budget_owner': self.budget.owner.document() if self.budget is not None else None,
            'current_attempt': self.current_attempt,
            'resources': [resource.document() for resource in self.resources],
        }


def install_run_proofs(tx: Transaction, *, origin: Mapping[str, Any], anchor: str,
                       profile_sha256: str, external_readproof_sha256: str,
                       run_ref: str, run_head: str, head_evidence: tuple[SourceKey, ...],
                       subjects: tuple[SubjectProof, ...], created_at: float) -> str:
    """Store the whole graph set once, with each material owned by its Subject."""
    run_id = origin['run_id']
    native = tx.rows("SELECT id,generation,issue FROM subjects WHERE kind='ticket' ORDER BY id")
    indexed = {proof.owner.subject: proof for proof in subjects}
    if (len(indexed) != len(subjects) or set(indexed) != {row['id'] for row in native}
            or anchor not in indexed or not head_evidence
            or tx.rows("SELECT 1 FROM history WHERE kind IN ('v6_run_import','v6_subject_import')")):
        raise MigrationRefused('source_import_set')
    refs: set[str] = {run_ref}
    checkouts: set[str] = set()
    for row in native:
        proof = indexed[row['id']]
        owner = proof.owner
        if owner.generation != row['generation'] or owner.ticket != row['issue'] or owner.generation != 1:
            raise MigrationRefused('source_import_identity')
        if proof.kind == 'unallocated':
            if (owner.job is not None or proof.historical_base is not None or proof.budget is not None
                    or proof.current_attempt is not None or proof.resources):
                raise MigrationRefused('source_unallocated_import')
        elif (owner.job is None or proof.historical_base is None or proof.budget is None
                or proof.budget.owner != owner.job
                or (proof.current_attempt is None) != (proof.kind == 'completed')):
            raise MigrationRefused('source_import_identity')
        for resource in proof.resources:
            layout = resource.layout
            if (resource.source_record != owner.job
                    or layout.get('nonce') != origin['nonce']
                    or layout.get('source_sha256') != origin['source_sha256']
                    or layout.get('source_uuid') != origin['source_uuid']
                    or not isinstance(layout.get('source_ref'), str)
                    or not isinstance(layout.get('checkout'), str)
                    or layout['source_ref'] in refs or layout['checkout'] in checkouts):
                raise MigrationRefused('source_resource_union')
            refs.add(layout['source_ref'])
            checkouts.add(layout['checkout'])
    entries = []
    for row in native:
        proof = indexed[row['id']]
        material = tx.put(row['id'], 'v6_subject_import', wire_json(proof.document(origin)))
        tx.execute('INSERT INTO history(subject,kind,identity,material_id,generation,created_at) '
                   'VALUES (?,?,?,?,?,?)', (row['id'], 'v6_subject_import', origin['nonce'],
                                          material, row['generation'], created_at))
        entries.append({'subject': row['id'], 'generation': row['generation'], 'proof_id': material})
    body = {
        'origin_nonce': origin['nonce'], 'source_sha256': origin['source_sha256'],
        'profile_sha256': profile_sha256, 'external_readproof_sha256': external_readproof_sha256,
        'resource_mode': 'existing-verified',
        'run_record': SourceKey('run_root', (run_id,)).document(),
        'run_ref_ownership': {'source_ref': run_ref, 'expected_oid': run_head,
            'source_record': SourceKey('run_root', (run_id,)).document(),
            'head_evidence': [key.document() for key in head_evidence]},
        'subjects': entries,
    }
    material = tx.put(anchor, 'v6_run_import', wire_json(body))
    tx.execute('INSERT INTO history(subject,kind,identity,material_id,generation,created_at) '
               'VALUES (?,?,?,?,1,?)', (anchor, 'v6_run_import', origin['nonce'], material, created_at))
    return material
