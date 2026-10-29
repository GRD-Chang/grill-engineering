"""Literal binding of the existing gh fixture, not a new simulator or service."""
import ast
import json
import os
import subprocess
import sys

import pytest

from support.remote_effects import FAKE_GH, fake_gh, _fixture, _publish_pr
from agent_run.contracts.write_authority import WriteAuthority
from dataclasses import replace


def test_default_remote_fixture_program_is_byte_for_byte_unchanged():
    assert fake_gh() == FAKE_GH


def test_remote_fixture_generation_preserves_unrelated_text_and_identifiers(monkeypatch):
    from support import remote_effects
    note = "Documentation mentions publisher and example/project without binding authority."
    source = FAKE_GH + "\npublisher_note = " + repr(note) + "\n"
    monkeypatch.setattr(remote_effects, "FAKE_GH", source)
    generated = fake_gh(repository="publisher/project", actor="bounded-actor")
    tail = ast.parse(generated).body[-1]
    assert isinstance(tail, ast.Assign) and tail.targets[0].id == "publisher_note"
    assert tail.value.value == note


@pytest.mark.parametrize(("repository", "actor"), [
    ("publisher/project", "bounded-actor"),
    ("owner/publisher-helper", "publisher-bot"),
    ("example/project", "literal-example/project"),
    ("publisher/project\nquote'\"", "publisher'; raise AssertionError('injected') #"),
])
def test_remote_fixture_fields_do_not_rewrite_each_other_or_python(tmp_path, repository, actor):
    program = fake_gh(repository=repository, actor=actor)
    ast.parse(program)
    state, calls = tmp_path / "state.json", tmp_path / "calls.jsonl"
    state.write_text('{"prs":[],"comments":[],"events":[],"issue_state":"open"}')
    environment = {"HOME": str(tmp_path), "PATH": os.defpath,
        "REMOTE_TEST_STATE": str(state), "REMOTE_TEST_CALLS": str(calls)}
    # Only the generated script's repo/user read branches run; no Git, account,
    # credential provider, Core/Host or remote network boundary is entered.
    for arguments, key, expected in ((["repo", "view"], "nameWithOwner", repository),
                                      (["api", "user"], "login", actor)):
        completed = subprocess.run([sys.executable, "-I", "-c", program, *arguments],
            cwd=tmp_path, env=environment, text=True, capture_output=True, timeout=5)
        assert completed.returncode == 0, (completed.returncode, completed.stdout, completed.stderr)
        assert completed.stderr == "" and json.loads(completed.stdout)[key] == expected
    assert json.loads(state.read_text()) == {"prs":[],"comments":[],"events":[],"issue_state":"open"}


def test_remote_fixture_keeps_exact_repository_actor_and_publication_receipt(tmp_path, monkeypatch):
    repository, actor = "publisher/project", "bounded-actor"
    port, _git, push, state, calls = _fixture(tmp_path, monkeypatch, repository=repository, actor=actor)
    effect, publication = _publish_pr(port, push)
    assert publication.repository == publication.head_repository == repository
    assert publication.actor == actor and publication.number == 17
    assert publication.head.sha == push.after_sha
    assert publication.source_ref == push.source_ref and publication.target_ref == push.target_ref
    assert publication.title == "Original title" and publication.body == "Original narrative\r\n中文"
    document = json.loads(state.read_text())
    pr, = document["prs"]
    assert pr["head"]["repo"]["full_name"] == pr["base"]["repo"]["full_name"] == repository
    assert pr["user"]["login"] == actor
    actual = [json.loads(line) for line in calls.read_text().splitlines()]
    assert sum(call[1] == "repos/publisher/project/pulls" and "POST" in call for call in actual) == 1
    assert port.reconcile(effect).disposition == "target"
    comment = replace(push, effect_id='bound-comment', operation='completion_comment', after_sha=None,
        pr_number=publication.number, payload={'issue': 2, 'merge_sha': push.after_sha, 'kind': 'ticket'})
    receipt = port.execute(comment, WriteAuthority(lambda: lambda: None))
    assert receipt.disposition == 'target'
    stored, = json.loads(state.read_text())['comments']
    assert stored['user']['login'] == actor
    assert stored['issue_url'] == f'https://api.github.com/repos/{repository}/issues/2'
    assert stored['id'] == receipt.raw['close_intent']['comment_id']
    assert stored['body'] == receipt.raw['close_intent']['comment_body']


def test_local_failure_generated_source_preserves_dependency_in_both_projections(git_repo, tmp_path, monkeypatch):
    from agent_run.adapters.github import GhGitHubReader
    from agent_run.adapters.source import read_requirements
    from support.remote_effects import with_ticket_dependencies
    from support.systemd_manager import _CODEX, _control_environment

    environment, state = _control_environment(git_repo, tmp_path, monkeypatch, codex=_CODEX, tickets=(2, 3, 4, 5))
    document = json.loads(state.read_text())
    document['source_graph']['issues']['5']['blocked_by'] = [3]
    state.write_text(json.dumps(document))
    binary = tmp_path / 'control-bin/gh'
    binary.write_text(with_ticket_dependencies(binary.read_text()))
    reader = GhGitHubReader('example/project', working_directory=git_repo, runner_identity={'source': 'host'})
    before = state.read_bytes()
    source = read_requirements(reader, 1)
    dependent, = [issue for issue in source.issues if issue.number == 5]
    assert [(item.number, item.state) for item in dependent.blocked_by] == [(3, 'OPEN')]
    assert all(not issue.blocked_by for issue in source.issues if issue.number != 5)
    assert state.read_bytes() == before  # Source GETs do not rewrite service state.

    document['source_graph']['issues']['3']['state'] = 'CLOSED'
    state.write_text(json.dumps(document))
    source = read_requirements(reader, 1)
    dependent, = [issue for issue in source.issues if issue.number == 5]
    assert [(item.number, item.state) for item in dependent.blocked_by] == [(3, 'CLOSED')]
    query = ('query Ticket($owner:String!,$name:String!,$number:Int!) { '
        'repository(owner:$owner,name:$name) { issue(number:$number) { '
        'number blockedBy(first:100) { nodes { number state } } } } }')
    single = subprocess.run([str(binary), 'api', 'graphql', '-f', 'query=' + query,
        '-f', 'owner=example', '-f', 'name=project', '-F', 'number=5'],
        cwd=git_repo, env=environment, text=True, capture_output=True, timeout=5)
    assert single.returncode == 0, single.stderr
    nodes = json.loads(single.stdout)['data']['repository']['issue']['blockedBy']['nodes']
    assert [(item['number'], item['state']) for item in nodes] == [(3, 'CLOSED')]


@pytest.mark.parametrize('change', ['missing', 'duplicate'])
def test_local_failure_dependency_transform_requires_one_ticket_projection(change):
    from support.remote_effects import with_ticket_dependencies
    anchor = "    def ticket_projection(p):\n        blockers=[]\n"
    source = FAKE_GH.replace(anchor, '') if change == 'missing' else FAKE_GH + anchor
    with pytest.raises(ValueError, match='exactly once'):
        with_ticket_dependencies(source)
