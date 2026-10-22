"""Authenticate the complete original resource set, without creating resources."""
from __future__ import annotations

from contextlib import closing
import hashlib
import os
from pathlib import Path
import sqlite3
from typing import Any, Mapping

from agent_run.managed_workspace import ManagedWorkspace

from .effects import LocalGit
from .migration import MigrationRefused, SourceSnapshot, _require_no_v6_notification_journal
from .migration_checkout import _verify_checkout_archive, seal_checkout
from .migration_conversion import _git, _one
from .migration_layout import verify_layout
from .migration_run import OriginalRunFacts, UnallocatedTicket, WorkedTicket
from .migration_run_proof import ResourceProof
from .migration_facts import ImportedRunFacts
from .migration_run_proof import imported_resource_layout
from .model import DeliveryKind


def authenticate_imported_resources(facts: ImportedRunFacts, origin: Mapping[str, str], evidence: Path,
        *, detached: bool = False) -> None:
    """Authenticate the explicit original-clone resource set, not a role class.

    Copied transport has its own private-root/new-identity checks and cannot
    enter the original Task retirement executor through this function.
    """
    spec, snapshot = facts.spec, facts.source.snapshot
    _require_no_v6_notification_journal(snapshot, spec.run_id)
    if facts.source.mode != 'original' or facts.source.task is None or facts.source.checkout_uuid is None:
        raise MigrationRefused('source_existing_identity')
    managed = ManagedWorkspace.for_repository(spec.repository)
    root, common = managed.repository_root, managed.repository_root / '.git'
    if (spec.repository_root != str(root) or facts.source.task.workspace != root or managed.open_path() != root
            or snapshot.source != managed.state_root / 'state.sqlite3'
            or origin['target_db'] != str(managed.state_root / spec.run_id / 'kernel.sqlite3')
            or origin['source_db'] != str(snapshot.source) or origin['run_id'] != spec.run_id
            or origin['source_sha256'] != snapshot.sha256 or origin['source_uuid'] != facts.source.checkout_uuid
            or origin['task_fingerprint'] != facts.source.task.fingerprint
            or evidence.resolve() != evidence or evidence.is_relative_to(managed.root)):
        raise MigrationRefused('source_existing_resources')
    with closing(sqlite3.connect(snapshot.path.as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
        db.row_factory = sqlite3.Row
        uuid = _one(db, 'SELECT value_checkout_identity FROM run_root WHERE run_id=?', (spec.run_id,))[0]
        state = _one(db, 'SELECT run_state_dir FROM control_tasks')[0]
        if uuid != facts.source.checkout_uuid or state != str(managed.state_root):
            raise MigrationRefused('source_existing_identity')
    marker = common / 'agent-run-checkout-id'
    if (marker.is_symlink() or not marker.is_file() or marker.stat().st_nlink != 1
            or marker.read_bytes() != (facts.source.checkout_uuid + '\n').encode('ascii')):
        raise MigrationRefused('source_existing_identity')
    final = next(delivery for delivery in spec.deliveries if delivery.kind == DeliveryKind.FINAL_RUN)
    reader = LocalGit(root)
    run_ref = facts.run_resource
    if run_ref.kind != 'run-ref' or run_ref.disposition != 'existing' or run_ref.expected_oid is None:
        raise MigrationRefused('source_run_head_unproven')
    for ref, sha in ((run_ref.source_ref, run_ref.expected_oid), (final.target_ref, final.base.sha)):
        if reader.run('symbolic-ref', '-q', ref, check=False).returncode != 1 or _git(root, 'rev-parse', '--verify', ref) != sha:
            raise MigrationRefused('source_existing_ref')
    if _git(root, 'rev-parse', 'HEAD') != final.base.sha or _git(root, 'status', '--porcelain'):
        raise MigrationRefused('source_existing_checkout')
    expected_paths = {str(root)}
    seen_refs = {run_ref.source_ref, final.target_ref}
    deliveries = {value.key: value for value in spec.deliveries if value.kind == DeliveryKind.TICKET}
    for subject in facts.subjects:
        if len(subject.resources) != 1:
            raise MigrationRefused('source_resource_union')
        resource = subject.resources[0]
        checkout = managed.state_root / 'worktrees' / spec.run_id / f'ticket-{subject.issue}'
        delivery = deliveries[subject.subject]
        if (resource.kind != 'ticket-workspace' or resource.owner != subject.record
                or resource.source_ref != delivery.source_ref or resource.source_ref in seen_refs
                or resource.common_git_dir != common):
            raise MigrationRefused('source_resource_union')
        seen_refs.add(resource.source_ref)
        if resource.disposition == 'absent':
            if (any(value is not None for value in (resource.expected_oid, resource.checkout,
                    resource.workspace_oid, resource.owned_root, resource.before_head,
                    resource.checkout_archive_sha256, resource.checkout_index_sha256))
                    or resource.cleanup_pending or checkout.exists() or checkout.is_symlink()
                    or _git(root, 'for-each-ref', '--format=%(refname)', resource.source_ref)):
                raise MigrationRefused('source_resource_absence')
        elif resource.disposition == 'existing':
            if (resource.checkout != checkout or resource.owned_root != checkout.parent
                    or resource.expected_oid is None or reader.run('symbolic-ref', '-q', resource.source_ref, check=False).returncode != 1
                    or _git(root, 'rev-parse', '--verify', resource.source_ref) != resource.expected_oid):
                raise MigrationRefused('source_existing_ref')
            layout = imported_resource_layout(resource, origin)
            verify_layout(layout, state='either' if detached else 'attached')
            folder = evidence / ('ticket-' + str(subject.issue)) if facts.source.scope == 'whole-run' else evidence
            _verify_checkout_archive(folder / 'checkout.tar', checkout, resource.checkout_archive_sha256 or '')
            index = Path(_git(checkout, 'rev-parse', '--path-format=absolute', '--git-path', 'index'))
            saved = folder / 'checkout-index'
            if (index.is_symlink() or index.resolve() != index or not index.is_relative_to(common / 'worktrees')
                    or not index.is_file() or index.stat().st_nlink != 1 or index.stat().st_size > 32 * 1024 * 1024
                    or saved.is_symlink() or not saved.is_file() or saved.stat().st_nlink != 1
                    or saved.stat().st_size > 32 * 1024 * 1024
                    or hashlib.sha256(saved.read_bytes()).hexdigest() != resource.checkout_index_sha256
                    or saved.read_bytes() != index.read_bytes() or _git(checkout, 'diff', '--cached', '--name-only')):
                raise MigrationRefused('source_checkout_index')
            expected_paths.add(str(checkout))
        else:
            raise MigrationRefused('source_existing_resources')
        commits = [subject.historical_base, subject.candidate]
        if subject.completion is not None:
            commits.extend((subject.completion.publication, subject.completion.integrated))
        for commit in commits:
            if commit is not None and (_git(root, 'cat-file', '-t', commit.sha) != 'commit'
                    or _git(root, 'rev-parse', commit.sha + '^{tree}') != commit.tree):
                raise MigrationRefused('source_git_object')
        if subject.historical_base is not None:
            children = [subject.candidate] if subject.completion is None else [subject.completion.publication, subject.completion.integrated]
            if any(commit is not None and _git(root, 'show', '-s', '--format=%P', commit.sha) != subject.historical_base.sha
                   for commit in children):
                raise MigrationRefused('source_git_ancestry')
    paths = _git(root, 'worktree', 'list', '--porcelain').strip().split('\n\n')
    if {block.splitlines()[0].removeprefix('worktree ') for block in paths} != expected_paths or len(paths) != len(expected_paths):
        raise MigrationRefused('source_existing_worktrees')


def seal_run_checkouts(facts: OriginalRunFacts, evidence: Path) -> dict[str, dict[str, str]]:
    managed = ManagedWorkspace.for_repository(facts.header.repository)
    sealed = {}
    for ticket in facts.tickets:
        if isinstance(ticket, WorkedTicket) and ticket.kind == 'returned':
            folder = evidence / ('ticket-' + str(ticket.owner.ticket))
            folder.mkdir(mode=0o700)
            checkout = managed.state_root / 'worktrees' / facts.header.run_id / folder.name
            archive_sha, index_sha = seal_checkout(checkout, managed.repository_root, folder / 'checkout.tar')
            descriptor = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            sealed[ticket.owner.subject] = {'checkout_archive_sha256': archive_sha, 'checkout_index_sha256': index_sha}
    return sealed


def run_resource_proofs(facts: OriginalRunFacts, origin: Mapping[str, str],
                        sealed: Mapping[str, Any]) -> dict[str, tuple[ResourceProof, ...]]:
    """Reconstruct saved intent only; this does not inspect or grant resources."""
    header = facts.header
    managed = ManagedWorkspace.for_repository(header.repository)
    returned = {ticket.owner.subject for ticket in facts.tickets if isinstance(ticket, WorkedTicket) and ticket.kind == 'returned'}
    if set(sealed) != returned:
        raise MigrationRefused('source_resource_union')
    result: dict[str, tuple[ResourceProof, ...]] = {}
    for ticket in facts.tickets:
        subject = ticket.owner.subject
        if isinstance(ticket, UnallocatedTicket) or ticket.kind == 'completed':
            result[subject] = ()
            continue
        data = sealed[subject]
        if not isinstance(data, dict) or set(data) != {'checkout_archive_sha256', 'checkout_index_sha256'}:
            raise MigrationRefused('source_checkout_archive')
        checkout = managed.state_root / 'worktrees' / header.run_id / f'ticket-{ticket.owner.ticket}'
        layout = {'version': 1, 'nonce': origin['nonce'], 'source_sha256': origin['source_sha256'],
            'source_uuid': origin['source_uuid'], 'repository_root': str(managed.repository_root), 'checkout': str(checkout),
            'owned_root': str(checkout.parent), 'common_git_dir': str(managed.repository_root / '.git'),
            'source_ref': ticket.source_ref, 'expected_oid': ticket.candidate_sha or ticket.base_sha,
            'before_head': 'ref: ' + ticket.source_ref + '\n', 'checkout_archive_sha256': data['checkout_archive_sha256']}
        assert ticket.owner.job is not None
        result[subject] = (ResourceProof(layout, data['checkout_index_sha256'], ticket.owner.job),)
    return result


def authenticate_run_resources(snapshot: SourceSnapshot, facts: OriginalRunFacts,
        origin: Mapping[str, str], evidence: Path, sealed: Mapping[str, Any], run_head: str,
        *, detached: bool = False) -> dict[str, tuple[ResourceProof, ...]]:
    header = facts.header
    managed = ManagedWorkspace.for_repository(header.repository)
    root, common = managed.repository_root, managed.repository_root / '.git'
    if (snapshot.source != managed.state_root / 'state.sqlite3' or header.task.workspace != root
            or managed.open_path() != root or origin['target_db'] != str(managed.state_root / header.run_id / 'kernel.sqlite3')
            or origin['source_sha256'] != facts.source_sha256 or origin['task_fingerprint'] != header.task.fingerprint
            or evidence.resolve() != evidence or evidence.is_relative_to(managed.root)):
        raise MigrationRefused('source_existing_resources')
    with closing(sqlite3.connect(snapshot.path.as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
        db.row_factory = sqlite3.Row
        uuid = _one(db, 'SELECT value_checkout_identity FROM run_root WHERE run_id=?', (header.run_id,))[0]
        state = _one(db, 'SELECT run_state_dir FROM control_tasks')[0]
        if origin['source_uuid'] != uuid or state != str(managed.state_root):
            raise MigrationRefused('source_existing_identity')
    marker = common / 'agent-run-checkout-id'
    if (marker.is_symlink() or not marker.is_file() or marker.stat().st_nlink != 1
            or marker.read_bytes() != (origin['source_uuid'] + '\n').encode('ascii')):
        raise MigrationRefused('source_existing_identity')
    reader = LocalGit(root)
    for ref, sha in (('refs/heads/' + header.run_branch, run_head),
                     ('refs/heads/' + header.default_branch, header.default_sha)):
        if (reader.run('symbolic-ref', '-q', ref, check=False).returncode != 1
                or _git(root, 'rev-parse', '--verify', ref) != sha):
            raise MigrationRefused('source_existing_ref')
    if (_git(root, 'rev-parse', 'HEAD') != header.default_sha or _git(root, 'status', '--porcelain')):
        raise MigrationRefused('source_existing_checkout')
    expected_paths = {str(root)}
    result = run_resource_proofs(facts, origin, sealed)
    for ticket in facts.tickets:
        subject = ticket.owner.subject
        checkout = managed.state_root / 'worktrees' / header.run_id / f'ticket-{ticket.owner.ticket}'
        ref = f'refs/heads/agent-run/{header.run_id}/ticket-{ticket.owner.ticket}'
        if isinstance(ticket, UnallocatedTicket) or ticket.kind == 'completed':
            if (checkout.exists() or checkout.is_symlink() or _git(root, 'for-each-ref', '--format=%(refname)', ref)):
                raise MigrationRefused('source_resource_absence')
            if isinstance(ticket, WorkedTicket):
                for commit_sha, tree in ((ticket.candidate_sha, ticket.candidate_tree),
                                  (ticket.job['value_publication_sha'], ticket.candidate_tree),
                                  (ticket.job['value_integrated_sha'], ticket.candidate_tree)):
                    if (not isinstance(commit_sha, str) or _git(root, 'cat-file', '-t', commit_sha) != 'commit'
                            or _git(root, 'rev-parse', commit_sha + '^{tree}') != tree):
                        raise MigrationRefused('source_git_object')
                if any(_git(root, 'show', '-s', '--format=%P', sha) != ticket.base_sha
                       for sha in (ticket.job['value_publication_sha'], ticket.job['value_integrated_sha'])):
                    raise MigrationRefused('source_git_ancestry')
            continue
        resource = result[subject][0]
        layout = resource.layout
        verify_layout(layout, state='either' if detached else 'attached')
        folder = evidence / ('ticket-' + str(ticket.owner.ticket))
        _verify_checkout_archive(folder / 'checkout.tar', checkout, layout['checkout_archive_sha256'])
        index = Path(_git(checkout, 'rev-parse', '--path-format=absolute', '--git-path', 'index'))
        saved = folder / 'checkout-index'
        if (index.is_symlink() or index.resolve() != index or not index.is_relative_to(common / 'worktrees')
                or not index.is_file() or index.stat().st_nlink != 1 or index.stat().st_size > 32 * 1024 * 1024
                or saved.is_symlink() or not saved.is_file() or saved.stat().st_nlink != 1
                or saved.stat().st_size > 32 * 1024 * 1024
                or hashlib.sha256(saved.read_bytes()).hexdigest() != resource.checkout_index_sha256
                or saved.read_bytes() != index.read_bytes() or _git(checkout, 'diff', '--cached', '--name-only')):
            raise MigrationRefused('source_checkout_index')
        if ticket.candidate_sha is not None and (
                _git(root, 'rev-parse', ticket.candidate_sha + '^{tree}') != ticket.candidate_tree
                or _git(root, 'show', '-s', '--format=%P', ticket.candidate_sha) != ticket.base_sha):
            raise MigrationRefused('source_git_ancestry')
        expected_paths.add(str(checkout))
    paths = _git(root, 'worktree', 'list', '--porcelain').strip().split('\n\n')
    if (len(paths) != len(expected_paths)
            or {block.splitlines()[0].removeprefix('worktree ') for block in paths} != expected_paths):
        raise MigrationRefused('source_existing_worktrees')
    return result
