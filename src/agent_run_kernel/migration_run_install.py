"""Install proved whole-Run facts into one unpublished Core-created database."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Mapping

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


def attempt_values(facts: OriginalRunFacts, ticket: WorkedTicket, call: AttemptFacts, ordinal: int,
                   revision: str, completion_revision: str, context_id: str, result_id: str) -> dict[str, Any]:
    boundary = call.allocation_boundary
    outcome = None if call.pending else (
        'development' if call.role == Role.DEVELOPMENT else 'publication' if call.role == Role.PUBLICATION
        else 'pass' if all(check['status'] == 'pass' for check in call.normalized['checks'].values()) else 'fail')
    return dict(id=call.attempt_id, subject=ticket.owner.subject, generation=1, ordinal=ordinal,
        role=call.role.value, purpose='requirements', window=1, parent_revision=facts.header.parent_revision,
        graph_revision=facts.header.graph_revision, revision=revision, base_sha=ticket.base_sha,
        candidate_sha=boundary.get('candidate_sha'), candidate_tree=boundary.get('candidate_tree'),
        expected_tree=boundary.get('candidate_tree'), completion_revision=completion_revision,
        context_id=context_id, current_invocation=None, result_id=result_id,
        status='returned' if call.pending else 'consumed',
        accounted=int(call.role == Role.REVIEW and call.selected_version['budget_consumed'] == 1), outcome=outcome)


def imported_invocation(call: AttemptFacts) -> dict[str, Any]:
    return {'attempt_id': call.attempt_id, 'old_role_ordinal': call.old_ordinal,
        'thread_id': call.thread_id, 'raw_evidence_id': call.raw_evidence_id, 'returned': dict(call.raw),
        'invocation': _wire_row(call.invocation), 'bound_version': _wire_row(call.bound_version),
        'selected_version': _wire_row(call.selected_version),
        'global_current_version': _wire_row(call.global_current_version)}


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
        context = tx.put(subject, 'v6_attempt_input_witness', wire_json(
            attempt_witness(facts, ticket, call, native['revision'], native['completion_revision'])))
        result = tx.put(subject, 'v6_normalized_artifact', wire_json(dict(call.normalized)))
        values = attempt_values(facts, ticket, call, index, native['revision'], native['completion_revision'], context, result)
        tx.execute('INSERT INTO attempts(' + ','.join(values) + ') VALUES (' + ','.join('?' for _ in values) + ')',
                   tuple(values.values()))
        original = imported_invocation(call)
        material = tx.put(subject, 'v6_imported_invocation', wire_json(original))
        tx.execute('INSERT INTO history(subject,kind,identity,material_id,generation,created_at) VALUES (?,?,?,?,1,?)',
            (subject, 'imported_invocation', ':'.join((header.run_id, subject, call.invocation['started_at'])),
             material, _created_at_epoch(call.invocation['ended_at'])))


def worked_ticket_values(facts: OriginalRunFacts, ticket: WorkedTicket, repository: Path) -> dict[str, Any]:
    header, subject, job = facts.header, ticket.owner.subject, ticket.job
    completed = ticket.kind == 'completed'
    role = ticket.calls[-1].role
    phase = 'complete' if completed else {Role.DEVELOPMENT: 'development', Role.REVIEW: 'review', Role.PUBLICATION: 'publish'}[role]
    development = [call for call in ticket.calls if call.role == Role.DEVELOPMENT][-1]
    base_tree = _git(repository, 'rev-parse', ticket.base_sha + '^{tree}')
    publication = job['value_publication_sha'] if completed else None
    integrated = job['value_integrated_sha'] if completed else ticket.candidate_sha if role == Role.PUBLICATION else None
    return dict(phase=phase, base_sha=ticket.base_sha, base_tree=base_tree,
        source_sha=publication or ticket.base_sha, current_attempt=None if completed else ticket.calls[-1].attempt_id,
        review_attempt=ticket.accepted_review, development_thread=_thread_id(development),
        candidate_sha=ticket.candidate_sha, candidate_tree=ticket.candidate_tree,
        workspace_sha=publication or ticket.candidate_sha or ticket.base_sha, expected_tree=ticket.candidate_tree,
        integrated_sha=integrated, integrated_tree=ticket.candidate_tree if integrated else None,
        integration_target=ticket.integration_target, gate_mode='review' if completed or role == Role.PUBLICATION else None,
        publication_sha=publication, publication_tree=ticket.candidate_tree if publication else None,
        pr_number=job['value_pr_number'] if completed else None, merge_sha=job['value_integrated_sha'] if completed else None)


def _install_worked_ticket(tx: Transaction, facts: OriginalRunFacts, ticket: WorkedTicket,
                           repository: Path, close_readproof: Mapping[str, Any] | None) -> None:
    header, subject = facts.header, ticket.owner.subject
    values = worked_ticket_values(facts, ticket, repository)
    tx.execute('UPDATE subjects SET ' + ','.join(key + '=?' for key in values) + ' WHERE id=?',
               (*values.values(), subject))
    tx.execute("UPDATE budgets SET developments_used=?,reviews_used=? WHERE subject=? AND purpose='requirements' AND window=1",
               (ticket.budget.developments_used, ticket.budget.reviews_used, subject))
    if ticket.integration is not None:
        tx.execute('INSERT INTO integration_snapshots VALUES (?,?,?,?,?,?,?,0)',
            (subject, 1, 1, ticket.source_ref, 'refs/heads/' + header.run_branch,
             ticket.base_sha, ticket.integration_target))
    _install_attempts(tx, facts, ticket)
    if ticket.kind == 'completed':
        native = tx.one('SELECT revision FROM subjects WHERE id=?', (subject,))
        for table, rows in completion_values(facts, ticket, native['revision'], values['base_tree'], close_readproof,
                                             lambda purpose, body: tx.put(subject, purpose, body)).items():
            for row in rows:
                tx.execute('INSERT INTO ' + table + '(' + ','.join(row) + ') VALUES (' + ','.join('?' for _ in row) + ')',
                           tuple(row.values()))


def completion_values(facts: OriginalRunFacts, ticket: WorkedTicket, revision: str, base_tree: str,
        close_readproof: Mapping[str, Any] | None, put: Callable[[str, bytes], str]) -> dict[str, list[dict[str, Any]]]:
    header, subject, job = facts.header, ticket.owner.subject, ticket.job
    if close_readproof is None or ticket.close_intent is None or ticket.close_ownership is None:
        raise MigrationRefused('source_external_close')
    if close_readproof['event']['id'] != ticket.close_ownership['value_event_id']:
        raise MigrationRefused('source_close_ownership')
    effects = []
    for operation in ('completion_comment', 'close'):
        payload, body = completion_documents(facts, ticket, close_readproof)
        effects.append(dict(id=f'v6-import:{header.run_id}:{ticket.owner.ticket}:{operation}', subject=subject,
            operation=operation, status='confirmed', executor=None, epoch=None,
            parent_revision=header.parent_revision, graph_revision=header.graph_revision, revision=revision, generation=1,
            source_ref=ticket.source_ref, target_ref='refs/heads/' + header.run_branch,
            before_sha=job['value_integrated_sha'], after_sha=job['value_integrated_sha'],
            candidate_sha=ticket.candidate_sha, candidate_tree=ticket.candidate_tree,
            base_sha=ticket.base_sha, base_tree=base_tree, pr_number=job['value_pr_number'],
            actor=ticket.close_intent['value_actor'],
            payload_id=put('v6_confirmed_effect_payload', wire_json(payload)),
            receipt_id=put('v6_confirmed_effect_receipt', wire_json(body))))
    development = [call for call in ticket.calls if call.role == Role.DEVELOPMENT][-1]
    completion = dict(subject=subject, generation=1, revision=revision, parent_revision=header.parent_revision,
        graph_revision=header.graph_revision, source_sha=job['value_publication_sha'],
        merge_sha=job['value_integrated_sha'], tree=ticket.candidate_tree, pr_number=job['value_pr_number'],
        gate_mode='review', review_attempt=ticket.accepted_review, development_attempt=development.attempt_id,
        comment_effect=effects[0]['id'], close_effect=effects[1]['id'])
    return {'effects': effects, 'completions': [completion]}


def original_run_spec(facts: OriginalRunFacts, repository_root: Path, state_root: Path) -> RunSpec:
    header = facts.header
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
    return RunSpec(header.run_id, header.repository, header.parent_number, header.parent_revision, header.graph_revision,
                   tuple(deliveries), header.policy, header.language, str(repository_root), header.inputs)


def run_head_evidence(facts: OriginalRunFacts, run_head: str) -> tuple[SourceKey, ...]:
    keys = tuple(ticket.owner.job for ticket in facts.tickets if isinstance(ticket, WorkedTicket)
                 and ticket.kind == 'completed' and ticket.job['value_integrated_sha'] == run_head
                 and ticket.owner.job is not None)
    if keys:
        return keys
    if run_head != facts.header.default_sha:
        raise MigrationRefused('source_run_head_unproven')
    return (SourceKey('run_condition_base', (facts.header.run_id,)),)


def subject_proofs(facts: OriginalRunFacts,
                   resources: Mapping[str, tuple[ResourceProof, ...]]) -> tuple[SubjectProof, ...]:
    return tuple(SubjectProof(ticket.owner, 'unallocated', None, None, None, resources[ticket.owner.subject])
        if isinstance(ticket, UnallocatedTicket) else SubjectProof(ticket.owner, ticket.kind, ticket.base_sha,
            ticket.budget, ticket.calls[-1].attempt_id if ticket.kind == 'returned' else None,
            resources[ticket.owner.subject]) for ticket in facts.tickets)


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
    spec = original_run_spec(facts, repository_root, state_root)
    kernel = Kernel.create(destination, spec, requirements=header.requirements, source_origin=origin)
    with kernel.storage.transaction() as tx:
        tx.execute('UPDATE runs SET created_at=?', (header.created_at,))
        for ticket in facts.tickets:
            if isinstance(ticket, WorkedTicket):
                _install_worked_ticket(tx, facts, ticket, repository_root, close_readproofs.get(ticket.owner.subject))
        proofs = subject_proofs(facts, resources)
        anchor = tx.one('SELECT id FROM subjects ORDER BY id LIMIT 1')[0]
        install_run_proofs(tx, origin=origin, anchor=anchor, profile_sha256=facts.profile_sha256,
            external_readproof_sha256=external_readproof_sha256, run_ref='refs/heads/' + header.run_branch,
            run_head=run_head, head_evidence=head_evidence, subjects=proofs, created_at=header.created_at)
        material = tx.put(anchor, 'v6_external_readproof', wire_json(dict(external_readproof)))
        tx.execute('INSERT INTO history(subject,kind,identity,material_id,generation,created_at) VALUES (?,?,?,?,1,?)',
            (anchor, 'v6_external_readproof', external_readproof_sha256, material, header.created_at))
    _preserve_copy(snapshot, kernel, run_id=header.run_id, subject=anchor,
                   profile_document=profile_document, whole_source=True)
    from .migration_run_projection import verify_prepared_installation
    verify_prepared_installation(kernel, facts, origin, external_readproof, external_readproof_sha256, resources, run_head)
    return kernel
