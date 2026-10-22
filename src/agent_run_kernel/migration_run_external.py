"""Read and authenticate external facts for one original whole Run."""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any, Mapping

from agent_run.github import GhGitHubReader

from .effects import LocalGit
from .migration import MigrationRefused
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


def prove_run_head(facts: OriginalRunFacts, repository_root: Path, run_head: str) -> None:
    completed = [ticket for ticket in facts.tickets if isinstance(ticket, WorkedTicket) and ticket.kind == 'completed']
    proved_heads = {facts.header.default_sha, *(ticket.job['value_integrated_sha'] for ticket in completed)}
    reader = LocalGit(repository_root)
    if run_head not in proved_heads or any(reader.run('merge-base', '--is-ancestor',
            ticket.job['value_integrated_sha'], run_head, check=False).returncode != 0 for ticket in completed):
        raise MigrationRefused('source_run_head_unproven')


def verify_run_external(facts: OriginalRunFacts, proof: Mapping[str, Any], repository_root: Path
                        ) -> tuple[str, dict[str, Mapping[str, Any]]]:
    header = facts.header
    run_ref, default_ref = 'refs/heads/' + header.run_branch, 'refs/heads/' + header.default_branch
    worked = [ticket for ticket in facts.tickets if isinstance(ticket, WorkedTicket)]
    completed = [ticket for ticket in worked if ticket.kind == 'completed']
    heads = proof.get('remote_heads')
    subjects = proof.get('subjects')
    if (set(proof) != {'source_kind', 'source_sha256', 'runner_identity', 'requirements', 'remote_heads', 'subjects'}
            or proof.get('source_kind') != 'original_run' or proof.get('source_sha256') != facts.source_sha256
            or proof.get('runner_identity') != header.inputs.runner_identity
            or proof.get('requirements') != requirements_document(header.requirements)
            or not isinstance(heads, dict) or set(heads) != {run_ref, default_ref, *(ticket.source_ref for ticket in worked)}
            or not isinstance(subjects, dict) or set(subjects) != {ticket.owner.subject for ticket in completed}):
        raise MigrationRefused('source_run_external')
    reader = LocalGit(repository_root)
    run_head, default_head = heads[run_ref], heads[default_ref]
    proved_heads = {header.default_sha, *(ticket.job['value_integrated_sha'] for ticket in completed)}
    if (not isinstance(run_head, str) or run_head not in proved_heads
            or not isinstance(default_head, str)
            or reader.run('merge-base', '--is-ancestor', header.default_sha, default_head, check=False).returncode != 0
            or reader.run('symbolic-ref', '-q', run_ref, check=False).returncode != 1
            or _git(repository_root, 'rev-parse', '--verify', run_ref) != run_head):
        raise MigrationRefused('source_external_refs')
    result: dict[str, Mapping[str, Any]] = {}
    prove_run_head(facts, repository_root, run_head)
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
