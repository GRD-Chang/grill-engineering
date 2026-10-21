"""Create and continue v6 test authorities in the unchanged frozen runtime."""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
from typing import Any, Callable


BASELINE = "02b008e43386a73c4510250732a7216d7dba17a5"

_SCRIPT = r'''
import json, sys
from pathlib import Path
from agent_run.controller import Controller
from agent_run.agents import ReviewResult
from agent_run.git import GitRepository
from agent_run.github_fixture import FixtureGitHubReader
from agent_run.run_acceptance import RunAcceptanceEngine
from agent_run.state import SimulatedProcessCrash, StateStore
from support.run_artifacts import _passing_artifact
from support.run_scenarios import _completed_run
from support.seeding import write_fixture
import agent_run.controller

frozen = Path(sys.argv[1]).resolve()
assert Path(agent_run.controller.__file__).resolve().is_relative_to(frozen / "project/src")
request = json.load(sys.stdin)
operation = request["operation"]
root = Path(request["root"])
states = None if operation == "fixture" else StateStore(root)
if operation == "fixture":
    result = str(write_fixture(Path(request["fixture_path"]), issues=request["issues"]))
elif operation == "save":
    states.save_run(request["run_id"], request["state"])
    result = None
elif operation == "load":
    result = states.load_run(request["run_id"])
elif operation == "start":
    state, reused = Controller(FixtureGitHubReader(Path(request["fixture"])),
                               GitRepository(Path(request["git"])), states).start(1)
    result = {"state": state, "reused": reused, "loaded": states.load_run(state["run_id"])}
elif operation == "returned_review":
    state, states, git = _completed_run(Path(request["git"]))
    assert states.root == root
    run_id = state["run_id"]

    class Reviewer:
        def review(self, request):
            event = request["_invocation_event"]
            event("started", requested_thread_id=request.get("thread_id"), attempt_count=0)
            event("thread_started", reported_thread_id="captured-review", attempt_count=1)
            event("completed", reported_thread_id="captured-review", attempt_count=1)
            return ReviewResult("captured-review", _passing_artifact())

    def cut_after_raw():
        saved = StateStore(states.root).load_current_run(run_id)
        assert saved is not None
        if "returned_result" in saved.get("run_acceptance", {}).get("pending_semantic_attempt", {}):
            raise SimulatedProcessCrash("durable raw before consumption")

    states._after_run_saved = cut_after_raw
    try:
        RunAcceptanceEngine(git=git, states=states, agents=Reviewer()).accept(run_id)
    except SimulatedProcessCrash as error:
        assert "durable raw" in str(error)
    else:
        raise AssertionError("old runtime did not reach the durable raw boundary")
    result = states.load_run(run_id)
elif operation == "consume_review":
    class NoNewReview:
        def review(self, request):
            raise AssertionError("the original durable raw must be consumed")

    result = RunAcceptanceEngine(git=GitRepository(Path(request["git"])), states=states,
                                 agents=NoNewReview()).accept(request["run_id"])
else:
    raise AssertionError(operation)
print(json.dumps(result, ensure_ascii=False))
'''


def old_v6_runtime(directory: Path) -> Callable[..., Any]:
    """Materialize once; only the old child imports legacy production classes."""
    repository = Path(__file__).resolve().parents[3]
    archive = subprocess.run(
        ["git", "-C", str(repository), "archive", BASELINE,
         "project/src", "project/tests/support"],
        check=True, capture_output=True, timeout=30,
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
        for member in bundle.getmembers():
            target = directory / member.name
            assert target.resolve().is_relative_to(directory.resolve())
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                assert member.isfile(), member.name
                stream = bundle.extractfile(member)
                assert stream is not None
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(stream.read())
    expected = {path: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in directory.rglob("*") if path.is_file()}
    tree = subprocess.run(
        ["git", "-C", str(repository), "ls-tree", "-rz", BASELINE,
         "project/src", "project/tests/support"],
        check=True, capture_output=True, timeout=30,
    ).stdout
    originals = {}
    for entry in tree.rstrip(b"\0").split(b"\0"):
        metadata, name = entry.split(b"\t", 1)
        originals[directory / name.decode()] = metadata.split()[2].decode()
    assert set(originals) == set(expected)
    for path, blob in originals.items():
        raw = path.read_bytes()
        assert hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest() == blob

    def invoke(operation: str, *, root: Path, **values: Any) -> Any:
        assert {path: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in directory.rglob("*") if path.is_file()} == expected
        environment = {**os.environ,
                       "PYTHONPATH": os.pathsep.join(str(directory / name)
                                                    for name in ("project/src", "project/tests")),
                       "PYTHONDONTWRITEBYTECODE": "1"}
        request = {"operation": operation, "root": str(root),
                   **{key: str(value) if isinstance(value, Path) else value for key, value in values.items()}}
        completed = subprocess.run(
            [sys.executable, "-c", _SCRIPT, str(directory)], env=environment,
            input=json.dumps(request), capture_output=True, text=True, timeout=30,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
        assert {path: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in directory.rglob("*") if path.is_file()} == expected
        return json.loads(completed.stdout)

    return invoke
