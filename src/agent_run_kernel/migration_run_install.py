"""Install proved whole-Run facts into one unpublished Core-created database."""
from __future__ import annotations

from pathlib import Path
import json
import os
import tempfile
from typing import Any, Callable, Mapping

from .kernel import Kernel
from .migration import MigrationRefused, _created_at_epoch
from .migration_records import _preserve_copy
from .model import DeliveryKind, Role, wire_json
from .migration_facts import ImportedAttempt, ImportedRunFacts, ImportedSubject, SourceRecord


def source_record(value: SourceRecord) -> dict[str, Any]:
    return {'table': value.table, 'key': list(value.key)}


def import_object(body: bytes) -> dict[str, Any]:
    value = json.loads(body)
    if not isinstance(value, dict):
        raise MigrationRefused('source_import_document')
    return value


def imported_thread(call: ImportedAttempt) -> str | None:
    return None if call.execution is None else 'v6:' + call.execution.binding_id


def imported_witness(facts: ImportedRunFacts, subject: ImportedSubject, call: ImportedAttempt,
                     revision: str, completion_revision: str) -> dict[str, Any]:
    boundary = None if call.allocation_boundary is None else import_object(call.allocation_boundary)
    value = {'old_attempt': call.record.key[-1], 'old_role_ordinal': call.role_ordinal,
        'original_allocation_boundary': boundary,
        'bound_version': None if call.bound_version is None else import_object(call.bound_version),
        'selected_version': import_object(call.selected_version), 'global_current_version': import_object(call.current_version),
        'native_authority': {'kind': 'continuation-boundary', 'parent_revision': facts.spec.parent_revision,
            'graph_revision': facts.spec.graph_revision, 'revision': revision, 'completion_revision': completion_revision,
            'base_sha': None if subject.historical_base is None else subject.historical_base.sha,
            'candidate_sha': None if boundary is None else boundary.get('candidate_sha'),
            'candidate_tree': None if boundary is None else boundary.get('candidate_tree')}}
    if call.execution is not None:
        value['input_fingerprint'] = import_object(call.execution.invocation)['input_fingerprint']
    if call.role == Role.REVIEW:
        value.update(checkout=None, base_sha=None if subject.historical_base is None else subject.historical_base.sha,
            reviewed_commit=None if boundary is None else boundary.get('candidate_sha'),
            reviewed_tree=None if boundary is None else boundary.get('candidate_tree'), revision=revision,
            parent_revision=facts.spec.parent_revision, graph_revision=facts.spec.graph_revision,
            completion_revision=completion_revision)
    return value


def imported_attempt_values(facts: ImportedRunFacts, subject: ImportedSubject, call: ImportedAttempt, ordinal: int,
        revision: str, completion_revision: str, context_id: str, result_id: str) -> dict[str, Any]:
    boundary = {} if call.allocation_boundary is None else import_object(call.allocation_boundary)
    result = import_object(call.normalized_artifact)
    outcome = None if call.pending else ('development' if call.role == Role.DEVELOPMENT else
        'publication' if call.role == Role.PUBLICATION else
        'pass' if all(check['status'] == 'pass' for check in result['checks'].values()) else 'fail')
    window = call.window
    if window is None:
        window = next(b.window for b in subject.budgets if b.current and b.purpose == call.purpose)
    return dict(id=call.record.key[-1], subject=subject.subject, generation=call.generation, ordinal=ordinal,
        role=call.role.value, purpose=call.purpose.value, window=window, parent_revision=facts.spec.parent_revision,
        graph_revision=facts.spec.graph_revision, revision=revision,
        base_sha=None if subject.historical_base is None else subject.historical_base.sha,
        candidate_sha=boundary.get('candidate_sha'), candidate_tree=boundary.get('candidate_tree'),
        expected_tree=boundary.get('candidate_tree'), completion_revision=completion_revision,
        context_id=context_id, current_invocation=None, thread_id=None, opening_id=None, result_id=result_id,
        status='returned' if call.pending else 'consumed', accounted=int(call.accounted), outcome=outcome)


def imported_subject_values(facts: ImportedRunFacts, subject: ImportedSubject) -> dict[str, Any]:
    if subject.kind == 'unallocated':
        return {}
    if subject.historical_base is None or not subject.attempts:
        raise MigrationRefused('source_import_subject')
    base, candidate, completion = subject.historical_base, subject.candidate, subject.completion
    role = subject.attempts[-1].role
    development = next(call for call in reversed(subject.attempts) if call.role == Role.DEVELOPMENT)
    integrated = completion.integrated if completion is not None else candidate if role == Role.PUBLICATION else None
    publication = None if completion is None else completion.publication
    integration = None if subject.integration is None else import_object(subject.integration)
    return dict(phase='complete' if completion is not None else
        {Role.DEVELOPMENT: 'development', Role.REVIEW: 'review', Role.PUBLICATION: 'publish'}[role],
        base_sha=base.sha, base_tree=base.tree, source_sha=base.sha if publication is None else publication.sha,
        current_attempt=None if completion is not None else subject.attempts[-1].record.key[-1],
        review_attempt=subject.accepted_review, development_thread=imported_thread(development),
        candidate_sha=None if candidate is None else candidate.sha, candidate_tree=None if candidate is None else candidate.tree,
        workspace_sha=publication.sha if publication is not None else candidate.sha if candidate is not None else base.sha,
        expected_tree=None if candidate is None else candidate.tree,
        integrated_sha=None if integrated is None else integrated.sha, integrated_tree=None if integrated is None else integrated.tree,
        integration_target=None if integration is None else integration['target_sha'],
        gate_mode='review' if completion is not None or role == Role.PUBLICATION else None,
        publication_sha=None if publication is None else publication.sha, publication_tree=None if publication is None else publication.tree,
        pr_number=None if completion is None else completion.pr_number, merge_sha=None if completion is None else completion.integrated.sha)


def imported_execution(call: ImportedAttempt) -> dict[str, Any]:
    if call.execution is None:
        raise MigrationRefused('source_import_execution')
    return {'attempt_id': call.record.key[-1], 'old_role_ordinal': call.role_ordinal,
        'thread_id': call.execution.thread_id, 'raw_evidence_id': call.raw_record.key[-1],
        'returned': import_object(call.raw_result), 'invocation': import_object(call.execution.invocation),
        'bound_version': None if call.bound_version is None else import_object(call.bound_version),
        'selected_version': import_object(call.selected_version), 'global_current_version': import_object(call.current_version)}


def imported_completion_rows(facts: ImportedRunFacts, subject: ImportedSubject, revision: str,
        put: Callable[[str, bytes], str]) -> dict[str, list[dict[str, Any]]]:
    item = subject.completion
    if item is None or subject.historical_base is None or subject.candidate is None:
        raise MigrationRefused('source_import_completion')
    delivery = next(d for d in facts.spec.deliveries if d.key == subject.subject)
    intent = import_object(item.close_intent)
    proof = None if item.close_readproof is None else import_object(item.close_readproof)
    effects = []
    for operation in ('completion_comment', 'close'):
        effect_id = f'v6-import:{facts.spec.run_id}:{subject.issue}:{operation}'
        if proof is not None:
            close_intent = {**proof['close_intent'], 'run_id': facts.spec.run_id, 'subject': subject.subject,
                'issue': subject.issue, 'merge_sha': item.integrated.sha,
                'effect_id': f'v6-import:{facts.spec.run_id}:{subject.issue}:completion_comment',
                'pr_number': item.pr_number, 'kind': 'ticket', 'comment_id': proof['comment']['id'],
                'comment_created_at': proof['comment']['created_at'], 'comment_body': proof['comment']['body']}
            payload = {'issue': subject.issue, 'merge_sha': item.integrated.sha, 'kind': 'ticket', 'close_intent': close_intent}
            receipt = {'disposition': 'target', 'commit': None, 'pr': None,
                'raw': {**proof, 'close_intent': close_intent, 'source': 'v6-intent-and-external-readproof'}}
        else:
            payload = {'action': 'completion_comment' if operation == 'completion_comment' else 'close_issue',
                'run_id': facts.spec.run_id, 'ticket_number': subject.issue, 'pr_number': item.pr_number,
                'integrated_sha': item.integrated.sha}
            receipt = {'mutation': payload, 'close_intent': intent,
                'close_ownership': None if operation != 'close' or item.close_ownership is None else import_object(item.close_ownership),
                'source': 'old-github-fixture-and-v6-close-ledger'}
        effects.append(dict(id=effect_id, subject=subject.subject, operation=operation, status='confirmed',
            executor=None, epoch=None, parent_revision=facts.spec.parent_revision, graph_revision=facts.spec.graph_revision,
            revision=revision, generation=subject.generation, source_ref=delivery.source_ref, target_ref=delivery.target_ref,
            before_sha=item.integrated.sha, after_sha=item.integrated.sha, candidate_sha=subject.candidate.sha,
            candidate_tree=subject.candidate.tree, base_sha=subject.historical_base.sha, base_tree=subject.historical_base.tree,
            pr_number=item.pr_number, actor=intent['value_actor'], payload_id=put('v6_confirmed_effect_payload', wire_json(payload)),
            receipt_id=put('v6_confirmed_effect_receipt', wire_json(receipt))))
    return {'effects': effects, 'completions': [dict(subject=subject.subject, generation=subject.generation,
        revision=revision, parent_revision=facts.spec.parent_revision, graph_revision=facts.spec.graph_revision,
        source_sha=item.publication.sha, merge_sha=item.integrated.sha, tree=item.integrated.tree,
        pr_number=item.pr_number, gate_mode='review', review_attempt=item.review_attempt,
        development_attempt=item.development_attempt, comment_effect=effects[0]['id'], close_effect=effects[1]['id'])]}


def install_imported_facts(facts: ImportedRunFacts, destination: Path, *, origin: Mapping[str, str] | None = None) -> Kernel:
    """Publish only a completely installed, strictly verified private candidate."""
    if destination.exists() or destination.is_symlink():
        raise MigrationRefused('destination_exists')
    with tempfile.TemporaryDirectory(dir=destination.parent, prefix='.canonical-install-') as directory:
        candidate = Path(directory) / 'kernel.sqlite3'
        _install_imported_candidate(facts, candidate, origin=origin)
        descriptor = os.open(candidate, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        try:
            os.link(candidate, destination)
        except FileExistsError as error:
            raise MigrationRefused('destination_exists') from error
        descriptor = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    return Kernel(destination)


def _install_imported_candidate(facts: ImportedRunFacts, destination: Path, *, origin: Mapping[str, str] | None) -> Kernel:
    """Install one canonical fact set into an unpublished Core-created file.

    Source/Git/external authentication and all cutover locks belong to the
    offline caller. This never publishes a Task, fences a source or allocates
    execution. No old-family installation is retried on failure.
    """
    from .migration import validate_snapshot, _require_no_v6_notification_journal
    from .model import artifact
    from .migration_run_projection import verify_imported_installation
    from .migration_run_proof import imported_proof_documents

    validate_snapshot(facts.source.snapshot)
    _require_no_v6_notification_journal(facts.source.snapshot, facts.spec.run_id)
    if (destination.exists() or destination.is_symlink() or facts.spec.inputs is None
            or facts.source.mode == 'copied' and origin is not None
            or {s.subject for s in facts.subjects} != {d.key for d in facts.spec.deliveries if d.kind == DeliveryKind.TICKET}
            or len({s.subject for s in facts.subjects}) != len(facts.subjects)):
        raise MigrationRefused('source_import_identity')
    for subject in facts.subjects:
        if subject.generation != 1 or any(b.window != 1 or not b.current for b in subject.budgets):
            raise MigrationRefused('unsupported_source_generation_or_window')
        for call in subject.attempts:
            if call.generation != subject.generation or call.window not in (None, 1) or call.purpose.value != 'requirements':
                raise MigrationRefused('unsupported_source_generation_or_window')
            if call.continuation_basis is not None:
                raise MigrationRefused('unsupported_source_continuation_basis')
            result = artifact(call.role, call.normalized_artifact)
            if result.get('result_kind') == 'human_blocker' or call.role == Role.REVIEW and any(
                    check['status'] == 'blocked' for check in result['checks'].values()):
                raise MigrationRefused('unsupported_source_human_return')
    native = Kernel.create(destination, facts.spec, requirements=facts.requirements, source_origin=origin)
    with native.storage.transaction() as tx:
        tx.execute('UPDATE runs SET created_at=?', (facts.created_at,))
        if facts.source.scope != 'whole-run':
            tx.execute("DELETE FROM history WHERE kind!='v6_original_cutover'")
        for subject in facts.subjects:
            values = imported_subject_values(facts, subject)
            if values:
                tx.execute('UPDATE subjects SET ' + ','.join(k + '=?' for k in values) + ' WHERE id=?',
                    (*values.values(), subject.subject))
            for budget in subject.budgets:
                tx.execute('UPDATE budgets SET developments_used=?,reviews_used=? WHERE subject=? AND purpose=? AND window=?',
                    (budget.developments_used, budget.reviews_used, subject.subject, budget.purpose.value, budget.window))
            if subject.integration is not None:
                integration = import_object(subject.integration)
                delivery = next(d for d in facts.spec.deliveries if d.key == subject.subject)
                tx.execute('INSERT INTO integration_snapshots VALUES (?,?,?,?,?,?,?,0)',
                    (subject.subject, 1, 1, delivery.source_ref, delivery.target_ref, integration['source_sha'], integration['target_sha']))
            job = tx.one('SELECT revision,completion_revision FROM subjects WHERE id=?', (subject.subject,))
            threads = set()
            for ordinal, call in enumerate(subject.attempts, 1):
                thread = imported_thread(call)
                if thread is not None and thread not in threads:
                    assert call.execution is not None
                    binding = import_object(call.execution.binding)
                    tx.execute('INSERT INTO threads(id,subject,role,profile_revision,backend_id) VALUES (?,?,?,1,?)',
                        (thread, subject.subject, binding['role'], call.execution.thread_id))
                    tx.put(subject.subject, 'v6_thread_binding', call.execution.binding)
                    threads.add(thread)
                context = tx.put(subject.subject, 'v6_attempt_input_witness', wire_json(
                    imported_witness(facts, subject, call, job['revision'], job['completion_revision'])))
                result_id = tx.put(subject.subject, 'v6_normalized_artifact', call.normalized_artifact)
                row = imported_attempt_values(facts, subject, call, ordinal, job['revision'], job['completion_revision'], context, result_id)
                tx.execute('INSERT INTO attempts(' + ','.join(row) + ') VALUES (' + ','.join('?' for _ in row) + ')', tuple(row.values()))
                if call.execution is not None:
                    invocation = import_object(call.execution.invocation)
                    material = tx.put(subject.subject, 'v6_imported_invocation', wire_json(imported_execution(call)))
                    tx.execute('INSERT INTO history(subject,kind,identity,material_id,generation,created_at) VALUES (?,?,?,?,?,?)',
                        (subject.subject, 'imported_invocation', ':'.join((facts.spec.run_id, subject.subject, invocation['started_at'])),
                         material, subject.generation, _created_at_epoch(invocation['ended_at'])))
            if subject.completion is not None:
                for table, rows in imported_completion_rows(facts, subject, job['revision'],
                        lambda purpose, body: tx.put(subject.subject, purpose, body)).items():
                    for row in rows:
                        tx.execute('INSERT INTO ' + table + '(' + ','.join(row) + ') VALUES (' + ','.join('?' for _ in row) + ')', tuple(row.values()))
            for resource in subject.resources:
                if resource.cleanup_pending:
                    if (resource.checkout is None or resource.owned_root is None or resource.expected_oid is None
                            or resource.workspace_oid is None or subject.completion is None):
                        raise MigrationRefused('source_import_cleanup')
                    tx.execute('INSERT INTO resource_cleanup(subject,generation,source_ref,source_sha,checkout,workspace_sha,owned_root,common_git_dir) VALUES (?,?,?,?,?,?,?,?)',
                        (subject.subject, subject.generation, resource.source_ref, resource.expected_oid, str(resource.checkout),
                         resource.workspace_oid, str(resource.owned_root), str(resource.common_git_dir)))
        for event in facts.history:
            material = tx.put(event.subject, 'v6_timeline_event', event.body)
            tx.execute('INSERT INTO history(subject,kind,identity,material_id,generation,created_at) VALUES (?,?,?,?,?,?)',
                (event.subject, event.kind, event.identity, material, event.generation, event.created_at))
        anchor = min(s.subject for s in facts.subjects)
        for proof_subject, kind, identity, purpose, body in imported_proof_documents(facts, origin):
            material = tx.put(proof_subject, purpose, body)
            if kind is not None:
                tx.execute('INSERT INTO history(subject,kind,identity,material_id,generation,created_at) VALUES (?,?,?,?,1,?)',
                    (proof_subject, kind, identity, material, facts.created_at))
    _preserve_copy(facts.source.snapshot, native, run_id=facts.spec.run_id, subject=anchor,
        profile_document=facts.source.profile_document, whole_source=facts.source.mode == 'original')
    native.refresh(lambda: facts.observed_requirements)
    verify_imported_installation(native, facts, origin=origin)
    return native
