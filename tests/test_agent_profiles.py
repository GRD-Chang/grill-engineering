from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from agent_run.agent_profiles import AgentProfileStore, ProfiledAgentBackend, resolve_profiles
from conftest import write_fixture
from test_cli import load_only_run_state, run_cli, stdout_json
from test_cli_delivery import parent_publication, passing_acceptance


def test_preset_resolution_keeps_publication_linked_and_applies_overrides() -> None:
    economy = resolve_profiles(preset="economy")
    assert economy["profiles"]["development"] == {
        "model": "gpt-5.6-luna",
        "reasoning_effort": "max",
        "reference": None,
        "provenance": {
            "preset": "economy",
            "overrides": [],
            "reference": None,
        },
    }
    assert economy["profiles"]["publication"]["reference"] == "development"
    assert economy["profiles"]["publication"]["model"] == "gpt-5.6-luna"

    premium = resolve_profiles(
        preset="premium",
        overrides={
            "development_model": "custom-development",
            "development_effort": "high",
        },
    )
    assert premium["profiles"]["development"]["model"] == "custom-development"
    assert premium["profiles"]["development"]["reasoning_effort"] == "high"
    assert premium["profiles"]["review"]["model"] == "gpt-5.6-sol"
    assert premium["profiles"]["publication"]["model"] == "custom-development"


def test_profile_store_revisions_and_thread_bindings_are_durable(tmp_path: Path) -> None:
    store = AgentProfileStore(tmp_path)
    initial = store.initialize("run-1")
    assert initial["profile_revision"] == 1

    first = store.bind_thread("run-1", role="development", thread_id=None)
    store.attach_thread("run-1", first["binding_id"], "thread-1")
    configured = store.configure(
        "run-1", overrides={"development_model": "next-model"}
    )
    assert configured["profile_revision"] == 2
    assert store.load("run-1")["bindings"][0]["model"] == "gpt-5.6-luna"

    resumed = store.bind_thread("run-1", role="development", thread_id="thread-1")
    assert resumed == first | {"thread_id": "thread-1"}
    fresh = store.bind_thread("run-1", role="development", thread_id=None)
    assert fresh["model"] == "next-model"
    assert fresh["profile_revision"] == 2

    persisted = json.loads((tmp_path / "profiles" / "run-1.json").read_text())
    assert persisted["profile_revision"] == 2
    assert len(persisted["revisions"]) == 2
    assert not list((tmp_path / "profiles").glob("*.tmp"))


def test_publication_override_is_independent_until_reference_is_restored(
    tmp_path: Path,
) -> None:
    store = AgentProfileStore(tmp_path)
    store.initialize("run-1", preset="premium")
    store.configure("run-1", overrides={"publication_model": "publication-model"})
    independent = store.configure(
        "run-1", overrides={"development_model": "later-development"}
    )
    assert independent["profiles"]["publication"] == {
        "model": "publication-model",
        "reasoning_effort": "medium",
        "reference": None,
        "provenance": {
            "preset": "premium",
            "overrides": ["model"],
            "reference": None,
        },
    }
    restored = store.configure(
        "run-1", overrides={"publication_from_development": True}
    )
    assert restored["profiles"]["publication"]["reference"] == "development"
    assert restored["profiles"]["publication"]["model"] == "later-development"


def test_profile_writers_serialize_and_atomic_failure_preserves_previous_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = AgentProfileStore(tmp_path)
    store.initialize("run-1")
    source_path = str(Path(__file__).parents[1] / "src")
    environment = os.environ.copy()
    environment["PYTHONPATH"] = (
        source_path
        if not environment.get("PYTHONPATH")
        else f"{source_path}{os.pathsep}{environment['PYTHONPATH']}"
    )
    script = (
        "from pathlib import Path; import sys; "
        "from agent_run.agent_profiles import AgentProfileStore; "
        "AgentProfileStore(Path(sys.argv[1])).configure("
        "'run-1', overrides={'development_model': sys.argv[2]})"
    )
    writers = [
        subprocess.Popen(
            [sys.executable, "-c", script, str(tmp_path), model],
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for model in ("writer-a", "writer-b")
    ]
    results = [writer.communicate(timeout=10) for writer in writers]
    assert all(writer.returncode == 0 for writer in writers), results
    document = store.load("run-1")
    assert document is not None
    assert document["profile_revision"] == 3
    assert {
        entry["profiles"]["development"]["model"]
        for entry in document["revisions"]
    } == {"gpt-5.6-luna", "writer-a", "writer-b"}

    path = tmp_path / "profiles" / "run-1.json"
    previous = path.read_bytes()

    def fail_replace(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated profile replacement failure")

    monkeypatch.setattr("agent_run.agent_profiles.os.replace", fail_replace)
    with pytest.raises(OSError, match="simulated profile replacement failure"):
        store.configure("run-1", overrides={"development_model": "never-current"})
    assert path.read_bytes() == previous
    assert not list((tmp_path / "profiles").glob("*.tmp"))


class _Backend:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    def develop(self, request: dict[str, Any]) -> str:
        self.requests.append(request)
        event = request["_invocation_event"]
        event("started")
        event("thread_started", reported_thread_id="thread-1")
        event("completed", reported_thread_id="thread-1")
        return "ok"

    review = develop
    publication = develop
    run_publication = develop


def test_profiled_backend_records_binding_facts_before_agent_starts(tmp_path: Path) -> None:
    store = AgentProfileStore(tmp_path)
    store.initialize("run-1")
    events: list[dict[str, Any]] = []
    backend = _Backend()
    profiled = ProfiledAgentBackend(backend, store)

    request = {
        "run_id": "run-1",
        "thread_id": None,
        "_invocation_event": lambda kind, **facts: events.append(
            {"kind": kind, **facts}
        ),
    }
    assert profiled.develop(request) == "ok"
    started = events[0]
    assert started["model"] == "gpt-5.6-luna"
    assert started["reasoning_effort"] == "max"
    assert started["profile_revision"] == 1
    assert started["thread_execution_binding"]["thread_id"] is None
    assert events[1]["thread_execution_binding"]["thread_id"] == "thread-1"

    resumed_events: list[dict[str, Any]] = []
    profiled.develop(
        {
            "run_id": "run-1",
            "thread_id": "thread-1",
            "_invocation_event": lambda kind, **facts: resumed_events.append(
                {"kind": kind, **facts}
            ),
        }
    )
    assert resumed_events[0]["profile_revision"] == 1
    assert resumed_events[0]["model"] == "gpt-5.6-luna"


def test_independent_publication_does_not_reuse_development_thread(
    tmp_path: Path,
) -> None:
    store = AgentProfileStore(tmp_path)
    store.initialize("run-1", preset="premium")
    store.configure("run-1", overrides={"publication_model": "publication-model"})

    class RoleBackend:
        def __init__(self) -> None:
            self.requests: list[dict[str, Any]] = []

        def develop(self, request: dict[str, Any]) -> str:
            self.requests.append(request)
            event = request["_invocation_event"]
            event("started")
            event("thread_started", reported_thread_id="development-thread")
            event("completed", reported_thread_id="development-thread")
            return "development"

        def publication(self, request: dict[str, Any]) -> str:
            self.requests.append(request)
            assert request["thread_id"] is None
            event = request["_invocation_event"]
            event("started")
            event("thread_started", reported_thread_id="publication-thread")
            event("completed", reported_thread_id="publication-thread")
            return "publication"

    backend = RoleBackend()
    profiled = ProfiledAgentBackend(backend, store)
    profiled.develop({"run_id": "run-1", "thread_id": None})
    assert (
        profiled.publication(
            {"run_id": "run-1", "thread_id": "development-thread"}
        )
        == "publication"
    )
    bindings = store.load("run-1")["bindings"]
    assert {(item["role"], item["thread_id"]) for item in bindings} == {
        ("development", "development-thread"),
        ("publication", "publication-thread"),
    }


def test_duplicate_thread_attach_is_idempotent_and_cleans_pending_binding(
    tmp_path: Path,
) -> None:
    store = AgentProfileStore(tmp_path)
    store.initialize("run-1")
    first = store.bind_thread("run-1", role="development", thread_id=None)
    second = store.bind_thread("run-1", role="development", thread_id=None)
    store.attach_thread("run-1", first["binding_id"], "thread-1")
    attached = store.attach_thread("run-1", second["binding_id"], "thread-1")
    assert attached["binding_id"] == first["binding_id"]
    bindings = store.load("run-1")["bindings"]
    assert len(bindings) == 1
    assert bindings[0]["thread_id"] == "thread-1"


def test_public_cli_creates_and_updates_a_run_profile(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    started = run_cli(
        git_repo,
        fixture,
        "start",
        "1",
        "--preset",
        "premium",
        "--development-effort",
        "high",
    )
    assert started.returncode == 0, started.stderr
    run_id = str(stdout_json(started)["run_id"])
    profile_path = git_repo / ".agent-run" / "profiles" / f"{run_id}.json"
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    assert profile["profile_revision"] == 1
    assert profile["profiles"]["development"]["model"] == "gpt-5.6-sol"
    assert profile["profiles"]["development"]["reasoning_effort"] == "high"
    assert profile["profiles"]["publication"]["reference"] == "development"
    before_status = load_only_run_state(git_repo)["status"]

    configured = run_cli(
        git_repo,
        fixture,
        "configure",
        run_id,
        "--development-model",
        "custom-development",
    )
    assert configured.returncode == 0, configured.stderr
    assert stdout_json(configured)["profile_revision"] == 2
    assert load_only_run_state(git_repo)["status"] == before_status
    empty = run_cli(git_repo, fixture, "configure", run_id)
    assert empty.returncode == 2
    assert json.loads(profile_path.read_text(encoding="utf-8"))["profile_revision"] == 2


def test_public_configuration_during_active_invocation_keeps_old_binding(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    started_file = git_repo / "gate.started"
    release_file = git_repo / "gate.release"
    agent_fixture = git_repo / "agents.json"
    agent_fixture.write_text(
        json.dumps(
            {
                "invocation_gate": {
                    "role": "developments",
                    "started_file": str(started_file),
                    "release_file": str(release_file),
                    "timeout_seconds": 20,
                },
                "developments": [
                    {
                        "expected_thread_id": None,
                        "thread_id": "parent-development",
                        "summary": "Parent request delivered.",
                        "write_files": {"parent-feature.txt": "done\n"},
                    }
                ],
                "publications": [parent_publication()],
                "reviews": [passing_acceptance("parent-review", "passed")],
            }
        ),
        encoding="utf-8",
    )
    environment = os.environ.copy()
    source_path = str(Path(__file__).parents[1] / "src")
    environment["PYTHONPATH"] = (
        source_path
        if not environment.get("PYTHONPATH")
        else f"{source_path}{os.pathsep}{environment['PYTHONPATH']}"
    )
    command = [
        sys.executable,
        "-m",
        "agent_run",
        "run",
        "1",
        "--preset",
        "premium",
        "--development-model",
        "old-development",
        "--agent-fixture",
        str(agent_fixture),
        "--github-fixture",
        str(fixture),
    ]
    process = subprocess.Popen(
        command,
        cwd=git_repo,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 10
        while not started_file.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert started_file.exists()
        run_files = list((git_repo / ".agent-run" / "runs").glob("*.json"))
        assert len(run_files) == 1
        run_id = run_files[0].stem

        active_before = stdout_json(run_cli(git_repo, fixture, "status", run_id, "--json"))
        assert active_before["agent_invocation"]["model"] == "old-development"
        assert active_before["agent_invocation"]["profile_revision"] == 1

        configured = run_cli(
            git_repo,
            fixture,
            "configure",
            run_id,
            "--development-model",
            "new-development",
            "--review-model",
            "new-review",
        )
        assert configured.returncode == 0, configured.stderr
        assert stdout_json(configured)["profile_revision"] == 2

        active_after = stdout_json(run_cli(git_repo, fixture, "status", run_id, "--json"))
        assert active_after["agent_invocation"]["model"] == "old-development"
        assert active_after["agent_invocation"]["profile_revision"] == 1
        assert active_after["status"] == active_before["status"]

        release_file.touch()
        stdout, stderr = process.communicate(timeout=20)
        assert process.returncode == 0, stderr
        assert json.loads(stdout)["status"] == "parent_approval_pending"
        assert (
            "Agent Execution Binding: role=development thread=new "
            "model=old-development reasoning_effort=medium profile_revision=1"
        ) in stderr
        assert (
            "Agent Execution Binding: role=review thread=new "
            "model=new-review reasoning_effort=high profile_revision=2"
        ) in stderr
        idle = stdout_json(run_cli(git_repo, fixture, "status", run_id, "--json"))
        assert idle["agent_invocation"] is None
        history = stdout_json(run_cli(git_repo, fixture, "history", run_id, "--json"))
        development = next(
            item
            for item in history["agent_invocations"]
            if item.get("binding_role") == "development"
        )
        assert development["model"] == "old-development"
        assert development["reasoning_effort"] == "medium"
        assert development["profile_revision"] == 1
        review = next(
            item
            for item in history["agent_invocations"]
            if item.get("binding_role") == "review"
        )
        assert review["model"] == "new-review"
        assert review["profile_revision"] == 2
        text_history = run_cli(git_repo, fixture, "history", run_id).stdout
        assert "model=old-development" in text_history
        assert "profile_revision=1" in text_history
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
