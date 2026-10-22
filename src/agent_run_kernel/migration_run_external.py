"""Read and authenticate external facts for one original whole Run."""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any, Mapping

from agent_run.github import GhGitHubReader

from .effects import LocalGit
from .migration import MigrationRefused
from .migration_run_facts import RunHeader
from .migration_facts import ImportedSubject
from .migration_completed import _external_close_proof
from .migration_conversion import _git
from .migration_run import OriginalRunFacts, WorkedTicket
from .source_reading import read_pull_request_facts, read_remote_heads, read_requirements
from .migration_readback import read_issue_close_facts
from .model import Requirements, wire_json


def requirements_document(requirements: Requirements) -> dict[str, Any]:
    import json
    return dict(json.loads(wire_json({'parent': asdict(requirements.parent), 'issues': [
        {**asdict(issue), 'labels': sorted(issue.labels)} for issue in sorted(requirements.issues, key=lambda item: item.number)]})))


def collect_run_external(facts: OriginalRunFacts, repository_root: Path) -> dict[str, Any]:
    """Collect complete raw command responses without performing writes."""
    header = facts.header
    runner = header.inputs.runner_identity
    reader = GhGitHubReader(header.repository, working_directory=repository_root, runner_identity=runner)
    requirements = read_requirements(reader, header.parent_number)
    run_ref, default_ref = 'refs/heads/' + header.run_branch, 'refs/heads/' + header.default_branch
    run_head, default_head = read_remote_heads(repository_root, run_ref, default_ref, runner_identity=runner)
    heads: dict[str, str | None] = {run_ref: run_head, default_ref: default_head}
    subjects = {}
    for ticket in facts.tickets:
        if not isinstance(ticket, WorkedTicket):
            continue
        source_head, repeated_run = read_remote_heads(repository_root, ticket.source_ref, run_ref, runner_identity=runner)
        if repeated_run != run_head:
            raise MigrationRefused('source_external_refs')
        heads[ticket.source_ref] = source_head
        if ticket.kind == 'completed':
            number = ticket.job['value_pr_number']
            before = read_pull_request_facts(reader, number)
            close = read_issue_close_facts(reader, ticket.owner.ticket)
            after = read_pull_request_facts(reader, number)
            subjects[ticket.owner.subject] = {'pr_before': before, 'close': close, 'pr_after': after}
    return {'source_kind': 'original_run', 'source_sha256': facts.source_sha256,
            'runner_identity': dict(runner), 'requirements': requirements_document(requirements),
            'remote_heads': heads, 'subjects': subjects}


def _prove_run_head(default_sha: str, completed_heads: set[str], repository_root: Path, run_head: str) -> None:
    reader = LocalGit(repository_root)
    if run_head not in {default_sha, *completed_heads} or any(reader.run('merge-base', '--is-ancestor',
            sha, run_head, check=False).returncode != 0 for sha in completed_heads):
        raise MigrationRefused('source_run_head_unproven')


def prove_run_head(facts: OriginalRunFacts, repository_root: Path, run_head: str) -> None:
    _prove_run_head(facts.header.default_sha, {ticket.job['value_integrated_sha'] for ticket in facts.tickets
        if isinstance(ticket, WorkedTicket) and ticket.kind == 'completed'}, repository_root, run_head)


def verify_run_readback_header(header: RunHeader, source_sha: str, proof: Mapping[str, Any],
        repository_root: Path, *, worked_refs: set[str], completed_subjects: set[str], completed_heads: set[str], verify_current: bool = True) -> str:
    """Shared exact external scope/ref check; no legacy writer facts required."""
    run_ref, default_ref = 'refs/heads/' + header.run_branch, 'refs/heads/' + header.default_branch
    heads, subjects = proof.get('remote_heads'), proof.get('subjects')
    if (set(proof) != {'source_kind', 'source_sha256', 'runner_identity', 'requirements', 'remote_heads', 'subjects'}
            or proof.get('source_kind') != 'original_run' or proof.get('source_sha256') != source_sha
            or proof.get('runner_identity') != header.inputs.runner_identity
            or proof.get('requirements') != requirements_document(header.requirements)
            or not isinstance(heads, dict) or set(heads) != {run_ref, default_ref, *worked_refs}
            or not isinstance(subjects, dict) or set(subjects) != completed_subjects):
        raise MigrationRefused('source_run_external')
    reader = LocalGit(repository_root)
    run_head, default_head = heads[run_ref], heads[default_ref]
    if (not isinstance(run_head, str) or run_head not in {header.default_sha, *completed_heads}
            or not isinstance(default_head, str)
            or reader.run('merge-base', '--is-ancestor', header.default_sha, default_head, check=False).returncode != 0
            or (verify_current and (reader.run('symbolic-ref', '-q', run_ref, check=False).returncode != 1
                or _git(repository_root, 'rev-parse', '--verify', run_ref) != run_head))):
        raise MigrationRefused('source_external_refs')
    _prove_run_head(header.default_sha, completed_heads, repository_root, run_head)
    return run_head


def verify_run_external(facts: OriginalRunFacts, proof: Mapping[str, Any], repository_root: Path
                        ) -> tuple[str, dict[str, Mapping[str, Any]]]:
    header = facts.header
    run_ref, default_ref = 'refs/heads/' + header.run_branch, 'refs/heads/' + header.default_branch
    worked = [ticket for ticket in facts.tickets if isinstance(ticket, WorkedTicket)]
    completed = [ticket for ticket in worked if ticket.kind == 'completed']
    run_head = verify_run_readback_header(header, facts.source_sha256, proof, repository_root,
        worked_refs={ticket.source_ref for ticket in worked}, completed_subjects={ticket.owner.subject for ticket in completed},
        completed_heads={ticket.job['value_integrated_sha'] for ticket in completed})
    heads, subjects = proof['remote_heads'], proof['subjects']
    default_head = heads[default_ref]
    reader = LocalGit(repository_root)
    result: dict[str, Mapping[str, Any]] = {}
    for ticket in worked:
        if ticket.kind == 'returned':
            if heads[ticket.source_ref] != ticket.base_sha:
                raise MigrationRefused('source_external_refs')
            continue
        integrated = ticket.job['value_integrated_sha']
        if reader.run('merge-base', '--is-ancestor', integrated, run_head, check=False).returncode != 0:
            raise MigrationRefused('source_external_refs')
        entry = subjects[ticket.owner.subject]
        if not isinstance(entry, dict) or set(entry) != {'pr_before', 'close', 'pr_after'}:
            raise MigrationRefused('source_external_close')
        # The shared close validator binds one write's exact PR/actor/intent.
        # Current Run refs are authenticated above independently of that write's
        # historical merge base. Do not rewrite or discard the raw API response.
        close_input = {**entry, 'source_sha256': facts.source_sha256,
            'runner_identity': proof['runner_identity'], 'requirements': proof['requirements'],
            'remote_heads': {run_ref: run_head, default_ref: default_head,
                             ticket.source_ref: heads[ticket.source_ref]}}
        assert ticket.close_intent is not None
        result[ticket.owner.subject] = _external_close_proof(close_input,
            run_id=header.run_id, repository=header.repository, ticket=ticket.owner.ticket,
            pr_number=ticket.job['value_pr_number'], publication_sha=ticket.job['value_publication_sha'],
            integrated_sha=integrated, base_sha=ticket.base_sha, run_branch=header.run_branch,
            ticket_branch=ticket.job['value_ticket_branch'], base_branch=header.default_branch,
            source_sha=facts.source_sha256, intent=ticket.close_intent,
            requirements=header.requirements, runner_identity=header.inputs.runner_identity,
            proved_run_head=run_head)
    return run_head, result


def verify_imported_run_external(header: RunHeader, source_sha: str, subjects: tuple[ImportedSubject, ...],
        proof: Mapping[str, Any], repository_root: Path) -> tuple[str, dict[str, Mapping[str, Any]]]:
    """Use the same external contract from canonical source facts."""
    import json
    worked = [subject for subject in subjects if subject.kind != 'unallocated']
    completed = [subject for subject in worked if subject.completion is not None]
    run_head = verify_run_readback_header(header, source_sha, proof, repository_root,
        worked_refs={subject.resources[0].source_ref for subject in worked},
        completed_subjects={subject.subject for subject in completed},
        completed_heads={subject.completion.integrated.sha for subject in completed if subject.completion is not None},
        verify_current=False)
    heads = proof['remote_heads']
    run_ref, default_ref = 'refs/heads/' + header.run_branch, 'refs/heads/' + header.default_branch
    result: dict[str, Mapping[str, Any]] = {}
    for subject in worked:
        ref = subject.resources[0].source_ref
        assert subject.historical_base is not None
        if subject.kind == 'returned':
            if heads[ref] != subject.historical_base.sha:
                raise MigrationRefused('source_external_refs')
            continue
        completion = subject.completion
        assert completion is not None
        entry = proof['subjects'][subject.subject]
        if not isinstance(entry, dict) or set(entry) != {'pr_before', 'close', 'pr_after'}:
            raise MigrationRefused('source_external_close')
        result[subject.subject] = _external_close_proof(
            {**entry, 'source_sha256': source_sha, 'runner_identity': proof['runner_identity'],
             'requirements': proof['requirements'], 'remote_heads': {run_ref: run_head,
                default_ref: heads[default_ref], ref: heads[ref]}},
            run_id=header.run_id, repository=header.repository, ticket=subject.issue,
            pr_number=completion.pr_number, publication_sha=completion.publication.sha,
            integrated_sha=completion.integrated.sha, base_sha=subject.historical_base.sha,
            run_branch=header.run_branch, ticket_branch=ref.removeprefix('refs/heads/'),
            base_branch=header.default_branch, source_sha=source_sha, intent=json.loads(completion.close_intent),
            requirements=header.requirements, runner_identity=header.inputs.runner_identity, proved_run_head=run_head)
    return run_head, result
