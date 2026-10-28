"""Literal binding of the existing gh fixture, not a new simulator or service."""
import ast
import json
import os
import subprocess
import sys

import pytest

from support.remote_effects import FAKE_GH, fake_gh, _fixture, _publish_pr


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
