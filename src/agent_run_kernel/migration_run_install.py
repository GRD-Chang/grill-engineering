"""Install proved whole-Run facts into one unpublished Core-created database."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from .kernel import Kernel
from .migration import MigrationRefused, SourceSnapshot, _created_at_epoch
from .migration_active import _wire_row
from .migration_conversion import _git
from .migration_records import _preserve_copy
from .migration_run import OriginalRunFacts, UnallocatedTicket, WorkedTicket
from .migration_run_attempts import AttemptFacts
from .migration_run_proof import ResourceProof, SubjectProof, install_run_proofs
from .migration_run_facts import SourceKey
from .model import Commit, Delivery, DeliveryKind, Role, RunSpec, wire_json
from .storage import Transaction


def _thread_id(call: AttemptFacts) -> str:
    # Backend IDs belong to the old worker, whereas the binding PK is unique
    # across this Run. Keep both facts instead of using backend text as a PK.
    return 'v6:' + str(call.invocation['binding_id'])


def completion_documents(facts: OriginalRunFacts, ticket: WorkedTicket,
                         close_readproof: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    header, subject, job = facts.header, ticket.owner.subject, ticket.job
    close_intent = {**close_readproof['close_intent'], 'run_id': header.run_id, 'subject': subject,
        'issue': ticket.owner.ticket, 'merge_sha': job['value_integrated_sha'],
        'effect_id': f'v6-import:{header.run_id}:{ticket.owner.ticket}:completion_comment',
        'pr_number': job['value_pr_number'], 'kind': 'ticket',
        'comment_id': close_readproof['comment']['id'], 'comment_created_at': close_readproof['comment']['created_at'],
        'comment_body': close_readproof['comment']['body']}
    payload = {'issue': ticket.owner.ticket, 'merge_sha': job['value_integrated_sha'], 'kind': 'ticket', 'close_intent': close_intent}
    body = {'disposition': 'target', 'commit': None, 'pr': None,
            'raw': {**close_readproof, 'close_intent': close_intent, 'source': 'v6-intent-and-external-readproof'}}
    return payload, body


def attempt_witness(facts: OriginalRunFacts, ticket: WorkedTicket, call: AttemptFacts,
                    revision: str, completion_revision: str) -> dict[str, Any]:
    """The same immutable input witness is installed and checked before cutover."""
    header, boundary = facts.header, call.allocation_boundary
    witness: dict[str, Any] = {
        'old_attempt': call.attempt_id, 'old_role_ordinal': call.old_ordinal,
        'input_fingerprint': call.invocation['input_fingerprint'],
        'original_allocation_boundary': dict(boundary),
        'bound_version': _wire_row(call.bound_version),
        'selected_version': _wire_row(call.selected_version),
        'global_current_version': _wire_row(call.global_current_version),
        'native_authority': {'kind': 'continuation-boundary', 'parent_revision': header.parent_revision,
            'graph_revision': header.graph_revision, 'revision': revision,
            'completion_revision': completion_revision, 'base_sha': ticket.base_sha,
            'candidate_sha': boundary.get('candidate_sha'), 'candidate_tree': boundary.get('candidate_tree')},
    }
    if call.role == Role.REVIEW:
        witness.update(checkout=None, base_sha=ticket.base_sha,
            reviewed_commit=boundary.get('candidate_sha'), reviewed_tree=boundary.get('candidate_tree'),
            revision=revision, parent_revision=header.parent_revision, graph_revision=header.graph_revision,
            completion_revision=completion_revision)
    return witness


def _install_attempts(tx: Transaction, facts: OriginalRunFacts, ticket: WorkedTicket) -> None:
    header, subject = facts.header, ticket.owner.subject
    native = tx.one('SELECT revision,completion_revision FROM subjects WHERE id=?', (subject,))
    seen: set[str] = set()
    for index, call in enumerate(ticket.calls, start=1):
        thread_id = _thread_id(call)
        if thread_id not in seen:
            tx.execute('INSERT INTO threads(id,subject,role,profile_revision,backend_id) VALUES (?,?,?,1,?)',
                       (thread_id, subject, call.binding['role'], call.thread_id))
            tx.put(subject, 'v6_thread_binding', wire_json(dict(call.binding)))
            seen.add(thread_id)
        boundary = call.allocation_boundary
        context = tx.put(subject, 'v6_attempt_input_witness', wire_json(
            attempt_witness(facts, ticket, call, native['revision'], native['completion_revision'])))
        result = tx.put(subject, 'v6_normalized_artifact', wire_json(dict(call.normalized)))
        outcome = None if call.pending else (
            'development' if call.role == Role.DEVELOPMENT else 'publication' if call.role == Role.PUBLICATION
            else 'pass' if all(check['status'] == 'pass' for check in call.normalized['checks'].values()) else 'fail')
        tx.execute('''INSERT INTO attempts(
            id,subject,generation,ordinal,role,purpose,window,parent_revision,graph_revision,
            revision,base_sha,candidate_sha,candidate_tree,expected_tree,completion_revision,
            context_id,current_invocation,result_id,status,accounted,outcome)
            VALUES (?,?,1,?,?,'requirements',1,?,?,?,?,?,?,?,?,?,NULL,?,?,?,?)''',
            (call.attempt_id, subject, index, call.role.value, header.parent_revision, header.graph_revision,
             native['revision'], ticket.base_sha, boundary.get('candidate_sha'), boundary.get('candidate_tree'),
             boundary.get('candidate_tree'), native['completion_revision'], context, result,
             'returned' if call.pending else 'consumed',
             int(call.role == Role.REVIEW and call.selected_version['budget_consumed'] == 1), outcome))
        original = {'attempt_id': call.attempt_id, 'old_role_ordinal': call.old_ordinal,
            'thread_id': call.thread_id, 'raw_evidence_id': call.raw_evidence_id, 'returned': dict(call.raw),
            'invocation': _wire_row(call.invocation), 'bound_version': _wire_row(call.bound_version),
            'selected_version': _wire_row(call.selected_version),
            'global_current_version': _wire_row(call.global_current_version)}
        material = tx.put(subject, 'v6_imported_invocation', wire_json(original))
        tx.execute('INSERT INTO history(subject,kind,identity,material_id,generation,created_at) VALUES (?,?,?,?,1,?)',
            (subject, 'imported_invocation', ':'.join((header.run_id, subject, call.invocation['started_at'])),
             material, _created_at_epoch(call.invocation['ended_at'])))


def _install_worked_ticket(tx: Transaction, facts: OriginalRunFacts, ticket: WorkedTicket,
                           repository: Path, close_readproof: Mapping[str, Any] | None) -> None:
    header, subject, job = facts.header, ticket.owner.subject, ticket.job
    completed = ticket.kind == 'completed'
    role = ticket.calls[-1].role
    phase = 'complete' if completed else {Role.DEVELOPMENT: 'development', Role.REVIEW: 'review', Role.PUBLICATION: 'publish'}[role]
    development = [call for call in ticket.calls if call.role == Role.DEVELOPMENT][-1]
    base_tree = _git(repository, 'rev-parse', ticket.base_sha + '^{tree}')
    publication = job['value_publication_sha'] if completed else None
    integrated = job['value_integrated_sha'] if completed else ticket.candidate_sha if role == Role.PUBLICATION else None
    tx.execute('''UPDATE subjects SET phase=?,base_sha=?,base_tree=?,source_sha=?,current_attempt=?,review_attempt=?,
        development_thread=?,candidate_sha=?,candidate_tree=?,workspace_sha=?,expected_tree=?,integrated_sha=?,
        integrated_tree=?,integration_target=?,gate_mode=?,publication_sha=?,publication_tree=?,pr_number=?,merge_sha=?
        WHERE id=?''', (phase, ticket.base_sha, base_tree, publication or ticket.base_sha,
        None if completed else ticket.calls[-1].attempt_id, ticket.accepted_review,
        _thread_id(development), ticket.candidate_sha, ticket.candidate_tree,
        publication or ticket.candidate_sha or ticket.base_sha, ticket.candidate_tree, integrated,
        ticket.candidate_tree if integrated else None, ticket.integration_target,
        'review' if completed or role == Role.PUBLICATION else None, publication,
        ticket.candidate_tree if publication else None, job['value_pr_number'] if completed else None,
        job['value_integrated_sha'] if completed else None, subject))
    tx.execute("UPDATE budgets SET developments_used=?,reviews_used=? WHERE subject=? AND purpose='requirements' AND window=1",
               (ticket.budget.developments_used, ticket.budget.reviews_used, subject))
    if ticket.integration is not None:
        tx.execute('INSERT INTO integration_snapshots VALUES (?,?,?,?,?,?,?,0)',
            (subject, 1, 1, ticket.source_ref, 'refs/heads/' + header.run_branch,
             ticket.base_sha, ticket.integration_target))
    _install_attempts(tx, facts, ticket)
    if not completed:
        return
    if close_readproof is None or ticket.close_intent is None or ticket.close_ownership is None:
        raise MigrationRefused('source_external_close')
    if close_readproof['event']['id'] != ticket.close_ownership['value_event_id']:
        raise MigrationRefused('source_close_ownership')
    native = tx.one('SELECT revision FROM subjects WHERE id=?', (subject,))
    receipt_ids = []
    for operation in ('completion_comment', 'close'):
        effect_id = f'v6-import:{header.run_id}:{ticket.owner.ticket}:{operation}'
        payload, body = completion_documents(facts, ticket, close_readproof)
        receipt_id = tx.put(subject, 'v6_confirmed_effect_receipt', wire_json(body))
        payload_id = tx.put(subject, 'v6_confirmed_effect_payload', wire_json(payload))
        tx.execute('''INSERT INTO effects(id,subject,operation,status,executor,epoch,parent_revision,graph_revision,
            revision,generation,source_ref,target_ref,before_sha,after_sha,candidate_sha,candidate_tree,base_sha,
            base_tree,pr_number,actor,payload_id,receipt_id)
            VALUES (?,?,?,'confirmed',NULL,NULL,?,?,?,1,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (effect_id, subject, operation, header.parent_revision, header.graph_revision, native['revision'],
             ticket.source_ref, 'refs/heads/' + header.run_branch, integrated, integrated,
             ticket.candidate_sha, ticket.candidate_tree, ticket.base_sha, base_tree,
             job['value_pr_number'], ticket.close_intent['value_actor'], payload_id, receipt_id))
        receipt_ids.append(effect_id)
    supporting_development = [call for call in ticket.calls if call.role == Role.DEVELOPMENT][-1]
    tx.execute('INSERT INTO completions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (subject, 1, native['revision'], header.parent_revision, header.graph_revision, publication,
         integrated, ticket.candidate_tree, job['value_pr_number'], 'review', ticket.accepted_review,
         supporting_development.attempt_id, *receipt_ids))


def build_original_run_candidate(snapshot: SourceSnapshot, facts: OriginalRunFacts, destination: Path, *,
        repository_root: Path, state_root: Path, origin: Mapping[str, str],
        resources: Mapping[str, tuple[ResourceProof, ...]], run_head: str,
        head_evidence: tuple[SourceKey, ...], external_readproof: Mapping[str, Any],
        external_readproof_sha256: str, close_readproofs: Mapping[str, Mapping[str, Any]],
        profile_document: bytes) -> Kernel:
    """Create once with the complete graph, then install only authenticated facts.

    Callers must authenticate source/Git/external observations and hold all
    relevant locks before publishing. This private file never binds a Task.
    """
    header = facts.header
    if (snapshot.sha256 != facts.source_sha256 or origin['source_sha256'] != facts.source_sha256
            or origin['run_id'] != header.run_id or set(resources) != {ticket.owner.subject for ticket in facts.tickets}):
        raise MigrationRefused('source_original_identity')
    base = Commit(header.default_sha, _git(repository_root, 'rev-parse', header.default_sha + '^{tree}'))
    common = repository_root / '.git'
    worktrees = state_root / 'worktrees' / header.run_id
    deliveries = []
    for ticket in facts.tickets:
        checkout = worktrees / f'ticket-{ticket.owner.ticket}'
        deliveries.append(Delivery(ticket.owner.subject, DeliveryKind.TICKET, ticket.owner.ticket, 'source-verified',
            base, f'refs/heads/agent-run/{header.run_id}/ticket-{ticket.owner.ticket}', 'refs/heads/' + header.run_branch,
            checkout=str(checkout), owned_root=str(worktrees), common_git_dir=str(common)))
    final_checkout = repository_root.parent / 'worktrees' / header.run_id / 'run-publication'
    deliveries.append(Delivery(f'final:{header.run_id}', DeliveryKind.FINAL_RUN, header.parent_number, header.parent_revision,
        base, 'refs/heads/' + header.run_branch, 'refs/heads/' + header.default_branch,
        checkout=str(final_checkout), owned_root=str(final_checkout.parent), common_git_dir=str(common)))
    spec = RunSpec(header.run_id, header.repository, header.parent_number, header.parent_revision, header.graph_revision,
                   tuple(deliveries), header.policy, header.language, str(repository_root), header.inputs)
    kernel = Kernel.create(destination, spec, requirements=header.requirements, source_origin=origin)
    with kernel.storage.transaction() as tx:
        tx.execute('UPDATE runs SET created_at=?', (header.created_at,))
        proofs = []
        for ticket in facts.tickets:
            if isinstance(ticket, UnallocatedTicket):
                proofs.append(SubjectProof(ticket.owner, 'unallocated', None, None, None, resources[ticket.owner.subject]))
                continue
            _install_worked_ticket(tx, facts, ticket, repository_root, close_readproofs.get(ticket.owner.subject))
            proofs.append(SubjectProof(ticket.owner, ticket.kind, ticket.base_sha, ticket.budget,
                ticket.calls[-1].attempt_id if ticket.kind == 'returned' else None, resources[ticket.owner.subject]))
        anchor = tx.one('SELECT id FROM subjects ORDER BY id LIMIT 1')[0]
        install_run_proofs(tx, origin=origin, anchor=anchor, profile_sha256=facts.profile_sha256,
            external_readproof_sha256=external_readproof_sha256, run_ref='refs/heads/' + header.run_branch,
            run_head=run_head, head_evidence=head_evidence, subjects=tuple(proofs), created_at=header.created_at)
        material = tx.put(anchor, 'v6_external_readproof', wire_json(dict(external_readproof)))
        tx.execute('INSERT INTO history(subject,kind,identity,material_id,generation,created_at) VALUES (?,?,?,?,1,?)',
            (anchor, 'v6_external_readproof', external_readproof_sha256, material, header.created_at))
    _preserve_copy(snapshot, kernel, run_id=header.run_id, subject=anchor,
                   profile_document=profile_document, whole_source=True)
    return kernel
