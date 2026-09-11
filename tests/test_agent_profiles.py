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
from agent_run.agent_invocation import invocation_event_recorder
from agent_run.semantic_attempt import allocate_semantic_attempt
from cli_fixtures import run_agents
from conftest import seed_run, write_fixture
from support.inprocess_cli import invoke_cli_inprocess
from test_cli import load_only_run_state, run_cli, stdout_json
from test_cli_delivery import parent_publication, passing_acceptance, ticket


def test_preset_resolution_respects_publication_defaults_and_overrides() -> None:
    economy = resolve_profiles(preset="economy")
    assert economy["profiles"]["development"] == {
        "model": "gpt-5.6-luna",
        "reasoning_effort": "xhigh",
        "reference": None,
        "provenance": {
            "preset": "economy",
            "overrides": [],
            "reference": None,
        },
    }
    assert economy["profiles"]["publication"]["reference"] == "development"
    assert economy["profiles"]["publication"]["model"] == "gpt-5.6-luna"
    assert economy["profiles"]["publication"]["reasoning_effort"] == "xhigh"
    assert economy["profiles"]["review"]["model"] == "gpt-6-astra"
    assert economy["profiles"]["review"]["reasoning_effort"] == "low"

    defaults = resolve_profiles(preset="premium")["profiles"]
    for role in ("development", "review"):
        assert defaults[role]["model"] == "gpt-6-astra"
        assert defaults[role]["reasoning_effort"] == "low"
    assert defaults["publication"]["model"] == "gpt-5.6-luna"
    assert defaults["publication"]["reasoning_effort"] == "xhigh"
    assert defaults["publication"]["reference"] is None

    premium = resolve_profiles(
        preset="premium",
        overrides={
            "development_model": "custom-development",
            "development_effort": "high",
        },
    )
    assert premium["profiles"]["development"]["model"] == "custom-development"
    assert premium["profiles"]["development"]["reasoning_effort"] == "high"
    assert premium["profiles"]["review"]["model"] == "gpt-6-astra"
    assert premium["profiles"]["publication"] == defaults["publication"]


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
        "reasoning_effort": "xhigh",
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


def test_preset_change_keeps_independent_publication_until_explicit_restore(
    tmp_path: Path,
) -> None:
    store = AgentProfileStore(tmp_path)
    store.initialize("run-1")
    store.configure("run-1", overrides={"publication_model": "custom-pub"})

    changed = store.configure(
        "run-1",
        preset="premium",
        overrides={"development_model": "premium-development"},
    )
    assert changed["profiles"]["publication"] == {
        "model": "custom-pub",
        "reasoning_effort": "xhigh",
        "reference": None,
        "provenance": {
            "preset": "economy",
            "overrides": ["model"],
            "reference": None,
        },
    }

    restored = store.configure(
        "run-1", overrides={"publication_from_development": True}
    )
    assert restored["profiles"]["publication"]["reference"] == "development"
    assert restored["profiles"]["publication"]["model"] == "premium-development"


def test_publication_provenance_keeps_preset_and_inherited_effort_source(
    tmp_path: Path,
) -> None:
    store = AgentProfileStore(tmp_path)
    store.initialize("run-1")
    store.configure("run-1", overrides={"publication_model": "custom-pub"})

    store.configure("run-1", preset="premium")
    changed = store.configure("run-1", overrides={"review_model": "custom-review"})
    publication = changed["profiles"]["publication"]
    assert publication["model"] == "custom-pub"
    assert publication["reasoning_effort"] == "xhigh"
    assert publication["provenance"] == {
        "preset": "economy",
        "overrides": ["model"],
        "reference": None,
    }

    linked_store = AgentProfileStore(tmp_path / "linked")
    linked_store.initialize("run-1")
    linked_store.configure("run-1", overrides={"development_effort": "ultra"})
    detached = linked_store.configure(
        "run-1", overrides={"publication_model": "custom-pub"}
    )
    assert detached["profiles"]["publication"]["provenance"] == {
        "preset": "economy",
        "overrides": ["model"],
        "reference": None,
        "inherited": {"role": "development", "overrides": ["effort"]},
    }

    later = linked_store.configure(
        "run-1", overrides={"review_model": "custom-review"}
    )
    assert later["profiles"]["publication"]["provenance"] == detached[
        "profiles"
    ]["publication"]["provenance"]


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
    assert started["reasoning_effort"] == "xhigh"
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

    publication_events: list[dict[str, Any]] = []
    profiled.publication(
        {
            "run_id": "run-1",
            "thread_id": "thread-1",
            "_invocation_event": lambda kind, **facts: publication_events.append(
                {"kind": kind, **facts}
            ),
        }
    )
    assert publication_events[0]["invocation_role"] == "publication"
    assert publication_events[0]["binding_role"] == "development"
    assert publication_events[0]["profile_role"] == "development"


def test_invocation_history_contains_started_snapshot_before_completion() -> None:
    state: dict[str, Any] = {"run_id": "run-1", "agent_invocation_history": []}
    saved: list[dict[str, Any]] = []
    attempt_owner: dict[str, Any] = {}
    semantic_attempt = allocate_semantic_attempt(
        attempt_owner,
        role="publication",
        work_subject="ticket:1",
        generation=1,
        currentness_boundary={"head_sha": "abc"},
        ordinal=1,
        budget_window=None,
    )
    record = invocation_event_recorder(
        state,
        role="publication",
        phase="publication",
        work_subject="ticket:1",
        generation=1,
        invocation_input={"request": "value", "_invocation_deadline_seconds": 42},
        currentness_boundary={"head_sha": "abc"},
        semantic_attempt=semantic_attempt,
        save=lambda value: saved.append(json.loads(json.dumps(value))),
    )

    record(
        "started",
        requested_thread_id=None,
        invocation_role="publication",
        binding_role="development",
        profile_role="development",
        model="gpt-5.6-luna",
        reasoning_effort="max",
        profile_revision=1,
    )
    assert state["agent_invocation_history"][0]["status"] == "running"
    assert state["agent_invocation_history"][0]["deadline_seconds"] == 42
    assert state["agent_invocation_history"][0]["deadline_at"]
    assert saved[-1]["agent_invocation_history"][0]["status"] == "running"

    record("completed", reported_thread_id="thread-1", attempt_count=1)
    assert len(state["agent_invocation_history"]) == 1
    assert state["agent_invocation_history"][0]["status"] == "completed"


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


def test_seeded_run_profile_can_be_updated_by_public_cli(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    started = seed_run(
        git_repo,
        fixture,
        "1",
        "--development-effort",
        "high",
    )
    assert started.returncode == 0, started.stderr
    run_id = str(stdout_json(started)["run_id"])
    profile_path = git_repo / ".agent-run" / "profiles" / f"{run_id}.json"
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    assert profile["profile_revision"] == 1
    assert profile["profiles"]["development"]["model"] == "gpt-5.6-luna"
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

    independent = run_cli(
        git_repo,
        fixture,
        "configure",
        run_id,
        "--publication-model",
        "custom-pub",
    )
    assert independent.returncode == 0, independent.stderr
    independent_profile = json.loads(profile_path.read_text(encoding="utf-8"))
    assert independent_profile["profiles"]["publication"]["reference"] is None
    assert independent_profile["profiles"]["publication"]["model"] == "custom-pub"

    changed_preset = run_cli(
        git_repo,
        fixture,
        "configure",
        run_id,
        "--preset",
        "premium",
        "--development-model",
        "premium-development",
    )
    assert changed_preset.returncode == 0, changed_preset.stderr
    changed_profile = json.loads(profile_path.read_text(encoding="utf-8"))
    assert changed_profile["profiles"]["publication"]["reference"] is None
    assert changed_profile["profiles"]["publication"]["model"] == "custom-pub"

    restored = run_cli(
        git_repo,
        fixture,
        "configure",
        run_id,
        "--publication-from-development",
    )
    assert restored.returncode == 0, restored.stderr
    restored_profile = json.loads(profile_path.read_text(encoding="utf-8"))
    assert restored_profile["profiles"]["publication"]["reference"] == "development"
    assert restored_profile["profiles"]["publication"]["model"] == "premium-development"

    empty = run_cli(git_repo, fixture, "configure", run_id)
    assert empty.returncode == 2
    assert json.loads(profile_path.read_text(encoding="utf-8"))["profile_revision"] == 5


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
        "--development-model",
        "old-development",
        "--agent-fixture",
        str(agent_fixture),
        "--github-fixture",
        str(fixture),
        "--json",
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
        assert active_before["agent_invocation"]["reasoning_effort"] == "xhigh"
        assert active_before["agent_invocation"]["profile_revision"] == 1
        active_history = stdout_json(
            run_cli(git_repo, fixture, "history", run_id, "--json")
        )
        running = next(
            item
            for item in active_history["agent_invocations"]
            if item.get("status") == "running"
        )
        assert running["model"] == "old-development"
        assert running["invocation_role"] == "development"

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
            "model=old-development reasoning_effort=xhigh profile_revision=1"
        ) in stderr
        assert (
            "Agent Execution Binding: role=review thread=new "
            "model=new-review reasoning_effort=low profile_revision=2"
        ) in stderr
        idle = stdout_json(
            invoke_cli_inprocess(git_repo, fixture, "status", run_id, "--json")
        )
        assert idle["agent_invocation"] is None
        history = stdout_json(
            invoke_cli_inprocess(git_repo, fixture, "history", run_id, "--json")
        )
        development = next(
            item
            for item in history["agent_invocations"]
            if item.get("binding_role") == "development"
        )
        assert development["model"] == "old-development"
        assert development["reasoning_effort"] == "xhigh"
        assert development["profile_revision"] == 1
        review = next(
            item
            for item in history["agent_invocations"]
            if item.get("binding_role") == "review"
        )
        assert review["model"] == "new-review"
        assert review["profile_revision"] == 2
        text_history = invoke_cli_inprocess(git_repo, fixture, "history", run_id).stdout
        assert "模型=old-development；推理强度=xhigh" in text_history
        assert "profile_revision" not in text_history
        publication = next(
            item
            for item in history["agent_invocations"]
            if item.get("invocation_role") == "publication"
        )
        assert publication["binding_role"] == "development"
        assert "profile=development" not in text_history
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)


def test_public_resume_reuses_bound_thread_and_reports_its_id(git_repo: Path) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    blocked_agents = git_repo / "blocked-agents.json"
    blocked_agents.write_text(
        json.dumps(
            {
                "developments": [
                    {
                        "expected_thread_id": None,
                        "thread_id": "parent-development",
                        "human_blockers": ["需要恢复开发权限。"],
                    }
                ],
                "publications": [],
                "reviews": [],
            }
        ),
        encoding="utf-8",
    )
    blocked = run_cli(
        git_repo,
        fixture,
        "run",
        "1",
        "--agent-fixture",
        str(blocked_agents),
    )
    assert blocked.returncode == 2, blocked.stderr
    run_id = str(stdout_json(blocked)["run_id"])
    failed_history = stdout_json(
        invoke_cli_inprocess(git_repo, fixture, "history", run_id, "--json")
    )["agent_invocations"]
    first_development = next(
        item for item in failed_history if item.get("invocation_role") == "development"
    )
    assert first_development["status"] == "completed"

    resumed_agents = git_repo / "resumed-agents.json"
    resumed_agents.write_text(
        json.dumps(
            {
                "developments": [
                    {
                        "expected_thread_id": "parent-development",
                        "thread_id": "parent-development",
                        "summary": "恢复后完成开发。",
                        "write_files": {"parent-feature.txt": "done\n"},
                    }
                ],
                "publications": [parent_publication()],
                "reviews": [passing_acceptance("parent-review", "恢复后验收通过。")],
            }
        ),
        encoding="utf-8",
    )
    resumed = run_cli(
        git_repo,
        fixture,
        "resume",
        run_id,
        "--message",
        "权限已恢复。",
        "--agent-fixture",
        str(resumed_agents),
    )
    assert resumed.returncode == 0, resumed.stderr
    assert stdout_json(resumed)["status"] == "parent_approval_pending"
    history = stdout_json(
        invoke_cli_inprocess(git_repo, fixture, "history", run_id, "--json")
    )["agent_invocations"]
    development_invocations = [
        item for item in history if item.get("invocation_role") == "development"
    ]
    assert len(development_invocations) == 2
    assert development_invocations[1]["binding_id"] == first_development["binding_id"]
    assert development_invocations[1]["requested_thread_id"] == "parent-development"
    assert "Agent Execution Binding: role=development thread=resume" in resumed.stderr
    assert "thread_id=parent-development" in resumed.stderr


def test_public_cli_drives_run_review_output_repair(git_repo: Path) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": ticket()})
    started_file = git_repo / "output-repair.started"
    release_file = git_repo / "output-repair.release"
    agent_fixture = run_agents(git_repo / "agents.json")
    agent_data = json.loads(agent_fixture.read_text(encoding="utf-8"))
    agent_data["invocation_gate"] = {
        "role": "run_reviews",
        "attempt": 2,
        "started_file": str(started_file),
        "release_file": str(release_file),
        "timeout_seconds": 20,
    }
    agent_data["run_reviews"] = [
        {"thread_id": "run-reviewer", "invalid": "first attempt"},
        passing_acceptance("run-reviewer", "Output Repair passed."),
    ]
    agent_fixture.write_text(json.dumps(agent_data), encoding="utf-8")

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
        "--agent-fixture",
        str(agent_fixture),
        "--github-fixture",
        str(fixture),
        "--json",
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
        invocation_before = active_before["agent_invocation"]
        assert invocation_before["model"] == "gpt-6-astra"
        assert invocation_before["reasoning_effort"] == "low"
        assert invocation_before["profile_revision"] == 1
        assert invocation_before["invocation_role"] == "review"
        assert invocation_before["binding_role"] == "review"
        assert invocation_before["profile_role"] == "review"
        assert invocation_before["reported_thread_id"] == "run-reviewer"
        binding_before = {
            key: invocation_before[key]
            for key in ("binding_id", "model", "reasoning_effort", "profile_revision")
        }

        active_history = stdout_json(
            run_cli(git_repo, fixture, "history", run_id, "--json")
        )
        running = next(
            item
            for item in active_history["agent_invocations"]
            if item.get("status") == "running"
        )
        assert running["attempt_count"] == 1
        assert running["binding_id"] == binding_before["binding_id"]
        assert running["profile_revision"] == binding_before["profile_revision"]

        configured = run_cli(
            git_repo,
            fixture,
            "configure",
            run_id,
            "--review-model",
            "changed-review",
        )
        assert configured.returncode == 0, configured.stderr
        assert stdout_json(configured)["profile_revision"] == 2

        active_after = stdout_json(run_cli(git_repo, fixture, "status", run_id, "--json"))
        invocation_after = active_after["agent_invocation"]
        assert {
            key: invocation_after[key]
            for key in ("binding_id", "model", "reasoning_effort", "profile_revision")
        } == binding_before
        assert invocation_after["reported_thread_id"] == "run-reviewer"

        release_file.touch()
        stdout, stderr = process.communicate(timeout=20)
        assert process.returncode == 0, stderr
        assert json.loads(stdout)["status"] == "run_approval_pending"
    finally:
        if process.poll() is None:
            release_file.touch()
            try:
                process.communicate(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()

    state = load_only_run_state(git_repo)
    reviewer_invocations = [
        item
        for item in state["agent_invocation_history"]
        if item.get("role") == "reviewer"
    ]
    assert len(reviewer_invocations) == 1
    assert reviewer_invocations[0]["invocation_role"] == "review"
    assert reviewer_invocations[0]["attempt_count"] == 2
    assert reviewer_invocations[0]["requested_thread_id"] is None
    assert reviewer_invocations[0]["reported_thread_id"] == "run-reviewer"
    assert {
        key: reviewer_invocations[0][key]
        for key in ("binding_id", "model", "reasoning_effort", "profile_revision")
    } == binding_before
