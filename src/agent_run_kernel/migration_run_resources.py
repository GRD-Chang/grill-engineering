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
from .migration import MigrationRefused, SourceSnapshot
from .migration_checkout import _verify_checkout_archive, seal_checkout
from .migration_conversion import _git, _one
from .migration_layout import verify_layout
from .migration_run import OriginalRunFacts, UnallocatedTicket, WorkedTicket
from .migration_run_proof import ResourceProof


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
