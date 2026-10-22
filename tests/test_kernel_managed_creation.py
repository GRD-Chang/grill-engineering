"""Real clone/path seam used by the native public lifecycle entry."""
from __future__ import annotations

import subprocess
import sys
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest

from agent_run_kernel.executor import _make_ports
from support.core_operations import CoreOperations

from agent_run.managed_workspace import ManagedWorkspace, read_git_identity


def git(root: Path, *arguments: str) -> str:
    return subprocess.run(["git", *arguments], cwd=root, check=True, text=True,
                          capture_output=True).stdout.strip()


def test_native_clone_copies_only_original_identity_and_opens_without_legacy_runtime(
    git_repo: Path, tmp_path: Path,
) -> None:
    git(git_repo, "config", "user.name", "Original creator")
    git(git_repo, "config", "user.email", "original@example.test")
    git(git_repo, "config", "core.hooksPath", str(tmp_path / "private-hooks"))
    original = read_git_identity(git_repo)
    before = git(git_repo, "status", "--porcelain=v1")
    workspace = ManagedWorkspace.for_repository("Example/Project")
    clone = workspace.ensure_path(remote_url=str(git_repo), identity=original)
    assert clone == workspace.repository_root
    assert clone != git_repo and not (clone / ".git/objects/info/alternates").exists()
    assert read_git_identity(clone) == {"user.name": "Original creator", "user.email": "original@example.test"}
    assert subprocess.run(["git", "config", "--local", "--get", "core.hooksPath"], cwd=clone).returncode == 1
    git(git_repo, "config", "user.name", "Later global choice")
    (clone / "README.md").write_text("unfinished native delivery\n")
    assert workspace.ensure_path(identity=read_git_identity(git_repo)) == clone
    assert workspace.open_path() == clone
    assert read_git_identity(clone) == original
    assert (clone / "README.md").read_text() == "unfinished native delivery\n"
    assert git(git_repo, "status", "--porcelain=v1") == before
    program = "from agent_run.managed_workspace import ManagedWorkspace; import sys; assert 'agent_run.git' not in sys.modules"
    result = subprocess.run([sys.executable, "-c", program], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


def test_creation_freezes_three_delivery_resource_kinds_without_creating_worktrees(tmp_path: Path) -> None:
    from agent_run_kernel.lifecycle_cli import creation_resources
    from agent_run_kernel.model import Commit, DeliveryKind, Issue, ParentIssue, Requirements
    source = Requirements(ParentIssue(1, "Parent", "Exact body", (9, 3)), (
        Issue(9, "Later", "body", "OPEN"), Issue(3, "First", "body", "CLOSED")))
    base = Commit("a" * 40, "b" * 40)
    root = tmp_path / "managed"
    jobs = creation_resources("run-1", source, base, "refs/heads/main", root)
    assert [job.kind for job in jobs] == [DeliveryKind.TICKET, DeliveryKind.TICKET, DeliveryKind.FINAL_RUN]
    assert [job.issue for job in jobs] == [9, 3, 1]
    assert [job.target_ref for job in jobs[:2]] == ["refs/heads/agent-run/run-1/run"] * 2
    assert jobs[-1].source_ref == "refs/heads/agent-run/run-1/run"
    assert jobs[-1].target_ref == "refs/heads/main"
    assert all(job.base == base and job.common_git_dir == str(root / ".git") for job in jobs)
    assert not root.exists()
    parent = creation_resources("run-2", Requirements(ParentIssue(1, "Parent", "body"), ()), base,
                                "refs/heads/trunk", root)
    assert len(parent) == 1 and parent[0].kind == DeliveryKind.PARENT_ONLY
    assert parent[0].source_ref == "refs/heads/agent-run/run-2/parent"
    assert parent[0].target_ref == "refs/heads/trunk"
    assert not root.exists()


def test_host_preflight_failure_never_accepts_action_or_creates_environment(tmp_path: Path) -> None:
    import os
    import pytest
    from agent_run_kernel import Commit, Delivery, DeliveryKind, Kernel, RunSpec
    from agent_run_kernel.host_adapter import HostUnavailable, SystemdHost
    from agent_run_kernel.lifecycle_cli import submit
    from agent_run.paths import app_runtime_root
    delivery = Delivery("parent:1", DeliveryKind.PARENT_ONLY, 1, "revision", Commit("base", "tree"),
                        "refs/heads/parent", "refs/heads/main")
    kernel = Kernel.create(tmp_path / "state.sqlite3", RunSpec("run-1", "example/project", 1,
                           "parent", "graph", (delivery,), repository_root=str(tmp_path)))
    manager = SystemdHost(systemctl=str(tmp_path / "missing-systemctl"),
                          systemd_run=str(tmp_path / "missing-systemd-run"), environment=dict(os.environ))
    with pytest.raises(HostUnavailable):
        submit(kernel, "run", action_id="not-applied", command=(sys.executable, "-m", "agent_run_kernel.executor"),
               manager=manager, environment=dict(os.environ), runner_lock=tmp_path / "runner.lock")
    with kernel.storage.transaction(write=False) as tx:
        assert tx.one("SELECT count(*) FROM actions")[0] == 0
        assert tx.one("SELECT count(*) FROM hosts")[0] == 0
        assert tx.one("SELECT executor FROM runs")[0] is None
    assert not (app_runtime_root() / "native-host").exists()


def test_prepare_creation_uses_original_defaults_real_remote_and_independent_clone(
    git_repo: Path, tmp_path: Path, monkeypatch,
) -> None:
    import json
    import os
    from tests.test_kernel_remote_effects import FAKE_GH
    from agent_run.user_defaults import UserDefaultsStore
    from agent_run_kernel.lifecycle_cli import prepare_creation
    from agent_run_kernel.model import DeliveryKind
    remote = tmp_path / "origin.git"
    git(git_repo, "init", "--bare", "--initial-branch=main", str(remote))
    git(git_repo, "push", str(remote), "HEAD:refs/heads/main")
    initial = git(git_repo, "rev-parse", "HEAD")
    original_identity = read_git_identity(git_repo)
    (git_repo / "unpublished.txt").write_text("Never copy this file\n")
    defaults = UserDefaultsStore()
    defaults.configure(language="en", profile={"preset": "economy"})
    binary = tmp_path / "fake-bin"
    binary.mkdir()
    script = binary / "gh"
    script.write_text("#!" + sys.executable + "\n" + FAKE_GH)
    script.chmod(0o755)
    state, calls = tmp_path / "github.json", tmp_path / "calls.jsonl"
    state.write_text(json.dumps({"prs": [], "source_graph": {"parent": {
        "number": 1, "title": "Original Parent", "body": "Original body\r\n保留", "sub_issues": [9]},
        "issues": {"9": {"number": 9, "title": "Ticket", "body": "Exact ticket body", "state": "OPEN",
                            "labels": ["ready-for-agent"]}}}}))
    monkeypatch.setenv("PATH", str(binary) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("REMOTE_TEST_STATE", str(state))
    monkeypatch.setenv("REMOTE_TEST_CALLS", str(calls))
    monkeypatch.setenv("REMOTE_TEST_BARE", str(remote))
    database, spec, source = prepare_creation(1, "example/project", source_directory=git_repo,
                                             defaults=defaults, remote_url=str(remote))
    root = Path(spec.repository_root)
    assert spec.language == "en" and spec.inputs.profiles["preset"] == "economy"
    assert spec.inputs.provenance["defaults"]["language"] == "en"
    assert [job.kind for job in spec.deliveries] == [DeliveryKind.TICKET, DeliveryKind.FINAL_RUN]
    assert all(job.base.sha == initial for job in spec.deliveries)
    assert source.parent.body == "Original body\r\n保留" and source.issues[0].body == "Exact ticket body"
    assert read_git_identity(root) == original_identity
    assert not (root / "unpublished.txt").exists()
    assert database.name == "kernel.sqlite3"
    assert not database.exists() and database.parent.parent == root.parent / "state"
    assert git(root, "for-each-ref", "--format=%(refname)", "refs/heads/agent-run") == ""
    assert git(root, "ls-remote", "--heads", "origin").splitlines() == [initial + "\trefs/heads/main"]
    assert not any(Path(job.checkout).exists() for job in spec.deliveries)
    assert all("--method" not in json.loads(line) for line in calls.read_text().splitlines())


def test_native_public_parser_preserves_creation_and_resume_arguments() -> None:
    from agent_run_kernel.query_cli import _parser
    parser = _parser("en")
    run = parser.parse_args(["run", "1", "--repo", "example/project", "--profile-preset", "economy",
                             "--dev-model", "chosen", "--dev-effort", "high", "--ticket-review-round", "1",
                             "--integration-resync-limit", "0", "--no-ticket-ci-review", "--review-timeout", "2h",
                             "--publication-use-development", "--runner-github-source", "host", "--no-notifications", "--json"])
    assert run.parent == 1 and run.profile_preset == "economy" and run.development_model == "chosen"
    assert run.development_effort == "high" and run.ticket_review_rounds == 1
    assert run.integration_resync_limit == 0 and run.ticket_ci_review is False and run.review_deadline == "2h"
    assert run.publication_from_development and run.no_notifications and run.as_json
    resume = parser.parse_args(["resume", "1", "--repo", "example/project", "--ticket", "9",
                                "--message", "原样反馈\r\nDo not rewrite", "--new-thread", "--json"])
    assert resume.run_id == "1" and resume.ticket == 9 and resume.new_thread
    assert resume.message == "原样反馈\r\nDo not rewrite" and resume.as_json


def test_requeue_of_a_missing_run_reports_failure_without_mutating_settings(tmp_path: Path, capsys) -> None:
    import json
    from agent_run.user_defaults import UserDefaultsStore
    from agent_run_kernel.query_cli import main
    defaults = UserDefaultsStore()
    defaults.configure(language="en")
    before = defaults.path.read_bytes()
    assert main(["requeue", "1", "--repo", "example/project", "--json"]) == 2
    output = json.loads(capsys.readouterr().out)
    assert output["result"] == "error" and output["diagnostics"][0]["code"] == "run_locator_missing"
    assert defaults.path.read_bytes() == before


def test_existing_run_error_uses_frozen_language_after_personal_language_changes(
    git_repo: Path, tmp_path: Path, capsys, monkeypatch,
) -> None:
    from agent_run.user_defaults import UserDefaultsStore
    from agent_run_kernel import Commit, Delivery, DeliveryKind, Kernel, RunSpec
    from agent_run_kernel.configuration import freeze_creation
    from agent_run_kernel.query_cli import main
    workspace = ManagedWorkspace.for_repository("example/project")
    root = workspace.ensure_path(str(git_repo), identity=read_git_identity(git_repo))
    base = Commit(git(root, "rev-parse", "HEAD"), git(root, "rev-parse", "HEAD^{tree}"))
    delivery = Delivery("parent:1", DeliveryKind.PARENT_ONLY, 1, "requirement", base,
                        "refs/heads/agent-run/run-1/parent", "refs/heads/main")
    defaults = UserDefaultsStore()
    defaults.configure(language="en")
    spec = freeze_creation(RunSpec("run-1", "example/project", 1, "parent", "graph", (delivery,),
                                   repository_root=str(root)), defaults)
    from agent_run_kernel import ParentIssue, Requirements
    kernel = Kernel.create_task(workspace.state_root / "run-1" / "kernel.sqlite3", spec,
        requirements=Requirements(ParentIssue(1, "Parent", "Frozen requirement"), ()))
    defaults.configure(language="zh")
    # This is a language/selection test, not a real user-bus release claim.
    # Keep readiness explicit through the existing executable manager fixture.
    from tests.test_kernel_host_adapter import controlled_manager
    from agent_run_kernel import lifecycle_cli, host_adapter
    manager, _request, _response, _calls = controlled_manager(tmp_path)
    monkeypatch.setattr(lifecycle_cli, "_active_runner", lambda: True)
    monkeypatch.setattr(host_adapter, "SystemdHost", lambda **kwargs: manager)
    before = kernel.source_status()
    assert main(["approve", "run-1", "--repo", "example/project", "--state-dir", str(workspace.state_root)]) == 2
    output = capsys.readouterr().out
    assert output.startswith("Command not applied:")
    assert "control_subject_ambiguous" in output
    assert Kernel(kernel.storage.path).source_status() == before
    with kernel.storage.transaction(write=False) as tx:
        assert tx.one("SELECT count(*) FROM actions")[0] == 0
        assert tx.one("SELECT count(*) FROM hosts")[0] == 0


def test_dispatched_unknown_host_is_attached_without_applying_a_new_action(tmp_path: Path, monkeypatch) -> None:
    import json
    import os
    from tests.test_kernel_host_adapter import controlled_manager, host_kernel
    from agent_run_kernel.lifecycle_cli import attach
    kernel = host_kernel(tmp_path)
    manager, fixture, response, calls = controlled_manager(tmp_path)
    private = tmp_path / "original-host"
    private.mkdir(mode=0o700)
    request = kernel.admit_host("original", kind="run", command=fixture.command,
                               environment_carrier=private / "environment.json", preflight=manager.preflight)
    assert kernel.dispatch_host(request) is True
    state = json.loads(response.read_text())
    state["fields"]["LoadState"] = "not-found"
    response.write_text(json.dumps(state))
    before = kernel.host_request("original")
    result = attach(kernel, requested_action="approve", manager=manager, environment=dict(os.environ),
                    runner_lock=tmp_path / "runner.lock")
    assert result["result"] == "attached" and result["action_id"] == "original"
    assert result["host"] == "unknown" and result["requested_action_applied"] is False
    assert result["requested_action"] == "approve"
    assert kernel.host_request("original") == before and kernel.host_dispatched("original")
    assert not request.environment_carrier.exists()
    assert all(json.loads(line)[0] != "systemd-run" for line in calls.read_text().splitlines())
    with kernel.storage.transaction(write=False) as tx:
        assert tx.one("SELECT count(*) FROM actions")[0] == 1
        assert tx.one("SELECT count(*) FROM hosts")[0] == 1


def test_allocated_original_host_continues_its_one_dispatch_and_then_only_attaches(tmp_path: Path, monkeypatch) -> None:
    import json
    import os
    from tests.test_kernel_host_adapter import controlled_manager, host_kernel
    from agent_run_kernel import native_host
    from agent_run_kernel.lifecycle_cli import attach
    kernel = host_kernel(tmp_path)
    manager, fixture, response, calls = controlled_manager(tmp_path)
    private = tmp_path / "original-host"
    private.mkdir(mode=0o700)
    request = kernel.admit_host("original", kind="run", command=fixture.command,
                               environment_carrier=private / "environment.json", preflight=manager.preflight)
    state = json.loads(response.read_text())
    state["launch_returncode"] = 1
    state["fields"]["LoadState"] = "not-found"
    response.write_text(json.dumps(state))
    original_prepare = native_host.prepare_handoff
    guardians = []
    def short_handoff(*args, **kwargs):
        result = original_prepare(*args, **kwargs, ttl_seconds=2.0)
        guardians.append(result.process)
        return result
    monkeypatch.setattr(native_host, "prepare_handoff", short_handoff)
    try:
        result = attach(kernel, requested_action="resume", manager=manager, environment=dict(os.environ),
                        runner_lock=tmp_path / "runner.lock")
        assert result["result"] == "attached" and result["action_id"] == "original"
        assert result["host"] == "unknown" and result["requested_action_applied"] is False
        assert kernel.host_dispatched("original") and kernel.host_request("original") == request
        assert len(guardians) == 1
        again = attach(kernel, requested_action="resume", manager=manager, environment=dict(os.environ),
                       runner_lock=tmp_path / "runner.lock")
        assert again["action_id"] == "original" and again["host"] == "unknown"
        assert len(guardians) == 1
        assert sum(json.loads(line)[0] == "systemd-run" for line in calls.read_text().splitlines()) == 1
        with kernel.storage.transaction(write=False) as tx:
            assert tx.one("SELECT count(*) FROM actions")[0] == 1
            assert tx.one("SELECT count(*) FROM hosts")[0] == 1
    finally:
        for process in guardians:
            assert process.wait(timeout=3) != 0
        assert not request.environment_carrier.exists()


def test_lifecycle_rejects_unsupported_github_host_before_managed_or_remote_access(monkeypatch, capsys) -> None:
    import json
    from agent_run_kernel.query_cli import main
    monkeypatch.setenv("GH_HOST", "outside.example.test")
    workspace = ManagedWorkspace.for_repository("example/project")
    assert main(["resume", "run-1", "--repo", "example/project", "--json"]) == 2
    output = json.loads(capsys.readouterr().out)
    assert output["result"] == "error" and "github.com" in output["diagnostics"][0]["message"]
    assert not workspace.root.exists()


@contextmanager
def _running_public_parent(git_repo: Path, tmp_path: Path, monkeypatch, *, admit: bool = True,
                           tickets: tuple[int, ...] = ()):
    """Real Git/source/manager reads around a controlled ready Host.

    This producer supplies usage readiness; the unchanged managed-entry E2E
    separately proves the actual FDSTORE and guardian handoff.
    """
    import json
    import os
    from agent_run.user_defaults import UserDefaultsStore
    from agent_run_kernel import Commit, Delivery, DeliveryKind, Issue, Kernel, ParentIssue, Requirements, RunSpec
    from agent_run_kernel.configuration import freeze_creation
    from tests.test_kernel_remote_effects import FAKE_GH
    binary = tmp_path / "source-bin"
    binary.mkdir()
    executable = binary / "gh"
    executable.write_text("#!" + sys.executable + "\n" + FAKE_GH)
    executable.chmod(0o700)
    remote_state = tmp_path / "github.json"
    remote_state.write_text(json.dumps({"prs": [], "source_graph": {"parent": {
        "number": 1, "title": "Parent", "body": "Frozen requirement", "sub_issues": list(tickets)}, "issues": {
            str(number): {"number": number, "title": f"Ticket {number}", "body": "Frozen Ticket requirement",
                          "state": "OPEN", "labels": ["ready-for-agent"]} for number in tickets}}}))
    monkeypatch.setenv("PATH", str(binary) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("REMOTE_TEST_STATE", str(remote_state))
    monkeypatch.setenv("REMOTE_TEST_CALLS", str(tmp_path / "gh-calls.jsonl"))
    monkeypatch.setenv("REMOTE_TEST_BARE", str(git_repo / ".git"))
    workspace = ManagedWorkspace.for_repository("example/project")
    root = workspace.ensure_path(str(git_repo), identity=read_git_identity(git_repo))
    base = Commit(git(root, "rev-parse", "HEAD"), git(root, "rev-parse", "HEAD^{tree}"))
    delivery = Delivery("parent:1", DeliveryKind.PARENT_ONLY, 1, "unobserved", base,
                        "refs/heads/parent", "refs/heads/main")
    deliveries = (delivery,) if not tickets else (*tuple(
        Delivery(f"ticket:{number}", DeliveryKind.TICKET, number, "unobserved", base,
                 f"refs/heads/ticket-{number}", "refs/heads/run") for number in tickets),
        Delivery("final:1", DeliveryKind.FINAL_RUN, 1, "unobserved", base, "refs/heads/run", "refs/heads/main"))
    defaults = UserDefaultsStore()
    defaults.configure(language="en")
    spec = freeze_creation(RunSpec("public-controls", workspace.repository, 1, "parent", "graph", deliveries,
                                   repository_root=str(root)), defaults,
                           policy_overrides={"parent_only_paired_rounds": 1, "development_deadline": "7s"})
    assert spec.inputs is not None
    spec = replace(spec, inputs=replace(spec.inputs, repository_remote="origin"))
    kernel = Kernel.create_task(workspace.state_root / spec.run_id / "kernel.sqlite3", spec,
        requirements=Requirements(ParentIssue(1, "Parent", "Frozen requirement", tickets), tuple(
            Issue(number, f"Ticket {number}", "Frozen Ticket requirement", "OPEN", labels=frozenset({"ready-for-agent"}))
            for number in tickets)))
    if tickets:
        # An explicit external run-ref precondition, observed through the real
        # native port rather than represented by a test-constructed World.
        git(git_repo, "update-ref", "refs/heads/run", base.sha)
    with _ready_public_host(kernel, tmp_path, monkeypatch, admit=admit) as (request, execution, calls):
        yield kernel, request, execution, base, calls


@contextmanager
def _ready_public_host(kernel, tmp_path: Path, monkeypatch, *, admit: bool = True):
    """Use the original executable manager and real PID; readiness is explicit."""
    import json
    from agent_run.paths import app_data_root
    from agent_run_kernel import host_adapter, lifecycle_cli
    from agent_run_kernel.model import Execution
    from tests.test_kernel_host_adapter import controlled_manager

    active = app_data_root() / "active" / "current"
    active.parent.mkdir(parents=True)
    active.symlink_to(Path(lifecycle_cli.__file__).resolve().parents[2], target_is_directory=True)
    manager_root = tmp_path / "manager"
    manager_root.mkdir()
    manager, fixture, response, calls = controlled_manager(manager_root)
    monkeypatch.setattr(host_adapter, "SystemdHost", lambda **_kwargs: manager)
    if not admit:
        yield None, None, calls
        return
    request = kernel.admit_host("original", kind="run", command=fixture.command,
        environment_carrier=tmp_path / "original-environment", preflight=manager.preflight)
    assert kernel.dispatch_host(request)
    process = subprocess.Popen([sys.executable, "-I", "-c", "import signal; signal.pause()"], start_new_session=True)
    try:
        group = next(line[3:] for line in Path(f"/proc/{process.pid}/cgroup").read_text().splitlines()
                     if line.startswith("0::"))
        fields = json.loads(response.read_text())
        fields["fields"].update(Id=request.unit_name, Description=request.description, ActiveState="active",
                                 SubState="running", MainPID=str(process.pid), ControlGroup=group)
        response.write_text(json.dumps(fields))
        observation = manager.observe(request)
        assert observation.disposition == "running"
        assert kernel.observe_host(request, replace(observation, usage_ready=True)) == "running"
        yield request, Execution(request.run_id, request.owner, request.epoch), calls
    finally:
        process.terminate()
        process.wait(timeout=3)


def test_public_busy_checkpoint_resume_applies_policy_to_the_original_host(
    git_repo: Path, tmp_path: Path, monkeypatch, capsys,
) -> None:
    import json
    from agent_run_kernel.model import EffectResult
    from agent_run_kernel.query_cli import main
    with _running_public_parent(git_repo, tmp_path, monkeypatch) as (kernel, request, execution, base, calls):
        operations = CoreOperations(kernel, execution, _make_ports)
        original = operations.until(lambda step: step.call is not None).call
        assert original is not None and original.deadline_seconds == 7.0
        kernel.bind_thread(original, "original-thread")
        kernel.receive(original, b'{"result_kind":"development","summary":"Candidate investigation","human_blockers":null}')
        assert operations.once().kind == "progress"
        execute = operations.runner.ports.execute
        def failed_candidate(effect, authorize):
            if effect.operation != "candidate":
                return execute(effect, authorize)
            authorize()  # Core dispatches and guards this exact fixture failure.
            return EffectResult("failure", base, raw={"head": base.sha, "reason": "candidate_integrity"})
        monkeypatch.setattr(operations.runner.ports, "execute", failed_candidate)
        assert operations.once().kind == "progress"
        assert operations.once().kind == "progress"
        assert operations.once().kind == "paused"
        assert "modification_budget_exhausted" in kernel.status("parent:1")["gates"]
        with kernel.storage.transaction(write=False) as tx:
            previous_calls = [dict(row) for row in tx.all("SELECT * FROM invocations")]
            creation_policy = tx.one("SELECT policy FROM runs")[0]
        message = "Continue the original investigation.\r\n保留反馈。"
        assert main(["resume", "1", "--repo", "example/project", "--parent-only-paired-round", "2",
                     "--development-deadline", "13s", "--message", message, "--new-thread", "--json"]) == 0
        output = json.loads(capsys.readouterr().out)
        assert output["result"] == "accepted" and output["host"] == "running"
        assert output["action_id"] != "original" and kernel.active_host_action() == "original"
        assert kernel.host_request("original") == request
        with kernel.storage.transaction(write=False) as tx:
            assert [dict(row) for row in tx.all("SELECT * FROM invocations")] == previous_calls
            assert tx.one("SELECT policy FROM runs")[0] == creation_policy
            assert tx.one("SELECT count(*) FROM hosts")[0] == 1
        budget = kernel.status("parent:1")["budgets"]["requirements"]
        assert (budget["window"], budget["development_limit"], budget["review_limit"]) == (2, 2, 2)
        continued = operations.until(lambda step: step.call is not None).call
        assert continued is not None and continued.deadline_seconds == 13.0
        assert continued.thread_id != original.thread_id and continued.context["human_responses"] == [message]
        assert all(json.loads(line)[0] != "systemd-run" for line in calls.read_text().splitlines())


def test_public_ordinary_busy_resume_rejects_policy_without_consuming_an_action(
    git_repo: Path, tmp_path: Path, monkeypatch, capsys,
) -> None:
    import json
    from agent_run_kernel.query_cli import main
    with _running_public_parent(git_repo, tmp_path, monkeypatch) as (kernel, request, execution, base, calls):
        operations = CoreOperations(kernel, execution, _make_ports)
        original = operations.until(lambda step: step.call is not None).call
        assert original is not None and kernel.dispatch_agent(original)
        kernel.agent_unavailable(original, "preparation_failed", "No Worker was started")
        before = kernel.status("parent:1")
        assert main(["resume", "1", "--repo", "example/project", "--development-deadline", "13s", "--json"]) == 2
        output = json.loads(capsys.readouterr().out)
        assert output["diagnostics"][0]["code"] == "policy_override_boundary"
        assert kernel.status("parent:1") == before
        with kernel.storage.transaction(write=False) as tx:
            assert tx.one("SELECT count(*) FROM actions")[0] == 1
        assert main(["resume", "1", "--repo", "example/project", "--message", "Worker setup fixed", "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["host"] == "running"
        continued = operations.until(lambda step: step.call is not None).call
        assert continued is not None and continued.attempt_id == original.attempt_id
        assert continued.deadline_seconds == 7.0 and kernel.host_request("original") == request
        assert all(json.loads(line)[0] != "systemd-run" for line in calls.read_text().splitlines())


def test_public_requeue_replaces_the_exact_stale_parent_generation_on_its_original_host(
    git_repo: Path, tmp_path: Path, monkeypatch, capsys,
) -> None:
    import json
    from agent_run.github import GhGitHubReader
    from agent_run_kernel import lifecycle_cli
    from agent_run_kernel.query_cli import main
    from agent_run_kernel.source_reading import read_requirements

    with _running_public_parent(git_repo, tmp_path, monkeypatch) as (kernel, request, execution, base, calls):
        operations = CoreOperations(kernel, execution, _make_ports)
        original = operations.until(lambda step: step.call is not None).call
        assert original is not None
        kernel.bind_thread(original, "original-parent-thread")
        kernel.receive(original, b'{"result_kind":"development","summary":"Original investigation","human_blockers":null}')
        assert operations.once().kind == "progress"
        previous = kernel.delivery("parent:1")
        state = tmp_path / "github.json"
        changed = json.loads(state.read_text())
        changed["source_graph"]["parent"]["body"] = "Fresh Parent requirement\r\n保留正文。"
        state.write_text(json.dumps(changed))
        with kernel.storage.transaction(write=False) as tx:
            root = Path(tx.one("SELECT repository_root FROM runs")[0])
            original_calls = [dict(row) for row in tx.all("SELECT * FROM invocations")]
            stored = tx.one("SELECT subject,material_id FROM run_inputs")
            identity = json.loads(tx.get(stored["material_id"], stored["subject"]))["runner_identity"]
        reader = GhGitHubReader("example/project", working_directory=root, runner_identity=identity)
        kernel.refresh(lambda: read_requirements(reader, 1))
        assert kernel.source_status()["blocked"] == "requeue_required"
        monkeypatch.setattr(lifecycle_cli, "submit", lambda *_a, **_kw: pytest.fail("Requeue must use the exact ready original Host"))
        assert main(["requeue", "1", "--repo", "example/project", "--json"]) == 0
        result = json.loads(capsys.readouterr().out)
        assert result["action"] == "requeue" and result["host"] == "running" and result["status"] == "action_applied"
        current = kernel.delivery("parent:1")
        assert current.key == previous.key and current.kind == previous.kind and current.issue == previous.issue
        assert current.source_ref != previous.source_ref and current.checkout != previous.checkout
        assert current.target_ref == previous.target_ref and current.base == base
        assert current.owned_root == str(root.parent / "worktrees" / request.run_id)
        assert current.common_git_dir == str(root / ".git")
        assert not Path(current.checkout).exists()
        assert git(root, "for-each-ref", "--format=%(refname)", current.source_ref) == ""
        assert kernel.status("parent:1")["generation"] == 2
        assert kernel.status("parent:1")["phase"] == "workspace"
        assert kernel.status("parent:1")["revision"] == kernel.source_status()["observed_parent_revision"]
        assert kernel.status("parent:1")["retired_generations"][0]["generation"] == 1
        assert kernel.source_status()["blocked"] is None
        assert kernel.host_request("original") == request
        with kernel.storage.transaction(write=False) as tx:
            assert [dict(row) for row in tx.all("SELECT * FROM invocations")] == original_calls
            assert tx.one("SELECT count(*) FROM hosts")[0] == 1
            assert tx.one("SELECT count(*) FROM actions WHERE kind='requeue'")[0] == 1
        assert all(json.loads(line)[0] != "systemd-run" for line in calls.read_text().splitlines())


def test_public_requeue_without_a_current_change_job_does_not_consume_an_action(
    git_repo: Path, tmp_path: Path, monkeypatch, capsys,
) -> None:
    import json
    from agent_run_kernel.query_cli import main

    with _running_public_parent(git_repo, tmp_path, monkeypatch) as (kernel, request, _execution, _world, _calls):
        before = kernel.source_status()
        assert main(["requeue", request.run_id, "--repo", "example/project", "--json"]) == 2
        result = json.loads(capsys.readouterr().out)
        assert result["diagnostics"][0]["code"] == "control_subject_ambiguous"
        assert kernel.source_status() == before
        assert kernel.host_request("original") == request
        with kernel.storage.transaction(write=False) as tx:
            assert tx.one("SELECT count(*) FROM actions WHERE kind='requeue'")[0] == 0


def test_public_requeue_with_multiple_stale_tickets_requires_an_exact_current_change_job(
    git_repo: Path, tmp_path: Path, monkeypatch, capsys,
) -> None:
    import json
    from agent_run_kernel.query_cli import main

    with _running_public_parent(git_repo, tmp_path, monkeypatch, tickets=(2, 3)) as (kernel, request, execution, base, _calls):
        operations = CoreOperations(kernel, execution, _make_ports)
        for number in (2, 3):
            subject = f"ticket:{number}"
            call = operations.until(lambda step: step.call is not None).call
            assert call is not None and call.subject == subject
            kernel.bind_thread(call, f"original-ticket-{number}")
            kernel.receive(call, b'{"result_kind":"development","summary":"Original Ticket investigation","human_blockers":null}')
            assert operations.once(agent_capacity=0).kind == "progress"
        state = tmp_path / "github.json"
        changed = json.loads(state.read_text())
        for issue in changed["source_graph"]["issues"].values():
            issue["body"] += "\nClarified independently."
        state.write_text(json.dumps(changed))
        assert main(["requeue", "1", "--repo", "example/project", "--json"]) == 2
        result = json.loads(capsys.readouterr().out)
        assert result["diagnostics"][0]["code"] == "control_subject_ambiguous"
        assert kernel.source_status()["blocked"] == "requeue_required"
        assert all(kernel.status(f"ticket:{number}")["generation"] == 1 for number in (2, 3))
        assert kernel.host_request("original") == request
        with kernel.storage.transaction(write=False) as tx:
            assert tx.one("SELECT count(*) FROM actions WHERE kind='requeue'")[0] == 0


def test_public_idle_requeue_binds_the_exact_replacement_to_the_original_host_intent(
    git_repo: Path, tmp_path: Path, monkeypatch, capsys,
) -> None:
    import json
    from agent_run_kernel import lifecycle_cli
    from agent_run_kernel.query_cli import main

    with _running_public_parent(git_repo, tmp_path, monkeypatch, admit=False) as (kernel, _request, _execution, base, _calls):
        execution = kernel.claim_executor("isolated-preparation")
        operations = CoreOperations(kernel, execution, _make_ports)
        call = operations.until(lambda step: step.call is not None).call
        assert call is not None
        kernel.bind_thread(call, "original-parent-thread")
        kernel.receive(call, b'{"result_kind":"development","summary":"Original investigation","human_blockers":null}')
        assert operations.once().kind == "progress"
        kernel.stop_task("retire-isolated-preparation")
        kernel.confirm_executor_exit(execution)
        kernel.control("resume", "prepare-managed-requeue")
        state = tmp_path / "github.json"
        changed = json.loads(state.read_text())
        changed["source_graph"]["parent"]["body"] += "\nFresh observed Parent requirement."
        state.write_text(json.dumps(changed))
        admitted = []

        def record_host_intent(selected, kind, *, action_id, command, manager, environment, runner_lock, **intent):
            # A real durable admission; the long consumer proves physical handoff separately.
            request = selected.admit_host(action_id, kind=kind, command=command,
                environment_carrier=tmp_path / "requeue-environment.json", preflight=manager.preflight, **intent)
            admitted.append(request)
            return {"result": "accepted", "status": "action_applied", "run_id": request.run_id,
                    "action_id": action_id, "action": kind, "host": "unknown", "host_unit": request.unit_name}

        monkeypatch.setattr(lifecycle_cli, "submit", record_host_intent)
        assert main(["requeue", "1", "--repo", "example/project", "--json"]) == 0
        result = json.loads(capsys.readouterr().out)
        assert len(admitted) == 1 and result["host"] == "unknown"
        with kernel.storage.transaction(write=False) as tx:
            host = tx.one("SELECT subject,request_id FROM hosts")
            intent = json.loads(tx.get(host["request_id"], host["subject"]))["intent"]
            assert intent["kind"] == "requeue" and intent["subject"] == "parent:1"
            assert intent["replacement"]["key"] == "parent:1"
            assert intent["replacement"]["source_ref"] == kernel.delivery("parent:1").source_ref
            assert tx.one("SELECT count(*) FROM hosts")[0] == 1
        assert kernel.status("parent:1")["generation"] == 2


def test_public_abandon_of_an_unknown_host_only_fences_the_published_task(
    git_repo: Path, tmp_path: Path, monkeypatch, capsys,
) -> None:
    import json
    import pytest
    from agent_run_kernel import host_adapter, lifecycle_cli, remote_effects
    from agent_run_kernel.model import HostObservation, KernelError
    from agent_run_kernel.query_cli import main

    with _running_public_parent(git_repo, tmp_path, monkeypatch) as (kernel, request, execution, base, _calls):
        operations = CoreOperations(kernel, execution, _make_ports)
        assert kernel.observe_host(request, HostObservation("unknown", request.unit_name,
            evidence="The manager cannot currently prove this original Host's lifetime.")) == "unknown"
        with kernel.storage.transaction(write=False) as tx:
            original_host = dict(tx.one("SELECT * FROM hosts"))
            original_leases = [dict(row) for row in tx.all("SELECT * FROM leases")]
            original_epoch = tx.one("SELECT epoch FROM runs")[0]

        def forbidden(*_args, **_kwargs):
            pytest.fail("Unknown-Host Abandon must not launch, attach, read remote state, or claim cleanup")

        monkeypatch.setattr(host_adapter, "SystemdHost", forbidden)
        monkeypatch.setattr(lifecycle_cli, "attach", forbidden)
        monkeypatch.setattr(lifecycle_cli, "submit", forbidden)
        monkeypatch.setattr(remote_effects, "RemoteEffects", forbidden)
        assert main(["abandon", "1", "--repo", "example/project", "--json"]) == 0
        result = json.loads(capsys.readouterr().out)
        assert result["result"] == "accepted" and result["status"] == "action_pending"
        assert result["action"] == "abandon" and result["host"] == "unknown"
        assert kernel.source_status()["abandonment"] == {"action": result["action_id"], "status": "pending"}
        assert kernel.host_request("original") == request
        with kernel.storage.transaction(write=False) as tx:
            assert dict(tx.one("SELECT * FROM hosts")) == original_host
            assert [dict(row) for row in tx.all("SELECT * FROM leases")] == original_leases
            assert tx.one("SELECT epoch FROM runs")[0] == original_epoch + 1
            assert tx.one("SELECT count(*) FROM actions WHERE kind='abandon'")[0] == 1
            assert tx.one("SELECT count(*) FROM effects WHERE control_action IS NOT NULL")[0] == 0
        fenced = operations.once()
        assert fenced.kind == "paused" and fenced.reason == "executor_fenced", fenced


def test_public_queries_show_the_recorded_pending_abandonment_instead_of_new_business_actions(
    git_repo: Path, tmp_path: Path, monkeypatch, capsys,
) -> None:
    import json
    from agent_run_kernel.effects import LocalGit
    from agent_run_kernel.local_effects import LocalEffects
    from agent_run_kernel.query_cli import main

    with _running_public_parent(git_repo, tmp_path, monkeypatch) as (kernel, request, _execution, _world, _calls):
        with kernel.storage.transaction(write=False) as tx:
            root = Path(tx.one("SELECT repository_root FROM runs")[0])
        resources = kernel.generation_resources()
        inspections = LocalEffects(LocalGit(root)).inspect_resources(resources)
        kernel.stop_task("original-local-stop", subject="parent:1")
        assert kernel.control_host(request, "abandon", "original-abandon", resource_inspections=inspections) == "pending"
        before = kernel.source_status()
        for command in ("status", "history"):
            assert main([command, "--parent", "1", "--repo", "example/project", "--json"]) == 0
            document = json.loads(capsys.readouterr().out)
            assert document["status"] == "abandonment_pending"
            assert document["progress"]["status"] == "abandonment_pending"
            assert document["abandonment"]["action"] == "original-abandon"
            assert document["abandonment"]["status"] == "pending"
            assert all(action["operation"] == "abandon" for action in document["operator_actions"])
            assert main([command, "--parent", "1", "--repo", "example/project", "--plain"]) == 0
            assert "Abandonment pending" in capsys.readouterr().out
        assert kernel.source_status() == before


def test_public_abandon_uses_the_original_ready_host_without_a_second_launch(
    git_repo: Path, tmp_path: Path, monkeypatch, capsys,
) -> None:
    import json
    from agent_run_kernel import lifecycle_cli
    from agent_run_kernel.query_cli import main

    with _running_public_parent(git_repo, tmp_path, monkeypatch) as (kernel, request, _execution, _world, calls):
        monkeypatch.setattr(lifecycle_cli, "submit", lambda *_a, **_kw: pytest.fail("A ready original Host must not be replaced"))
        assert main(["abandon", "1", "--repo", "example/project", "--json"]) == 0
        result = json.loads(capsys.readouterr().out)
        assert result["result"] == "accepted" and result["status"] == "action_pending"
        assert result["action"] == "abandon" and result["host"] == "running"
        assert kernel.host_request("original") == request
        assert kernel.source_status()["abandonment"] == {"action": result["action_id"], "status": "pending"}
        with kernel.storage.transaction(write=False) as tx:
            assert tx.one("SELECT count(*) FROM hosts")[0] == 1
        assert all(json.loads(line)[0] != "systemd-run" for line in calls.read_text().splitlines())


@pytest.mark.parametrize("continuation", [False, True])
def test_public_idle_abandon_records_the_exact_initial_or_continuation_host_intent(
    git_repo: Path, tmp_path: Path, monkeypatch, capsys, continuation: bool,
) -> None:
    import json
    from agent_run_kernel import lifecycle_cli
    from agent_run_kernel.effects import LocalGit
    from agent_run_kernel.local_effects import LocalEffects
    from agent_run_kernel.query_cli import main

    with _running_public_parent(git_repo, tmp_path, monkeypatch, admit=False) as (kernel, _request, _execution, _world, _calls):
        if continuation:
            with kernel.storage.transaction(write=False) as tx:
                root = Path(tx.one("SELECT repository_root FROM runs")[0])
            inspections = LocalEffects(LocalGit(root)).inspect_resources(kernel.generation_resources())
            assert kernel.abandon_task("original-abandon", resource_inspections=inspections) == "pending"
        submitted = []

        def record_host_intent(selected, kind, *, action_id, command, manager, environment, runner_lock, **intent):
            # This verifies real Core admission, not physical Host handoff.
            request = selected.admit_host(action_id, kind=kind, command=command,
                environment_carrier=tmp_path / "new-environment.json", preflight=manager.preflight, **intent)
            submitted.append(request)
            with selected.storage.transaction(write=False) as tx:
                status = tx.one("SELECT status FROM actions WHERE id=?", (action_id,))[0]
            return {"result": "accepted", "status": "action_" + status, "run_id": request.run_id,
                    "action_id": action_id, "action": kind, "host": "unknown", "host_unit": request.unit_name}

        monkeypatch.setattr(lifecycle_cli, "submit", record_host_intent)
        assert main(["abandon", "1", "--repo", "example/project", "--discard-worktree", "--json"]) == 0
        result = json.loads(capsys.readouterr().out)
        assert result["result"] == "accepted" and result["host"] == "unknown"
        assert len(submitted) == 1
        request = submitted[0]
        with kernel.storage.transaction(write=False) as tx:
            host = tx.one("SELECT subject,request_id FROM hosts")
            intent = json.loads(tx.get(host["request_id"], host["subject"]))["intent"]
            assert intent["kind"] == "abandon" and intent["discard_worktree"] is True
            assert intent.get("continuation_of") == ("original-abandon" if continuation else None)
            assert tx.one("SELECT count(*) FROM hosts")[0] == 1
            assert tx.one("SELECT count(*) FROM actions WHERE kind='abandon'")[0] == 1
            assert tx.one("SELECT count(*) FROM invocations")[0] == 0
        assert kernel.source_status()["abandonment"]["status"] == "pending"
        assert kernel.host_request(result["action_id"]) == request
