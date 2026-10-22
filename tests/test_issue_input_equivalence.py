"""Compare real reader requests/results with the exact pre-replacement source."""

import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile

import pytest

from test_kernel_source_reading import _reader


BASELINE = "3084b9623a9972eaf6811fb44156f22ec93dc0d2"
_READ = """
from dataclasses import asdict
import json, sys
from pathlib import Path
from agent_run.github import GhGitHubReader, GitHubReadError
from agent_run_kernel.source_reading import read_requirements, read_pull_request_facts
import agent_run.github, agent_run.models
source, root, repository, operation = sys.argv[1:]
assert Path(agent_run.github.__file__).is_relative_to(source)
assert Path(agent_run.models.__file__).is_relative_to(source)
reader = GhGitHubReader(repository, working_directory=Path(root), runner_identity={'source': 'host'})
try:
    if operation == 'fixture':
        from agent_run.github_fixture import FixtureGitHubReader
        from agent_run.revisions import effective_revision_from_graph
        graph = FixtureGitHubReader(Path(root) / 'fixture.json').delivery_graph(1)
        value = {'graph': asdict(graph), 'order': list(graph.issues),
                 'revision': effective_revision_from_graph(graph, 9)}
    else:
        value = asdict(read_requirements(reader, 1)) if operation == 'requirements' else read_pull_request_facts(reader, 17)
    output = {'value': value}
except (GitHubReadError, ValueError) as error:
    output = {'error': getattr(error, 'code', type(error).__name__), 'message': str(error)}
print(json.dumps(output, ensure_ascii=False, sort_keys=True, default=sorted))
"""


@pytest.fixture(scope="session")
def original_source(tmp_path_factory):
    root = tmp_path_factory.mktemp("issue-input-baseline")
    repository = Path(__file__).resolve().parents[2]
    archive = subprocess.check_output(["git", "-C", str(repository), "archive", BASELINE, "project/src"])
    with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
        for member in bundle.getmembers():
            target = root / member.name
            assert target.resolve().is_relative_to(root.resolve())
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                assert member.isfile()
                stream = bundle.extractfile(member)
                assert stream is not None
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(stream.read())
    return root / "project/src"


def _observe(source, root, repository, operation, log):
    log.unlink(missing_ok=True)
    child = subprocess.run([sys.executable, "-B", "-c", _READ, str(source), str(root), repository, operation],
                           env={**os.environ, "PYTHONPATH": str(source)}, cwd=root,
                           text=True, capture_output=True, timeout=15)
    assert child.returncode == 0, child.stderr
    return json.loads(child.stdout), [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


@pytest.mark.parametrize(("repository", "damage"), [
    ("OwnerA/Project", None), ("OwnerB/Project", None),
    ("example/project", "parent-cursor"), ("example/project", "blocker-cursor"),
    ("example/project", "missing-ticket"), ("example/project", "blocker-state"),
    ("example/project", "duplicate-sub-issue"),
])
def test_paginated_graph_has_identical_results_errors_and_request_trace(
    tmp_path, monkeypatch, original_source, repository, damage,
):
    _adapter, log = _reader(tmp_path, monkeypatch)
    script = tmp_path / "bin/gh"
    program = script.read_text().replace("example/project", repository)
    owner, name = repository.split("/")
    program = program.replace("    key=fields.get('cursor')", f"    assert fields['owner']=={owner!r} and fields['name']=={name!r}\n    key=fields.get('cursor')")
    script.write_text(program)
    responses = tmp_path / "responses.json"
    data = json.loads(responses.read_text())
    if damage == "parent-cursor":
        data["parent"]["subIssues"]["pageInfo"]["endCursor"] = None
    elif damage == "blocker-cursor":
        data["9"]["blockedBy"]["pageInfo"]["endCursor"] = None
    elif damage == "missing-ticket":
        data["3"] = None
    elif damage == "blocker-state":
        data["blocker-next"]["blockedBy"]["nodes"][0]["state"] = 42
    elif damage == "duplicate-sub-issue":
        data["parent-next"]["subIssues"]["nodes"] = [{"number": 9}]
    responses.write_text(json.dumps(data))
    expected, before_calls = _observe(original_source, tmp_path, repository, "requirements", log)
    actual, after_calls = _observe(Path(__file__).resolve().parents[1] / "src", tmp_path, repository, "requirements", log)
    assert actual == expected and after_calls == before_calls
    assert not any("--method" in call for call in after_calls)
    if damage is None:
        assert len(after_calls) == 7
        assert actual["value"]["parent"]["sub_issue_numbers"] == [9, 3]
        assert actual["value"]["issues"][0]["blocked_by"] == [
            {"number": 99, "state": "OPEN"}, {"number": 3, "state": "CLOSED"}]
    else:
        assert "error" in actual and "value" not in actual
        assert actual["message"]


@pytest.mark.parametrize("head_repository", ["outside/fork", None])
def test_pr_actor_narrative_and_exact_foreign_or_deleted_head_facts_are_unchanged(
    tmp_path, monkeypatch, original_source, head_repository,
):
    _adapter, log = _reader(tmp_path, monkeypatch)
    responses = tmp_path / "responses.json"
    data = json.loads(responses.read_text())
    data["pull_request"]["head"]["repo"] = None if head_repository is None else {"full_name": head_repository}
    responses.write_text(json.dumps(data))
    expected, before_calls = _observe(original_source, tmp_path, "example/project", "pr", log)
    actual, after_calls = _observe(Path(__file__).resolve().parents[1] / "src", tmp_path, "example/project", "pr", log)
    assert actual == expected and after_calls == before_calls
    assert len(after_calls) == 3
    assert actual["value"] == data["pull_request"]


def test_shared_fixture_graph_index_and_revision_consumer_still_match(original_source, tmp_path):
    fixture = tmp_path / "fixture.json"
    fixture.write_text(json.dumps({
        "parent": {"number": 1, "title": "Parent", "body": "原始需求", "sub_issues": [9, 3],
                   "sub_issue_order_reliable": False},
        "issues": {str(number): {"number": number, "title": f"Issue {number}", "body": "Original body",
            "state": "OPEN", "labels": ["ready-for-agent"],
            "blocked_by": [{"number": 99, "state": "CLOSED"}]} for number in (9, 3)},
    }))
    before = fixture.read_bytes()
    log = tmp_path / "unused-transport-log"
    expected, old_calls = _observe(original_source, tmp_path, "example/project", "fixture", log)
    actual, new_calls = _observe(Path(__file__).resolve().parents[1] / "src", tmp_path, "example/project", "fixture", log)
    assert actual == expected and new_calls == old_calls == []
    assert actual["value"]["order"] == [9, 3]
    assert actual["value"]["graph"]["parent"]["sub_issue_order_reliable"] is False
    assert fixture.read_bytes() == before
