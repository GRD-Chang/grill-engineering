from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from agent_run import github_publish
from agent_run.git import GitRepository
from agent_run.github_publish import GhGitHubPublisher


@pytest.mark.parametrize(
    'message',
    [
        'feat: empty body',
        'feat: paragraphs\n\n\nleading blank line\n\n',
        'feat: 中文 "标题"\n\n第一段。\n\n第二段。\n  保留缩进和空格  ',
    ],
)
@pytest.mark.parametrize('lost_response', [False, True])
def test_squash_preserves_complete_message_through_git_and_github_readback(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, message: str, lost_response: bool
) -> None:
    """Exercise the real Publisher adapter, replacing only external gh transport."""
    git = GitRepository(git_repo)
    base = git.resolve('main')
    tree = git.resolve('main^{tree}')
    integrated: str | None = None

    def write(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal integrated
        assert command[:3] == ['gh', 'pr', 'merge']
        subject = command[command.index('--subject') + 1]
        # gh uses a default PR body if --body is absent, even for an empty body.
        body = command[command.index('--body') + 1] if '--body' in command else 'DEFAULT PR BODY'
        sent_message = subject + ('\n\n' + body if body else '')
        created = subprocess.run(
            ['git', 'commit-tree', tree, '-p', base, '-m', sent_message], cwd=git_repo,
            text=True, capture_output=True, check=True,
        )
        integrated = created.stdout.strip()
        return subprocess.CompletedProcess(command, int(lost_response), '', 'response lost' if lost_response else '')

    def read(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert integrated is not None
        if command[:3] == ['gh', 'pr', 'view']:
            payload: object = {
                'headRefName': 'agent-run/run-test/ticket-2', 'headRefOid': base,
                'headRepository': {'nameWithOwner': 'example/project'},
                'baseRefName': 'agent-run/run-test/run', 'baseRefOid': base,
                'state': 'MERGED', 'mergeCommit': {'oid': integrated},
            }
        else:
            assert command[:2] == ['gh', 'api']
            sha = command[2].rsplit('/', 1)[-1]
            raw = subprocess.run(
                ['git', 'cat-file', 'commit', sha], cwd=git_repo,
                text=True, capture_output=True, check=True,
            ).stdout
            payload = {'tree': {'sha': tree}, 'message': raw.split('\n\n', 1)[1], 'parents': [{'sha': base}]}
        return subprocess.CompletedProcess(command, 0, json.dumps(payload), '')

    monkeypatch.setattr(github_publish, 'run_write_command', write)
    monkeypatch.setattr(github_publish, 'run_read_command', read)
    publisher = GhGitHubPublisher('example/project', git)
    result = publisher.squash_merge(
        pr_number=12, expected_head_sha=base, run_branch='agent-run/run-test/run', commit_message=message,
    )
    assert result == integrated
    actual = subprocess.run(
        ['git', 'cat-file', 'commit', result], cwd=git_repo,
        text=True, capture_output=True, check=True,
    ).stdout.split('\n\n', 1)[1]
    expected = message if message.endswith('\n') else message + '\n'
    assert actual == expected
    assert publisher.live_pull_request(12)['integrated_message'] == expected


@pytest.mark.parametrize(
    ('actual', 'expected', 'matches'),
    [
        ('title\n\nbody', 'title\n\nbody', True),
        ('title\n\nbody\n', 'title\n\nbody', True),
        ('title\n\nbody\n\n', 'title\n\nbody', False),
        ('title\n\nbody\n\n', 'title\n\nbody\n', False),
        ('title\n\nbody\n', 'title\n\nbody\n\n', False),
        ('title\n\nbody\n\n', 'title\n\nbody\n\n', True),
        ('title\n\nbody\n\nbody\n', 'title\n\nbody', False),
        ('title\n', 'title\n\nbody', False),
        ('title\n\nbody\n', 'title\n\nbody ', False),
        ('title\n\nbody\n', 'title\n\n body', False),
    ],
)
def test_complete_message_contract_preserves_body_and_explicit_blank_lines(
    actual: str, expected: str, matches: bool,
) -> None:
    from agent_run.commit_messages import commit_messages_match

    assert commit_messages_match(actual, expected) is matches


@pytest.mark.parametrize('original', ['feat: message\n\nbody  ', 'feat: message\n\nbody\n\n'])
def test_publication_reuse_does_not_hide_body_whitespace_changes(
    git_repo: Path, tmp_path: Path, original: str,
) -> None:
    git = GitRepository(git_repo)
    base = git.resolve('main')
    checkout = tmp_path / 'ticket-2'
    git.prepare_ticket_checkout(branch='agent-run/test/ticket-2', base_sha=base, checkout=checkout)
    first = git.create_publication_commit(checkout, candidate_sha=base, base_sha=base, message=original)
    expected = 'feat: message\n\nbody'
    second = git.create_publication_commit(checkout, candidate_sha=first, base_sha=base, message=expected)
    assert second != first
    raw = subprocess.run(
        ['git', 'cat-file', 'commit', second], cwd=git_repo,
        text=True, capture_output=True, check=True,
    ).stdout
    assert raw.split('\n\n', 1)[1] == expected + '\n'
