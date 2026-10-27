from __future__ import annotations

from support.repository_fixture import create_kernel

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


def test_resume_without_a_managed_workspace_explains_how_to_start(tmp_path: Path) -> None:
    environment = os.environ.copy()
    environment['PYTHONPATH'] = str(Path(__file__).parents[1] / 'src')
    result = subprocess.run(
        [sys.executable, '-m', 'agent_run', 'resume', '213',
         '--repo', 'example/project', '--json'],
        cwd=tmp_path, env=environment, text=True, capture_output=True, check=False,
    )
    assert result.returncode == 2
    diagnostic = json.loads(result.stdout)['diagnostics'][0]
    assert diagnostic['code'] == 'unsupported_state'
    assert diagnostic['reason'] == 'unreadable_state' and diagnostic['recovery_hint']
    assert not Path(diagnostic['path']).exists()
    assert not (tmp_path / '.agent-run').exists()


@pytest.mark.parametrize("language", ["zh", "en"])
@pytest.mark.parametrize("arguments", [[], ["run"], ["resume"], ["prompts"], ["settings", "configure"]])
def test_current_command_help_uses_personal_language_without_starting_work(
    tmp_path: Path, monkeypatch, capsys, language: str, arguments: list[str],
) -> None:
    from agent_run.config.defaults import UserDefaultsStore
    from agent_run.core.kernel import Kernel
    from agent_run.cli.main import main

    store = UserDefaultsStore()
    store.configure(language=language)
    original = store.path.read_bytes()

    def forbidden(*_args, **_kwargs):
        pytest.fail("Help must not create a Run or start a process")

    monkeypatch.setattr(Kernel, "create_task", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    assert main([*arguments, "--help"]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.startswith("用法:" if language == "zh" else "usage:")
    assert ("显示帮助并退出" if language == "zh" else "show this help message and exit") in captured.out
    assert store.path.read_bytes() == original
    assert not (tmp_path / ".agent-run").exists()


@pytest.mark.parametrize("language", ["zh", "en"])
def test_invalid_choice_localizes_copy_and_preserves_argument_facts(
    monkeypatch, capsys, language: str,
) -> None:
    from agent_run.config.defaults import UserDefaultsStore
    from agent_run.cli.main import main

    store = UserDefaultsStore()
    store.configure(language=language)
    original = store.path.read_bytes()
    invalid = "user-value-用户原文"
    assert main(["settings", "configure", "--development-thread-policy", invalid]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert ("无效选项" if language == "zh" else "invalid choice") in captured.err
    assert invalid in captured.err
    assert "reuse" in captured.err and "new-per-attempt" in captured.err
    assert store.path.read_bytes() == original


@pytest.mark.parametrize("language", ["zh", "en"])
@pytest.mark.parametrize("as_json", [False, True], ids=["plain", "json"])
@pytest.mark.parametrize('authority', ['absent', 'empty'])
def test_missing_run_keeps_current_code_and_explains_discovery_before_creation(
    tmp_path: Path, monkeypatch, capsys, language: str, as_json: bool, authority: str,
) -> None:
    from agent_run.config.defaults import UserDefaultsStore
    from agent_run.cli.main import main

    monkeypatch.chdir(tmp_path)
    store = UserDefaultsStore()
    store.configure(language=language)
    original = store.path.read_bytes()
    from agent_run.adapters.workspace import ManagedWorkspace
    from agent_run.storage.routing import RepositoryRoute
    from support.environment import durable_snapshot
    workspace = ManagedWorkspace.for_repository('example/project')
    if authority == 'empty':
        RepositoryRoute.for_workspace(workspace).initialize()
    before = durable_snapshot(workspace.root)
    arguments = ["resume", "213", "--repo", "example/project"]
    assert main([*arguments, *(["--json"] if as_json else [])]) == 2
    captured = capsys.readouterr()
    assert captured.err == ""
    code = 'unsupported_state' if authority == 'absent' else 'run_locator_missing'
    if as_json:
        diagnostic = json.loads(captured.out)["diagnostics"][0]
        assert diagnostic['code'] == code
        if authority == 'absent':
            assert diagnostic['reason'] == 'unreadable_state'
            assert diagnostic['path'] == str(workspace.state_root / 'kernel.sqlite3')
            message = diagnostic['recovery_hint']
        else:
            message = diagnostic['message']
    else:
        assert code in captured.out
        if authority == 'absent':
            assert str(workspace.state_root / 'kernel.sqlite3') in captured.out
        message = captured.out
    # An absent requested authority is not proof of an empty Task. Retain the
    # exact preservation/recovery diagnosis instead of suggesting recreation.
    if authority == 'absent':
        assert 'Preserve the database and sidecars' in message
    else:
        assert ("找不到匹配的 Run" if language == "zh" else "No matching Run") in message
        assert ("Runner 工作区" if language == "zh" else "Runner workspace") in message
        assert "agent-run runs --repo OWNER/NAME" in message
        assert "agent-run run PARENT --repo OWNER/NAME" in message
    assert store.path.read_bytes() == original
    assert durable_snapshot(workspace.root) == before
    assert not (tmp_path / ".agent-run").exists()


@pytest.mark.parametrize("language", ["zh", "en"])
def test_doctor_help_localizes_every_option_without_running_a_probe(monkeypatch, capsys, language: str) -> None:
    from agent_run.config.defaults import UserDefaultsStore
    from agent_run.cli.main import main
    import agent_run.install.doctor as doctor

    store = UserDefaultsStore()
    store.configure(language=language)
    before = store.path.read_bytes()

    def forbidden(*_args, **_kwargs):
        pytest.fail("Doctor help must not execute local or online probes")

    monkeypatch.setattr(doctor, "run", forbidden)
    assert main(["doctor", "--help"]) == 0
    output = capsys.readouterr()
    assert output.err == ""
    for option in ("--local", "--online", "--mode", "--repo", "--run", "--state-dir", "--json"):
        assert option in output.out
    prose = " ".join(output.out.split())
    if language == "en":
        assert not any("\u4e00" <= character <= "\u9fff" for character in output.out)
        assert "without reading tokens" in prose
        assert "without calling a model" in prose
        assert "frozen identities" in prose
    else:
        assert "不读取 token" in output.out
        assert "不调用模型" in output.out
        assert "冻结身份" in output.out
    assert store.path.read_bytes() == before


@pytest.mark.parametrize("language", ["zh", "en"])
@pytest.mark.parametrize("as_json", [False, True], ids=["human", "json"])
@pytest.mark.parametrize("exact", [False, True], ids=["parent", "exact-run"])
def test_stop_receipt_uses_frozen_language_and_keeps_internal_identity_only_in_json(
    git_repo: Path, capsys, language: str, as_json: bool, exact: bool,
) -> None:
    from agent_run.adapters.workspace import ManagedWorkspace, read_git_identity
    from agent_run.config.creation import freeze_creation
    from agent_run.config.defaults import UserDefaultsStore
    from agent_run.core.kernel import Kernel
    from agent_run.model import Commit, Delivery, DeliveryKind, ParentIssue, Requirements, RunSpec
    from agent_run.cli.main import main

    store = UserDefaultsStore()
    store.configure(language=language)
    workspace = ManagedWorkspace.for_repository("example/project")
    root = workspace.ensure_path(str(git_repo), identity=read_git_identity(git_repo))
    from agent_run.adapters.git_objects import LocalGit
    base = LocalGit(root).commit("HEAD")
    delivery = Delivery("parent:12", DeliveryKind.PARENT_ONLY, 12, "original", Commit(base.sha, base.tree),
                        "refs/heads/task", "refs/heads/main")
    spec = freeze_creation(RunSpec("private-run-identity", workspace.repository, 12, "parent", "graph", (delivery,),
                                   repository_root=str(root)), store)
    kernel = Kernel.create_task(workspace.state_root / "kernel.sqlite3", spec,
                               requirements=Requirements(ParentIssue(12, "Original Parent", "Original requirement"), ()))
    store.configure(language="en" if language == "zh" else "zh")
    assert main(["stop", spec.run_id if exact else "12", "--repo", workspace.repository,
                 *(["--state-dir", str(kernel.storage.path)] if exact else []),
                 *(["--json"] if as_json else [])]) == 0
    output = capsys.readouterr()
    assert output.err == ""
    with kernel.storage.transaction(write=False) as tx:
        action = dict(tx.one("SELECT id,status FROM actions WHERE kind='stop'"))
        assert tx.one("SELECT stopped FROM runs")[0] == 1
        assert tx.one("SELECT count(*) FROM hosts")[0] == 0
    if as_json:
        assert json.loads(output.out) == {"result": "accepted", "status": "action_applied", "run_id": spec.run_id,
                                         "action_id": action["id"], "action": "stop", "host": "unknown"}
    else:
        assert spec.run_id not in output.out and action["id"] not in output.out
        assert "action_applied" not in output.out
        assert "example/project" in output.out
        if exact:
            assert "agent-run runs --repo example/project" in output.out
        else:
            assert "Parent #12" in output.out
            assert "agent-run status --repo example/project --parent 12" in output.out
        assert ("请求已受理" if language == "zh" else "Request accepted") in output.out
        assert ("不表示 Agent 已启动或交付已完成" if language == "zh" else
                "does not prove that an Agent has started or delivery has completed") in output.out
        assert ("Host 状态未确认" if language == "zh" else "Host state unconfirmed") in output.out


def test_ambiguous_run_error_keeps_locations_and_machine_identity_without_repeating_private_ids(tmp_path: Path, capsys, monkeypatch) -> None:
    from agent_run.cli.main import main
    from support.cli_fixtures import _create
    from support.environment import durable_snapshot as _snapshot

    from agent_run.storage.routing import RepositoryRoute

    # One Task cannot publish two active Runs in one authority. Supply two
    # independently created authorities at the routing boundary to exercise
    # the CLI's refusal and private-ID presentation, rather than reuse one Task.
    kernels = [_create(tmp_path / identity, identity) for identity in ("private-one", "private-two")]
    routes = tuple(RepositoryRoute.from_database(kernel.storage.path) for kernel in kernels)
    monkeypatch.setattr('agent_run.cli.selection.managed_repository_routes', lambda parsed: routes)
    before = _snapshot(tmp_path)
    arguments = ["status", "--repo", "example/project", "--parent", "1", "--data-root", str(tmp_path)]
    assert main(arguments) == 2
    human = capsys.readouterr().out
    assert "run_locator_conflict" in human and "example/project Parent #1" in human
    for kernel in kernels:
        # A necessary filesystem location may itself contain the Run directory
        # name; it remains copyable, without a separate private-ID column.
        location = str(kernel.storage.path.parent)
        assert location in human
        human = human.replace(location, "")
    assert "private-one" not in human and "private-two" not in human
    assert main([*arguments, "--json"]) == 2
    machine = json.loads(capsys.readouterr().out)
    assert machine["diagnostics"][0]["code"] == "run_locator_conflict"
    assert {item["run_id"] for item in machine["candidates"]} == {"private-one", "private-two"}
    assert _snapshot(tmp_path) == before


@pytest.mark.parametrize("language", ["zh", "en"])
def test_receipt_distinguishes_known_failed_action_from_unconfirmed_state(capsys, language: str) -> None:
    from argparse import Namespace
    from agent_run.cli.main import _print_receipt

    _print_receipt(Namespace(command="resume", repo="example/project", run_id="12", ticket=None, state_dir=None),
                   {"result": "accepted", "status": "action_failed", "host": "exited"}, language)
    output = capsys.readouterr().out
    assert ("请求处理失败" if language == "zh" else "Request processing failed") in output
    assert ("已记录请求状态: 未确认" if language == "zh" else "Stored request state: Unconfirmed") not in output
    assert "action_failed" not in output


def test_ambiguous_databases_in_the_same_directory_have_distinct_copyable_locations(tmp_path: Path, capsys) -> None:
    from agent_run.cli.main import main
    from agent_run.core.kernel import Kernel
    from agent_run.model import Commit, Delivery, DeliveryKind, ParentIssue, Requirements, RunSpec
    from support.offline import fixture_inputs
    from support.environment import durable_snapshot as _snapshot

    delivery = Delivery("parent:1", DeliveryKind.PARENT_ONLY, 1, "original", Commit("base", "tree"),
                        "refs/heads/task", "refs/heads/main")
    source = Requirements(ParentIssue(1, "Parent", "Original requirement"), ())
    for name, identity in (("kernel.sqlite3", "first-private-id"), ("state.sqlite3", "second-private-id")):
        create_kernel(tmp_path / name, RunSpec(identity, "example/project", 1, "parent", "graph", (delivery,),
                      language="en", repository_root=str(tmp_path / "checkout"), inputs=fixture_inputs()), requirements=source)
    before = _snapshot(tmp_path)
    arguments = ["status", "--repo", "example/project", "--parent", "1", "--state-dir", str(tmp_path)]
    assert main(arguments) == 2
    output = capsys.readouterr().out
    assert f"[{tmp_path / 'kernel.sqlite3'}]" in output
    assert f"[{tmp_path / 'state.sqlite3'}]" in output
    assert "first-private-id" not in output and "second-private-id" not in output
    assert main([*arguments, "--json"]) == 2
    candidates = json.loads(capsys.readouterr().out)["candidates"]
    assert {item["database"] for item in candidates} == {str(tmp_path / name) for name in ("kernel.sqlite3", "state.sqlite3")}
    assert {item["run_id"] for item in candidates} == {"first-private-id", "second-private-id"}
    assert _snapshot(tmp_path) == before
