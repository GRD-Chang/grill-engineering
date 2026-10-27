"""Displayed actions select the original Task through the real CLI grammar."""

from support.repository_fixture import create_kernel
from dataclasses import replace
from hashlib import sha256
import json
import subprocess
import sys

import pytest
from markdown_it import MarkdownIt

from agent_run.adapters.systemd import SystemdHost
from agent_run.adapters.git_objects import LocalGit
from agent_run.adapters.local_git import LocalEffects
from agent_run.adapters.workspace import ManagedWorkspace
from agent_run.cli import lifecycle
from agent_run.cli.main import main
from agent_run.cli.main import _parser
from agent_run.core.kernel import Kernel
from agent_run.core.operation_runner import OperationRunner
from agent_run.core.operation_contract import DeliveryFacts
from agent_run.model import Delivery, DeliveryKind, Issue, ParentIssue, Requirements, RunSpec
from agent_run.notifications.cards import card
from agent_run.notifications.envelope import event_envelope
from agent_run.notifications.event_projection import completion_context, event_state
from agent_run.queries.read_facts import action_guidance, read_boundaries
from agent_run.queries.run import read
from support.offline import GraphScenario, fixture_inputs
from support.core_operations import ReadOnlyPorts


@pytest.fixture
def shell_argv(tmp_path):
    binary = tmp_path / "argv-bin"
    binary.mkdir()
    executable = binary / "agent-run"
    # This captures shell decoding only; the decoded argv is then sent to the
    # actual product CLI below. It never pretends that an action succeeded.
    executable.write_text(f"#!{sys.executable} -I\nimport json,sys\nprint(json.dumps(sys.argv[1:]))\n")
    executable.chmod(0o700)

    def capture(command):
        result = subprocess.run(["/bin/sh", "-c", command], cwd=tmp_path,
            env={"PATH": str(binary), "HOME": str(tmp_path)}, capture_output=True, text=True, timeout=5)
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    return capture


@pytest.fixture
def unavailable_manager(tmp_path, monkeypatch):
    selected = []

    def manager(kernel, parsed, environment):
        selected.append((read(kernel.storage.path, 'status', storage=kernel.storage).data["run_id"], kernel.storage.path))
        # Actual ENOENT from the manager boundary, after real lifecycle Task
        # selection. No Host or external business operation is launched.
        return SystemdHost(systemctl=str(tmp_path / "absent-systemctl"),
                           systemd_run=str(tmp_path / "absent-systemd-run"), environment=environment)

    monkeypatch.setattr(lifecycle, "_active_runner", lambda: True)
    monkeypatch.setattr(lifecycle, "_manager_for", manager)
    return selected


def _displayed_commands(event):
    return [token.content.removesuffix("\n") for element in card(event)["body"]["elements"]
            if element["tag"] == "markdown" for token in MarkdownIt().parse(element["content"])
            if token.type == "fence" and token.content.startswith("agent-run ")]


@pytest.mark.parametrize("language", ["zh", "en"])
def test_completed_parent_cleanup_displays_parseable_original_identity(
    tmp_path, shell_argv, language,
):
    scenario = GraphScenario(tmp_path / "physical", Requirements(ParentIssue(1, "Parent", "Scope"), ()))
    # A fresh public producer, before any work or prepared intent exists.
    scenario.spec = replace(scenario.spec, run_id="a  b", language=language)
    scenario.kernel = create_kernel(tmp_path / "wanted" / "kernel.sqlite3",
                                         scenario.spec, requirements=scenario.requirements)
    scenario.execution = scenario.kernel.claim_executor("original-owner")
    assert scenario.run().kind == "approval"
    assert scenario.kernel.control("approve", "original-approval", subject="parent:1", world=scenario.world()) == "pending"
    for _ in range(100):
        if scenario.kernel.status("parent:1")["phase"] == "complete":
            break
        assert scenario.step().kind not in {"paused", "waiting", "approval"}
    else:
        pytest.fail("normal Parent producer did not reach completion")
    with scenario.kernel.storage.transaction(write=False) as tx:
        view, state = read_boundaries(tx, 0.0), event_state(tx)
        context = completion_context(view, state)
    assert view["delivery_cleanup"]["status"] == "cleanup_pending"
    event = event_envelope(state, "boundary", "original-completion", "Completed", **context)
    guidance = context["next_step"]
    assert guidance in _displayed_commands(event)
    argv = shell_argv(guidance)
    parsed = _parser(language).parse_args(argv)
    assert parsed.command == "resume" and parsed.run_id == "a  b" and parsed.repo == scenario.spec.repository
    document = read(scenario.kernel.storage.path, 'status', storage=scenario.kernel.storage).data
    assert document["next_action"] == guidance == document["delivery_cleanup"]["items"][0]["recovery_action"]
    assert document["run_id"] == "a  b" and document["parent"]["number"] == 1
    assert scenario.kernel.active_host_action() is None


def _published_task(remote, repository, identity, parent, *, language="en", tickets=False):
    workspace = ManagedWorkspace.for_repository(repository)
    workspace.ensure_path(str(remote), identity={"user.name": "Fixture", "user.email": "fixture@example.invalid"})
    source = Requirements(ParentIssue(parent, "Parent", "Scope", (2, 3) if tickets else ()),
        tuple(Issue(number, "Ticket", "Scope", "OPEN", frozenset({"ready-for-agent"}))
              for number in (2, 3)) if tickets else ())
    base = LocalGit(workspace.repository_root).commit("HEAD")
    jobs = tuple(Delivery(f"ticket:{number}", DeliveryKind.TICKET, number, "requirement", base,
                         f"refs/heads/ticket-{number}", "refs/heads/run") for number in (2, 3)) if tickets else ()
    jobs += (Delivery(f"final:{parent}" if tickets else f"parent:{parent}",
        DeliveryKind.FINAL_RUN if tickets else DeliveryKind.PARENT_ONLY, parent, "requirement", base,
        "refs/heads/run", "refs/heads/main"),)
    spec = RunSpec(identity, workspace.repository, parent, "parent", "graph", jobs,
                   repository_root=str(workspace.repository_root), language=language, inputs=fixture_inputs())
    directory = sha256(identity.encode()).hexdigest()[:16]
    return Kernel.create_task(workspace.state_root / "kernel.sqlite3", spec, requirements=source)


@pytest.mark.parametrize("language", ["zh", "en"])
@pytest.mark.parametrize("local", [False, True], ids=["global-stop", "sibling-gate"])
@pytest.mark.parametrize("identity", ["a b", "a  b", "a; false", "a 'quoted'", "--literal-run", "x" * 240, "7"])
def test_displayed_stop_resume_and_queries_select_only_the_original_published_task(
    tmp_path, git_repo, capsys, shell_argv, unavailable_manager, language, local, identity,
):
    wanted = _published_task(git_repo, "example/project", identity, 1, language=language, tickets=local)
    other = _published_task(git_repo, "example/project", "1", 7)
    foreign = _published_task(git_repo, "other/project", identity, 1)
    other_before, foreign_before = other.source_status(), foreign.source_status()
    stop = action_guidance("stop", run_id=identity, repository="example/project",
                          ticket=2 if local else None, language=language)
    if identity.isdecimal():
        assert "exact_run_command_unavailable" in stop and not stop.startswith("agent-run ")
        assert wanted.stop_task("explicit-api-stop", subject="ticket:2" if local else None) == "applied"
    else:
        argv = shell_argv(stop)
        assert main([argv[0], "--json", *argv[1:]]) == 0
        applied = json.loads(capsys.readouterr().out)
        assert applied["run_id"] == identity and applied["status"] == "action_applied"
    if local:
        assert wanted.status("ticket:2")["gates"] == ["operator_stopped"]
        assert wanted.status("ticket:3")["gates"] == []
    else:
        assert wanted.source_status()["stopped"] is True
    with wanted.storage.transaction(write=False) as tx:
        view, state = read_boundaries(tx, 0.0), event_state(tx)
    action, = view["actions"]
    assert action["action"]["selector"] == identity
    assert action["action"]["ticket_number"] == (2 if local else None)
    event = event_envelope(state, "boundary", "original-stop", "Stopped",
        summary="Original summary. " * 20, **completion_context(view, state))
    commands = _displayed_commands(event)
    assert len(commands) == (2 if identity.isdecimal() else 3)
    for command in commands:
        argv = shell_argv(command)
        if argv[0] == "resume":
            assert not identity.isdecimal() and argv[-1] == identity
            assert main([argv[0], "--json", *argv[1:]]) == 2
            failure = json.loads(capsys.readouterr().out)
            assert failure["diagnostics"][0]["code"] == "execution_readiness"
        else:
            assert argv[0] in {"status", "history"}
            assert main([argv[0], "--json", *argv[1:]]) == 0
            document = json.loads(capsys.readouterr().out)
            assert document["run_id"] == identity and document["parent"]["number"] == 1
            assert document["repository"] == "example/project"
    assert unavailable_manager == ([] if identity.isdecimal() else [(identity, wanted.storage.path)])
    assert other.source_status() == other_before and foreign.source_status() == foreign_before
    assert wanted.active_host_action() is None


@pytest.mark.parametrize("language", ["zh", "en"])
@pytest.mark.parametrize("operation", ["resume", "stop"])
def test_stale_numeric_run_card_cannot_control_its_normal_successor(
    tmp_path, git_repo, capsys, shell_argv, unavailable_manager, language, operation,
):
    original = _published_task(git_repo, "example/project", "7", 1, language=language)
    execution = original.claim_executor("original-driver")
    assert original.stop_task("original-pause") == "applied"
    with original.storage.transaction(write=False) as tx:
        state = event_state(tx)
    old_card = event_envelope(state, "boundary", "old-stop", "Stopped",
        next_step=action_guidance(operation, run_id="7", repository="example/project", language=language))
    # Finish only the normally claimed in-process fixture driver; no physical
    # Host or Worker was started or assigned an invented exit observation.
    original.confirm_executor_exit(execution)
    assert original.control("resume", "prepare-abandon") == "applied"
    driver = original.claim_executor("abandon-driver")
    assert original.abandon_task("original-abandon", resource_inspections=()) == "pending"
    base = LocalGit(ManagedWorkspace.for_repository("example/project").repository_root).commit("HEAD")
    local = LocalEffects(LocalGit(ManagedWorkspace.for_repository("example/project").repository_root))

    class Ports(ReadOnlyPorts):
        def inspect(self, resources):
            return local.inspect_resources(resources)

        def execute(self, effect, authority):
            if effect.operation == "inspect_resources":
                return local.execute(effect, authority)
            return super().execute(effect, authority)

    ports = Ports(Requirements(ParentIssue(1, "Parent", "Scope"), ()),
                         "refs/heads/run", "refs/heads/main", DeliveryFacts(base, base))
    runner = OperationRunner(original, driver, lambda _: ports)
    for _ in range(8):
        if runner.run_once().kind == "complete":
            break
    else:
        pytest.fail("the normal resource-free Abandon did not complete")
    original.confirm_executor_exit(driver)
    assert original.source_status()["abandonment"]["status"] == "complete"
    successor = _published_task(git_repo, "example/project", "successor", 1, language=language)
    assert successor.storage.path != original.storage.path
    assert read(successor.storage.path, 'status', storage=successor.storage).data["run_id"] == "successor"
    before = successor.source_status()
    executable = _displayed_commands(old_card)
    for command in executable:
        argv = shell_argv(command)
        main([argv[0], "--json", *argv[1:]])
        capsys.readouterr()
    assert successor.source_status() == before
    assert unavailable_manager == []
    assert executable == []
    rendered = json.dumps(card(old_card), ensure_ascii=False)
    assert "exact_run_command_unavailable" in rendered


@pytest.mark.parametrize("language", ["zh", "en"])
@pytest.mark.parametrize("terminal", [False, True], ids=["plain", "rich"])
@pytest.mark.parametrize("identity,encoded", [("a\u202eb", r'"a\u202eb"'),
    ("a\x1b[31mb", r'"a\u001b[31mb"'), ("a\nb", r'"a\nb"')])
def test_terminal_guidance_never_deletes_a_control_character_to_target_another_run(
    tmp_path, git_repo, capsys, shell_argv, monkeypatch, language, terminal, identity, encoded,
):
    wanted = _published_task(git_repo, "example/project", identity, 1, language=language)
    other = _published_task(git_repo, "example/project", "ab", 7)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: terminal)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("COLUMNS", "200")
    assert main(["status", "--repo", "example/project", "--", identity]) == 0
    output = capsys.readouterr().out
    commands = [line[line.index("agent-run "):] for line in output.splitlines() if "agent-run " in line]
    for command in commands:
        argv = shell_argv(command)
        assert main([argv[0], "--json", *argv[1:]]) == 0
        observed = json.loads(capsys.readouterr().out)
        assert observed["run_id"] == identity and observed["parent"]["number"] == 1
    assert commands == []
    assert encoded in output and identity not in output
    assert "unsafe_command_identity" in output
    assert main(["runs", "--repo", "example/project"]) == 0
    inventory = capsys.readouterr().out
    assert encoded in inventory and identity not in inventory
    assert read(wanted.storage.path, 'status', storage=wanted.storage).data["run_id"] == identity
    assert read(other.storage.path, 'status', storage=other.storage).data["run_id"] == "ab"


@pytest.mark.parametrize("language", ["zh", "en"])
@pytest.mark.parametrize("operation,identity", [("run", "normal-run"), ("resume", "7"),
    ("stop", "7"), ("approve", "7"), ("requeue", "7"), ("abandon", "7")])
def test_task_only_cli_grammar_never_becomes_an_exact_run_action(operation, identity, language):
    guidance = action_guidance(operation, run_id=identity, repository="example/project", language=language)
    assert "exact_run_command_unavailable" in guidance
    assert not guidance.startswith("agent-run ")
    assert json.dumps(identity) in guidance
