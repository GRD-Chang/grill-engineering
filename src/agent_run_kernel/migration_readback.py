"""Read original external close evidence; the mapper alone authorizes import.

No close/comment mutation, old state interpretation or ownership inference is
performed here. Original response text is retained beside its parsed value.
"""
from __future__ import annotations

import json
from typing import Any

from agent_run.github import GhGitHubReader, GitHubReadError
from agent_run.github_identity import validate_identity_snapshot
from agent_run.github_retry import run_read_command

from .model import wire_json
from .storage import MATERIAL_LIMIT


def read_issue_close_facts(reader: GhGitHubReader, ticket_number: int) -> dict[str, Any]:
    """Keep actor, original Issue and all comment/event pages, including IDs.

    A read failure remains unknown. No empty response, current CLOSED state or
    actor match is converted into proof that this Run owned an external write.
    """
    if type(ticket_number) is not int or ticket_number < 1:
        raise ValueError('Ticket Issue number must be a positive integer')
    identity = validate_identity_snapshot(reader.runner_identity)
    repository = reader.repository().name_with_owner
    base = f'repos/{repository}/issues/{ticket_number}'
    responses: list[dict[str, Any]] = []
    values: list[Any] = []
    for arguments in (['api', 'user'], ['api', base],
                      ['api', base + '/comments', '--paginate', '--slurp'],
                      ['api', base + '/events', '--paginate', '--slurp']):
        response = run_read_command(['gh', *arguments], cwd=reader.working_directory,
                                    runner_identity=identity)
        if response.returncode:
            raise GitHubReadError('github_read_failed', response.stderr.strip() or 'Close evidence read failed')
        try:
            values.append(json.loads(response.stdout))
        except (ValueError, UnicodeError) as error:
            raise GitHubReadError('github_invalid_response', 'Close evidence is not JSON') from error
        responses.append({'arguments': arguments, 'stdout': response.stdout})
    actor, issue, comments, events = values
    if (not isinstance(actor, dict) or not isinstance(actor.get('login'), str) or not actor['login']
            or not isinstance(issue, dict) or type(issue.get('number')) is not int
            or issue['number'] != ticket_number or issue.get('state') not in {'open', 'closed'}):
        raise GitHubReadError('github_invalid_response', 'Close evidence has another Issue or actor identity')
    for pages in (comments, events):
        if (not isinstance(pages, list)
                or any(not isinstance(page, list) or any(not isinstance(row, dict) for row in page)
                       for page in pages)):
            raise GitHubReadError('github_invalid_response', 'Close evidence has incomplete pagination')
    result = {'repository': repository, 'issue_number': ticket_number,
              'actor': actor, 'issue': issue, 'comment_pages': comments,
              'event_pages': events, 'responses': responses}
    if len(wire_json(result)) > MATERIAL_LIMIT:
        raise GitHubReadError('github_read_limit', 'Close evidence exceeds the material limit')
    return result
