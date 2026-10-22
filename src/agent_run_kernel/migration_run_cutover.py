"""One fenced whole-Run cutover and bounded inverse of its entire resource set."""
from __future__ import annotations

from contextlib import ExitStack, closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from typing import Any, Mapping

from agent_run.managed_workspace import ManagedWorkspace
from agent_run.runner_lease import default_runner_lock_path, runner_management_lease
from agent_run.task_identity import TaskKey

from .cleanup_adapter import _hold_ref
from .effects import LocalGit
from .kernel import Kernel
from .legacy_admission import legacy_task_lock, read_source_fence, source_fence
from .migration import MigrationRefused, SourceSnapshot, capture_v6
from .migration_completed import _read_document
from .migration_conversion import _git
from .migration_cutover import _OLD_PROTOCOL, _original_protocol, _retire_original, _withdraw_original_pointer
from .migration_layout import detach_layout, restore_layout, verify_layout
from .migration_records import V6RecordReader
from .migration_run import OriginalRunFacts, UnallocatedTicket, WorkedTicket, read_original_run
from .migration_run_external import verify_run_external
from .migration_run_facts import SourceKey
from .migration_run_install import attempt_witness, build_original_run_candidate, completion_documents, _thread_id
from .migration_run_proof import ResourceProof, SubjectProof
from .migration_run_resources import authenticate_run_resources, run_resource_proofs
from .model import DeliveryKind, Purpose, Role, wire_json
from .task_location import bind_task, locked_task, read_task


def _proofs(facts: OriginalRunFacts, resources: Mapping[str, tuple[ResourceProof, ...]]) -> tuple[SubjectProof, ...]:
    return tuple(SubjectProof(ticket.owner, 'unallocated', None, None, None, resources[ticket.owner.subject])
        if isinstance(ticket, UnallocatedTicket) else SubjectProof(ticket.owner, ticket.kind, ticket.base_sha,
            ticket.budget, ticket.calls[-1].attempt_id if ticket.kind == 'returned' else None,
            resources[ticket.owner.subject]) for ticket in facts.tickets)


def _head_evidence(facts: OriginalRunFacts, run_head: str) -> tuple[SourceKey, ...]:
    keys = tuple(ticket.owner.job for ticket in facts.tickets if isinstance(ticket, WorkedTicket)
                 and ticket.kind == 'completed' and ticket.job['value_integrated_sha'] == run_head
                 and ticket.owner.job is not None)
    if keys:
        return keys
    if run_head != facts.header.default_sha:
        raise MigrationRefused('source_run_head_unproven')
    return (SourceKey('run_condition_base', (facts.header.run_id,)),)


def prepared_original_run(native: Kernel, facts: OriginalRunFacts, origin: Mapping[str, str],
        proof: Mapping[str, Any], proof_sha: str, resources: Mapping[str, tuple[ResourceProof, ...]], run_head: str) -> None:
    """Authenticate the whole installed set; any native progress requires rollforward."""
    header = facts.header
    source_proofs = {item.owner.subject: item for item in _proofs(facts, resources)}
    expected_effects = {(f'v6-import:{header.run_id}:{ticket.owner.ticket}:{operation}', ticket.owner.subject, operation)
                        for ticket in facts.tickets if isinstance(ticket, WorkedTicket) and ticket.kind == 'completed'
                        for operation in ('close', 'completion_comment')}
    with native.storage.transaction(write=False) as tx:
        run = tx.one('SELECT * FROM runs')
        if (native.storage.path != Path(origin['target_db']) or run['id'] != header.run_id
                or run['repository'] != header.repository or run['parent_issue'] != header.parent_number
                or run['repository_root'] != str(header.task.workspace)
                or run['executor'] is not None or run['stopped'] or run['blocked'] is not None
                or run['epoch'] != 0 or run['profile_revision'] != 1
                or run['accepted_parent_revision'] != header.parent_revision
                or run['policy'] != wire_json(Kernel._policy_values(header.policy))
                or run['concurrency'] != header.policy.concurrency or run['language'] != header.language
                or run['final_action'] is not None or run['integration_holder'] is not None or run['next_ready'] != 0
                or run['parent_revision'] != header.parent_revision or run['graph_revision'] != header.graph_revision
                or any(tx.one(f'SELECT count(*) FROM {table}')[0] for table in (
                    'actions', 'hosts', 'invocations', 'leases', 'retired_executors', 'worker_activity',
                    'retired_generations', 'final_barriers', 'resource_cleanup', 'work_queue', 'integration_queue',
                    'waits', 'gates', 'grants', 'checks', 'effect_failures', 'issue_links'))):
            raise MigrationRefused('native_original_cutover_advanced')
        anchor = tx.one("SELECT subject,identity,material_id FROM history WHERE kind='v6_original_cutover'")
        manifest = tx.one("SELECT h.*,m.purpose AS material_purpose FROM history h JOIN material m ON m.id=h.material_id WHERE h.kind='v6_run_import'")
        body = json.loads(tx.get(manifest['material_id'], manifest['subject']))
        if not isinstance(body, dict) or set(body) != {'origin_nonce', 'source_sha256', 'profile_sha256',
                'external_readproof_sha256', 'resource_mode', 'run_record', 'run_ref_ownership', 'subjects'}:
            raise MigrationRefused('native_original_cutover_provenance')
        external = tx.one("SELECT subject,identity,material_id FROM history WHERE kind='v6_external_readproof'")
        if (anchor['identity'] != origin['nonce'] or tx.get(anchor['material_id'], anchor['subject']) != wire_json(dict(origin))
                or manifest['subject'] != anchor['subject'] or manifest['identity'] != origin['nonce']
                or manifest['material_purpose'] != 'v6_run_import' or manifest['generation'] != 1
                or body.get('origin_nonce') != origin['nonce'] or body.get('source_sha256') != facts.source_sha256
                or body.get('profile_sha256') != facts.profile_sha256 or body.get('external_readproof_sha256') != proof_sha
                or body.get('resource_mode') != 'existing-verified'
                or body.get('run_record') != SourceKey('run_root', (header.run_id,)).document()
                or external['identity'] != proof_sha or tx.get(external['material_id'], external['subject']) != wire_json(dict(proof))
                or body['run_ref_ownership'] != {'source_ref': 'refs/heads/' + header.run_branch, 'expected_oid': run_head,
                    'source_record': SourceKey('run_root', (header.run_id,)).document(),
                    'head_evidence': [key.document() for key in _head_evidence(facts, run_head)]}):
            raise MigrationRefused('native_original_cutover_provenance')
        entries = body.get('subjects')
        if (not isinstance(entries, list) or len(entries) != len(source_proofs)
                or {entry.get('subject') for entry in entries if isinstance(entry, dict)} != set(source_proofs)
                or tx.one("SELECT count(*) FROM history WHERE kind='v6_subject_import'")[0] != len(source_proofs)):
            raise MigrationRefused('native_original_cutover_provenance')
        for entry in entries:
            source = source_proofs[entry['subject']]
            imported = tx.one("SELECT h.identity,h.material_id,h.generation,m.purpose FROM history h JOIN material m ON m.id=h.material_id WHERE h.kind='v6_subject_import' AND h.subject=?",
                              (entry['subject'],))
            if (set(entry) != {'subject', 'generation', 'proof_id'} or entry['generation'] != 1
                    or imported['identity'] != origin['nonce'] or imported['material_id'] != entry['proof_id']
                    or imported['generation'] != 1 or imported['purpose'] != 'v6_subject_import'
                    or tx.get(entry['proof_id'], entry['subject']) != wire_json(source.document(origin))):
                raise MigrationRefused('native_original_cutover_provenance')
        actual = tx.rows('SELECT * FROM subjects ORDER BY id')
        if {row['id'] for row in actual} != set(source_proofs):
            raise MigrationRefused('native_original_cutover_advanced')
        attempts = []
        completions = []
        budgets = []
        resync = []
        integrations = []
        for ticket in facts.tickets:
            subject = ticket.owner.subject
            job = next(row for row in actual if row['id'] == subject)
            phase = 'workspace' if isinstance(ticket, UnallocatedTicket) else 'complete' if ticket.kind == 'completed' else {
                Role.DEVELOPMENT: 'development', Role.REVIEW: 'review', Role.PUBLICATION: 'publish'}[ticket.calls[-1].role]
            base = header.default_sha if isinstance(ticket, UnallocatedTicket) else ticket.base_sha
            base_tree = _git(header.task.workspace, 'rev-parse', base + '^{tree}')
            issue = next(issue for issue in header.requirements.issues if issue.number == ticket.owner.ticket)
            revision = Kernel._fingerprint({'title': issue.title, 'body': issue.body})
            expected_current = None if isinstance(ticket, UnallocatedTicket) or ticket.kind == 'completed' else ticket.calls[-1].attempt_id
            if (job['generation'] != 1 or job['phase'] != phase or job['current_attempt'] != expected_current
                    or job['run_id'] != header.run_id or job['kind'] != 'ticket' or job['issue'] != ticket.owner.ticket
                    or job['purpose'] != 'requirements' or job['version'] != 1 or job['revision'] != revision
                    or job['completion_revision'] != 'no-tickets' or job['base_tree'] != base_tree
                    or any(job[field] is not None for field in ('checks_id', 'grant_id', 'ci_failure', 'last_failure',
                        'resume_purpose', 'repair_source', 'validation_checkout', 'validation_cleanup', 'refresh_revision', 'policy_id'))
                    or job['base_sha'] != base or job['source_ref'] != f'refs/heads/agent-run/{header.run_id}/ticket-{ticket.owner.ticket}'
                    or job['target_ref'] != 'refs/heads/' + header.run_branch
                    or job['owned_root'] != str(Path(origin['source_db']).parent / 'worktrees' / header.run_id)
                    or job['common_git_dir'] != str(header.task.workspace / '.git')
                    or job['checkout'] != str(Path(origin['source_db']).parent / 'worktrees' / header.run_id / f'ticket-{ticket.owner.ticket}')):
                raise MigrationRefused('native_original_cutover_advanced')
            expected_budget = (0, 0) if isinstance(ticket, UnallocatedTicket) else (ticket.budget.developments_used, ticket.budget.reviews_used)
            for purpose, (development_limit, review_limit) in native._budget_limits(header.policy, DeliveryKind.TICKET).items():
                used = expected_budget if purpose == Purpose.REQUIREMENTS else (0, 0)
                review_required = purpose == Purpose.REQUIREMENTS or purpose == Purpose.CI and header.policy.ticket_ci_review
                budgets.append((subject, purpose.value, 1, *used, development_limit, review_limit, int(review_required), 1))
            resync.append((subject, 1, 0, header.policy.resync_limit, 1))
            if isinstance(ticket, UnallocatedTicket):
                if (job['source_sha'] != base or job['workspace_sha'] != base
                        or any(job[field] is not None for field in ('candidate_sha', 'candidate_tree', 'development_thread',
                            'integrated_sha', 'integrated_tree', 'publication_sha', 'publication_tree', 'expected_tree',
                            'integration_target', 'review_attempt', 'publication_attempt', 'gate_mode', 'pr_number', 'merge_sha'))):
                    raise MigrationRefused('native_original_cutover_advanced')
                continue
            workspace = ticket.job['value_publication_sha'] if ticket.kind == 'completed' else ticket.candidate_sha or ticket.base_sha
            integrated = (ticket.job['value_integrated_sha'] if ticket.kind == 'completed'
                          else ticket.candidate_sha if ticket.calls[-1].role == Role.PUBLICATION else None)
            if (job['candidate_sha'] != ticket.candidate_sha or job['candidate_tree'] != ticket.candidate_tree
                    or job['source_sha'] != (ticket.job['value_publication_sha'] if ticket.kind == 'completed' else base)
                    or job['integrated_sha'] != integrated
                    or job['integrated_tree'] != (ticket.candidate_tree if integrated else None)
                    or job['integration_target'] != ticket.integration_target or job['expected_tree'] != ticket.candidate_tree
                    or job['review_attempt'] != ticket.accepted_review or job['publication_attempt'] is not None
                    or job['development_thread'] != _thread_id([call for call in ticket.calls if call.role == Role.DEVELOPMENT][-1])
                    or job['publication_sha'] != (ticket.job['value_publication_sha'] if ticket.kind == 'completed' else None)
                    or job['publication_tree'] != (ticket.candidate_tree if ticket.kind == 'completed' else None)
                    or job['pr_number'] != (ticket.job['value_pr_number'] if ticket.kind == 'completed' else None)
                    or job['merge_sha'] != (ticket.job['value_integrated_sha'] if ticket.kind == 'completed' else None)
                    or job['gate_mode'] != ('review' if ticket.kind == 'completed' or ticket.calls[-1].role == Role.PUBLICATION else None)
                    or job['workspace_sha'] != workspace):
                raise MigrationRefused('native_original_cutover_advanced')
            if ticket.integration is not None:
                integrations.append((subject, 1, 1, ticket.source_ref, 'refs/heads/' + header.run_branch,
                                     ticket.base_sha, ticket.integration_target, 0))
            for index, call in enumerate(ticket.calls, start=1):
                attempt = tx.one('SELECT * FROM attempts WHERE id=?', (call.attempt_id,))
                outcome = None if call.pending else (
                    'development' if call.role == Role.DEVELOPMENT else 'publication' if call.role == Role.PUBLICATION
                    else 'pass' if all(check['status'] == 'pass' for check in call.normalized['checks'].values()) else 'fail')
                if (attempt['subject'] != subject or attempt['generation'] != 1 or attempt['ordinal'] != index
                        or attempt['role'] != call.role.value or attempt['status'] != ('returned' if call.pending else 'consumed')
                        or attempt['purpose'] != 'requirements' or attempt['window'] != 1 or attempt['base_sha'] != base
                        or attempt['parent_revision'] != header.parent_revision or attempt['graph_revision'] != header.graph_revision
                        or attempt['revision'] != revision or attempt['completion_revision'] != job['completion_revision']
                        or attempt['candidate_sha'] != call.allocation_boundary.get('candidate_sha')
                        or attempt['candidate_tree'] != call.allocation_boundary.get('candidate_tree')
                        or attempt['expected_tree'] != call.allocation_boundary.get('candidate_tree')
                        or attempt['new_thread'] != 0 or attempt['resume_action'] is not None
                        or attempt['outcome'] != outcome
                        or tx.get(attempt['context_id'], subject) != wire_json(attempt_witness(facts, ticket, call, revision, job['completion_revision']))
                        or attempt['current_invocation'] is not None
                        or attempt['accounted'] != int(call.role == Role.REVIEW and call.selected_version['budget_consumed'] == 1)
                        or tx.get(attempt['result_id'], subject) != wire_json(dict(call.normalized))):
                    raise MigrationRefused('native_original_cutover_advanced')
                attempts.append(call.attempt_id)
            if ticket.kind == 'completed':
                completions.append((subject, 1, job['revision'], header.parent_revision, header.graph_revision,
                    ticket.job['value_publication_sha'], ticket.job['value_integrated_sha'], ticket.candidate_tree,
                    ticket.job['value_pr_number'], 'review', ticket.accepted_review,
                    [call.attempt_id for call in ticket.calls if call.role == Role.DEVELOPMENT][-1],
                    f'v6-import:{header.run_id}:{ticket.owner.ticket}:completion_comment', f'v6-import:{header.run_id}:{ticket.owner.ticket}:close'))
        if (set(attempts) != {row[0] for row in tx.rows('SELECT id FROM attempts')}
                or set(budgets) != {tuple(row) for row in tx.rows('SELECT * FROM budgets')}
                or set(resync) != {tuple(row) for row in tx.rows('SELECT * FROM resync_allowances')}
                or set(integrations) != {tuple(row) for row in tx.rows('SELECT * FROM integration_snapshots')}
                or sorted(completions) != sorted(tuple(row) for row in tx.rows('SELECT * FROM completions'))
                or {(row['id'], row['subject'], row['operation']) for row in tx.rows('SELECT * FROM effects')} != expected_effects
                or tx.rows("SELECT id FROM effects WHERE status!='confirmed' OR executor IS NOT NULL OR epoch IS NOT NULL")
                or tx.rows("SELECT subject FROM budgets WHERE purpose!='requirements' AND (developments_used!=0 OR reviews_used!=0)")):
            raise MigrationRefused('native_original_cutover_advanced')
        threads = {(_thread_id(call), ticket.owner.subject, call.binding['role'], 1, call.thread_id)
                   for ticket in facts.tickets if isinstance(ticket, WorkedTicket) for call in ticket.calls}
        if {tuple(row) for row in tx.rows('SELECT id,subject,role,profile_revision,backend_id FROM threads')} != threads:
            raise MigrationRefused('native_original_cutover_advanced')
        _, closes = verify_run_external(facts, proof, header.task.workspace)
        for ticket in facts.tickets:
            if not isinstance(ticket, WorkedTicket) or ticket.kind != 'completed':
                continue
            payload, expected_receipt = completion_documents(facts, ticket, closes[ticket.owner.subject])
            for operation in ('close', 'completion_comment'):
                effect = tx.one('SELECT * FROM effects WHERE id=?', (f'v6-import:{header.run_id}:{ticket.owner.ticket}:{operation}',))
                if (effect['generation'] != 1 or effect['before_sha'] != ticket.job['value_integrated_sha']
                        or effect['after_sha'] != ticket.job['value_integrated_sha'] or effect['source_ref'] != ticket.source_ref
                        or effect['target_ref'] != 'refs/heads/' + header.run_branch or effect['base_sha'] != ticket.base_sha
                        or effect['candidate_sha'] != ticket.candidate_sha or effect['candidate_tree'] != ticket.candidate_tree
                        or tx.get(effect['payload_id'], ticket.owner.subject) != wire_json(payload)
                        or tx.get(effect['receipt_id'], ticket.owner.subject) != wire_json(expected_receipt)):
                    raise MigrationRefused('native_original_cutover_provenance')
    reader = V6RecordReader(native)
    if (reader.source['sha256'] != facts.source_sha256 or reader.source['run_id'] != header.run_id
            or reader.row('run_root', [header.run_id])['value_checkout_identity'] != origin['source_uuid']):
        raise MigrationRefused('native_original_cutover_provenance')


def _origin(snapshot: SourceSnapshot, facts: OriginalRunFacts, managed: ManagedWorkspace, nonce: str) -> dict[str, str]:
    with closing(sqlite3.connect(snapshot.path.as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
        uuid = db.execute('SELECT value_checkout_identity FROM run_root WHERE run_id=?', (facts.header.run_id,)).fetchone()[0]
    return read_source_fence(source_fence({'nonce': nonce, 'source_db': str(snapshot.source),
        'source_sha256': snapshot.sha256, 'run_id': facts.header.run_id,
        'task_fingerprint': facts.header.task.fingerprint, 'source_uuid': uuid,
        'target_db': str(managed.state_root / facts.header.run_id / 'kernel.sqlite3')}))


def _refs(facts: OriginalRunFacts, resources: Mapping[str, tuple[ResourceProof, ...]], run_head: str) -> dict[str, str]:
    refs = {'refs/heads/' + facts.header.run_branch: run_head,
            'refs/heads/' + facts.header.default_branch: facts.header.default_sha}
    for group in resources.values():
        for resource in group:
            layout = resource.layout
            if layout['source_ref'] in refs:
                raise MigrationRefused('source_resource_union')
            refs[layout['source_ref']] = layout['expected_oid']
    return refs


def migrate_original_run_v6(snapshot: SourceSnapshot, *, repository: str, parent_number: int,
        nonce: str, profile_snapshot: Path, external_snapshot: Path) -> Kernel:
    managed = ManagedWorkspace.for_repository(repository)
    task = TaskKey(managed.repository_root, repository, parent_number)
    if snapshot.source != managed.state_root / 'state.sqlite3' or snapshot.path.is_symlink() or snapshot.path.samefile(snapshot.source):
        raise MigrationRefused('source_original_identity')
    with runner_management_lease(default_runner_lock_path()), legacy_task_lock(task, managed.state_root), locked_task(task) as location:
        managed.open_path()
        sentinel = managed.state_root / 'task-control' / (task.fingerprint + '.json')
        if sentinel.exists() or sentinel.is_symlink():
            raise MigrationRefused('source_legacy_control_json')
        with tempfile.TemporaryDirectory(dir=managed.state_root, prefix='.whole-cutover-') as directory:
            checked = capture_v6(snapshot.path, Path(directory) / 'sealed.sqlite3')
            if (checked.sha256, checked.schema_sha256, checked.table_rows) != (snapshot.sha256, snapshot.schema_sha256, snapshot.table_rows):
                raise MigrationRefused('snapshot_changed')
            accepted = SourceSnapshot(snapshot.source, checked.path, checked.sha256, checked.source_protocol, checked.schema_sha256, checked.table_rows)
            facts = read_original_run(accepted, profile_snapshot)
            if facts.header.task != task:
                raise MigrationRefused('source_original_identity')
            origin = _origin(accepted, facts, managed, nonce)
            proof, proof_sha = _read_document(external_snapshot)
            sealed = _read_document(external_snapshot.parent / 'checkouts.json')[0]
            run_head = proof.get('remote_heads', {}).get('refs/heads/' + facts.header.run_branch)
            if not isinstance(run_head, str):
                raise MigrationRefused('source_external_refs')
            destination = Path(origin['target_db'])
            protocol = _original_protocol(snapshot.source)
            retired = protocol != _OLD_PROTOCOL
            if retired and read_source_fence(protocol) != origin:
                raise MigrationRefused('source_original_fence_changed')
            prior = read_task(location)
            if prior is not None and (not retired or prior != (facts.header.run_id, destination)):
                raise MigrationRefused('native_task_already_bound')
            resources = run_resource_proofs(facts, origin, sealed)
            if destination.exists():
                if destination.is_symlink():
                    raise MigrationRefused('native_original_cutover_provenance')
                native = Kernel(destination)
                prepared_original_run(native, facts, origin, proof, proof_sha, resources, run_head)
            else:
                if retired:
                    raise MigrationRefused('native_original_cutover_missing')
                destination.parent.mkdir(mode=0o700, exist_ok=True)
                run_head, closes = verify_run_external(facts, proof, managed.repository_root)
                resources = authenticate_run_resources(accepted, facts, origin, external_snapshot.parent, sealed, run_head)
                with tempfile.TemporaryDirectory(dir=destination.parent, prefix='.whole-prepared-') as private:
                    candidate = Path(private) / 'kernel.sqlite3'
                    build_original_run_candidate(accepted, facts, candidate, repository_root=managed.repository_root,
                        state_root=managed.state_root, origin=origin, resources=resources, run_head=run_head,
                        head_evidence=_head_evidence(facts, run_head), external_readproof=proof,
                        external_readproof_sha256=proof_sha, close_readproofs=closes, profile_document=profile_snapshot.read_bytes())
                    descriptor = os.open(candidate, os.O_RDONLY)
                    try:
                        os.fsync(descriptor)
                    finally:
                        os.close(descriptor)
                    os.link(candidate, destination)
                    descriptor = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        os.fsync(descriptor)
                    finally:
                        os.close(descriptor)
                native = Kernel(destination)
            prepared_original_run(native, facts, origin, proof, proof_sha, resources, run_head)
            verify_run_external(facts, proof, managed.repository_root)
            with native.storage.transaction(write=False) as tx:
                if tx.rows("SELECT 1 FROM history WHERE kind='v6_original_rollback_started'"):
                    raise MigrationRefused('native_original_rollback_started')
            with ExitStack() as refs:
                for ref, sha in sorted(_refs(facts, resources, run_head).items()):
                    refs.enter_context(_hold_ref(LocalGit(managed.repository_root), managed.repository_root, ref, sha))
                authenticate_run_resources(accepted, facts, origin, external_snapshot.parent, sealed, run_head, detached=retired)
                _retire_original(snapshot.source, checked.path, origin, retired=retired)
                for subject in sorted(resources):
                    for resource in resources[subject]:
                        detach_layout(resource.layout)
                authenticate_run_resources(accepted, facts, origin, external_snapshot.parent, sealed, run_head, detached=True)
                for group in resources.values():
                    for resource in group:
                        verify_layout(resource.layout, state='detached')
                if read_task(location) != prior:
                    raise MigrationRefused('native_task_binding_changed')
                bind_task(location, facts.header.run_id, destination)
    return Kernel(destination)


def rollback_original_run_v6(snapshot: SourceSnapshot, *, repository: str, parent_number: int,
        nonce: str, external_snapshot: Path) -> dict[str, str]:
    managed = ManagedWorkspace.for_repository(repository)
    task = TaskKey(managed.repository_root, repository, parent_number)
    with runner_management_lease(default_runner_lock_path()), legacy_task_lock(task, managed.state_root), locked_task(task) as location:
        managed.open_path()
        with tempfile.TemporaryDirectory(dir=managed.state_root, prefix='.whole-inverse-') as directory:
            checked = capture_v6(snapshot.path, Path(directory) / 'sealed.sqlite3')
            if (checked.sha256, checked.schema_sha256, checked.table_rows) != (snapshot.sha256, snapshot.schema_sha256, snapshot.table_rows):
                raise MigrationRefused('snapshot_changed')
            accepted = SourceSnapshot(snapshot.source, checked.path, checked.sha256, checked.source_protocol, checked.schema_sha256, checked.table_rows)
            facts = read_original_run(accepted, external_snapshot.parent / 'profile.json')
            if facts.header.task != task or snapshot.source != managed.state_root / 'state.sqlite3':
                raise MigrationRefused('source_original_identity')
            origin = _origin(accepted, facts, managed, nonce)
            destination = Path(origin['target_db'])
            if destination.is_symlink() or not destination.is_file():
                raise MigrationRefused('native_original_cutover_missing')
            proof, proof_sha = _read_document(external_snapshot)
            sealed = _read_document(external_snapshot.parent / 'checkouts.json')[0]
            run_head = proof.get('remote_heads', {}).get('refs/heads/' + facts.header.run_branch)
            if not isinstance(run_head, str):
                raise MigrationRefused('source_external_refs')
            resources = run_resource_proofs(facts, origin, sealed)
            native = Kernel(destination)
            prepared_original_run(native, facts, origin, proof, proof_sha, resources, run_head)
            verify_run_external(facts, proof, managed.repository_root)
            authenticate_run_resources(accepted, facts, origin, external_snapshot.parent, sealed, run_head, detached=True)
            with native.storage.transaction(write=False) as tx:
                anchor = tx.one("SELECT subject FROM history WHERE kind='v6_run_import'")[0]
                started = tx.rows("SELECT subject,identity,material_id FROM history WHERE kind='v6_original_rollback_started'")
                if started and (len(started) != 1 or started[0]['identity'] != nonce
                        or tx.get(started[0]['material_id'], started[0]['subject']) != wire_json(origin)):
                    raise MigrationRefused('native_original_cutover_provenance')
            protocol = _original_protocol(snapshot.source)
            prior = read_task(location)
            if (protocol != source_fence(origin) and not (started and protocol == _OLD_PROTOCOL)
                    or prior not in ((facts.header.run_id, destination), None) or prior is None and not started):
                raise MigrationRefused('source_original_fence_changed')
            with ExitStack() as refs:
                for ref, sha in sorted(_refs(facts, resources, run_head).items()):
                    refs.enter_context(_hold_ref(LocalGit(managed.repository_root), managed.repository_root, ref, sha))
                authenticate_run_resources(accepted, facts, origin, external_snapshot.parent, sealed, run_head, detached=True)
                _retire_original(snapshot.source, checked.path, origin, retired=protocol != _OLD_PROTOCOL, release=protocol == _OLD_PROTOCOL)
                if not started:
                    with native.storage.transaction() as tx:
                        material = tx.put(anchor, 'v6_original_rollback', wire_json(origin))
                        tx.execute('INSERT INTO history(subject,kind,identity,material_id,generation,created_at) VALUES (?,?,?,?,1,?)',
                            (anchor, 'v6_original_rollback_started', nonce, material, native.clock()))
                for subject in sorted(resources):
                    for resource in resources[subject]:
                        restore_layout(resource.layout)
                authenticate_run_resources(accepted, facts, origin, external_snapshot.parent, sealed, run_head)
                _withdraw_original_pointer(location, facts.header.run_id, destination)
                _retire_original(snapshot.source, checked.path, origin, retired=protocol != _OLD_PROTOCOL, release=True)
                with native.storage.transaction() as tx:
                    if not tx.rows("SELECT 1 FROM history WHERE kind='v6_original_rollback_complete'"):
                        material = tx.put(anchor, 'v6_original_rollback', wire_json(origin))
                        tx.execute('INSERT INTO history(subject,kind,identity,material_id,generation,created_at) VALUES (?,?,?,?,1,?)',
                            (anchor, 'v6_original_rollback_complete', nonce, material, native.clock()))
    return {'status': 'rolled_back', 'nonce': nonce, 'source_database': str(snapshot.source),
            'retained_native_database': str(destination), 'next_action': 'original_continuation'}
