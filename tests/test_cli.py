from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from copy import deepcopy
from pathlib import Path
from typing import Any, Sequence

import pytest

import agent_run.cli as cli
import agent_run.doctor as doctor
from agent_run.agent_fixture import FixtureAgentBackend
from agent_run.cli import build_parser, main
from agent_run.controller import Controller
from agent_run.codex import CodexProcessError
from agent_run.git import GitRepository
from agent_run.github_fixture import FixtureGitHubPublisher, FixtureGitHubReader, GitHubReadError
from agent_run.operator_action_presentation import print_operator_action
from agent_run.presentation_helpers import human_next_action
from agent_run.requeue import RequeueError
from agent_run.run_driver import DirectRunOperations, RunStep
from agent_run.semantic_attempt import canonical_fingerprint
from agent_run.state import FaultInjectingStateStore, StateStore
from agent_run.worker_sandbox import WorkerSandboxError
from conftest import write_fixture


PROJECT_ROOT = Path(__file__).parents[1]
_DOCTOR_TIMEOUT_TEST_LIMIT_SECONDS = 5


def _canonical_run_budget() -> dict[str, object]:
    return {
        "window": 1,
        "development_attempts": 0,
        "reviewer_invocations": 0,
        "final_ci_fix_used": False,
        "review_artifacts": [],
        "checkpoint_reason": None,
    }


def test_lifecycle_help_describes_operator_boundaries() -> None:
    help_text = build_parser().format_help()

    assert "doctor" in help_text
    assert "推进正常 Job Loop，停在需要操作者处理的边界" in help_text
    assert "恢复失败/Human Blocker Invocation 或监督超时窗口" in help_text
    assert "仅从 requeue_required 创建新的 Change Job Generation" in help_text
    assert "显示当前状态与下一条允许的操作" in help_text
    assert "显示有界 Invocation 与状态时间线" in help_text
    assert "promotion-handshake" not in help_text
    for internal_command in ("deliver", "accept-run", "publish-run"):
        assert internal_command not in help_text
        with pytest.raises(SystemExit):
            build_parser().parse_args([internal_command, "run-id"])


def test_public_run_preserves_non_invocation_execution_failure_until_resume(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"2": issue(2)})
    agents = git_repo / "agents.json"
    agent_data = {
        "developments": [
            {
                "expected_thread_id": None,
                "thread_id": "developer-2",
                "human_blockers": ["Maintainer input is required."],
            }
        ],
        "publications": [],
        "reviews": [],
    }
    agents.write_text(json.dumps(agent_data), encoding="utf-8")
    run_id = stdout_json(run_cli(git_repo, fixture, "start", "1"))["run_id"]
    states = StateStore(git_repo / ".agent-run")
    assert Controller(
        FixtureGitHubReader(fixture), GitRepository(git_repo), states
    ).record_execution_failure(run_id, "controller failed before an Agent started")
    failed = load_only_run_state(git_repo)
    assert failed["active_agent_invocation"] is None

    status = run_cli(git_repo, fixture, "status", run_id)
    assert "类型: Execution Failure" in status.stdout
    assert "唯一下一步: agent-run resume 1 --repo example/project" in status.stdout
    for command in ("status", "history"):
        json_view = stdout_json(
            run_cli(git_repo, fixture, command, run_id, "--json")
        )
        assert json_view["operator_action"]["type"] == "Execution Failure"
        assert json_view["operator_action"]["object"] == "Ticket #2"
        assert json_view["operator_action"]["phase"] == "active"
        assert json_view["operator_action"]["next_action"] == (
            "agent-run resume 1 --repo example/project"
        )
        assert json_view["next_action"] == json_view["operator_action"][
            "next_action"
        ]
    fixture_data = json.loads(fixture.read_text(encoding="utf-8"))
    fixture_data["repository_read_failures"] = [
        {
            "code": "github_read_failed",
            "message": "repository binding has not converged",
        }
    ]
    fixture.write_text(json.dumps(fixture_data), encoding="utf-8")
    ordinary_run = run_cli(
        git_repo,
        fixture,
        "run",
        "1",
        "--agent-fixture",
        str(agents),
    )

    assert ordinary_run.returncode == 2
    assert load_only_run_state(git_repo) == failed
    assert json.loads(agents.read_text(encoding="utf-8")) == agent_data

    ordinary_run_after_binding = run_cli(
        git_repo,
        fixture,
        "run",
        "1",
        "--agent-fixture",
        str(agents),
    )
    assert ordinary_run_after_binding.returncode == 2
    assert load_only_run_state(git_repo) == failed

    controller = Controller(
        FixtureGitHubReader(fixture), GitRepository(git_repo), states
    )
    with pytest.raises(RequeueError, match="unresolved Execution Failure"):
        controller.requeue(run_id)
    assert load_only_run_state(git_repo) == failed

    for lifecycle_command in ("requeue", "approve", "revise"):
        rejected = run_cli(git_repo, fixture, lifecycle_command, run_id)
        assert rejected.returncode == 2
        assert load_only_run_state(git_repo) == failed

    for invalid_option in (
        ("--message", "not valid for an Execution Failure"),
        ("--new-thread",),
    ):
        invalid_resume = run_cli(
            git_repo,
            fixture,
            "resume",
            "1",
            "--repo",
            "example/project",
            *invalid_option,
        )
        assert invalid_resume.returncode == 2
        assert load_only_run_state(git_repo) == failed

    resumed = run_cli(
        git_repo,
        fixture,
        "resume",
        "1",
        "--repo",
        "example/project",
        "--agent-fixture",
        str(agents),
    )

    assert resumed.returncode == 2, resumed.stderr
    resumed_state = load_only_run_state(git_repo)
    assert stdout_json(resumed)["status"] == "ready_for_human"
    assert resumed_state["status"] == "ready_for_human"
    assert resumed_state["resume_audit"]["history"][-1]["kind"] == (
        "execution_failure"
    )
    assert resumed_state["resume_audit"]["history"][-1]["failure_code"] == (
        "command_failed"
    )

    final_audit = load_only_run_state(git_repo)["resume_audit"]["history"][-1]
    assert final_audit["kind"] == "execution_failure"
    assert isinstance(final_audit["successor_invocation_started_at"], str)


def test_public_policy_cli_persists_user_defaults_and_shows_resolved_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.chdir(tmp_path)

    assert (
        main(
            [
                "policy",
                "configure",
                "--ticket-review-rounds",
                "2",
                "--parent-only-paired-rounds",
                "7",
                "--run-repair-rounds",
                "6",
                "--review-deadline",
                "90m",
            ]
        )
        == 0
    )
    configured = json.loads(capsys.readouterr().out)
    assert configured["result"] == "configured"
    assert configured["policy"]["ticket_review_rounds"] == 2
    assert configured["policy"]["parent_only_paired_rounds"] == 7
    assert configured["policy"]["run_repair_rounds"] == 6
    assert configured["policy"]["invocation_deadlines"]["review"] == 5400

    assert main(["policy", "show"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["result"] == "policy"
    assert shown["user_defaults"] == {
        "invocation_deadlines": {"review": "90m"},
        "parent_only_paired_rounds": 7,
        "run_repair_rounds": 6,
        "ticket_review_rounds": 2,
    }


def test_invalid_policy_is_rejected_before_run_state_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

    assert main(["run", "1", "--review-deadline", "0s"]) == 2
    assert json.loads(capsys.readouterr().out)["result"] == "error"
    assert not (tmp_path / ".agent-run").exists()


def test_public_operator_docs_describe_the_v01_quickstart() -> None:
    documents = [
        (PROJECT_ROOT / "README.md").read_text(encoding="utf-8"),
        (PROJECT_ROOT / "docs" / "agent-run.md").read_text(encoding="utf-8"),
    ]

    for document in documents:
        assert "./install.sh" in document
        assert "release tag" in document
        assert "agent-run doctor" in document
        assert "auth app configure" in document
        assert "--rollback" in document
        assert "--uninstall" in document
        assert "Linux/WSL" in document
        assert "目标交付仓库" in document


def test_doctor_reports_host_readiness_without_mutating_user_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    for name, output in {
        "git": "git version 2.0\n",
        "codex": "GH_TOKEN=doctor-secret\n",
        "openssl": "OpenSSL 3.0\n",
        "bwrap": "bubblewrap 0.8\n",
    }.items():
        executable = fake_bin / name
        executable.write_text(
            "#!/bin/sh\n"
            f"printf '%s' {output!r}\n",
            encoding="utf-8",
        )
        executable.chmod(0o700)
    gh = fake_bin / "gh"
    gh.write_text(
        "#!/bin/sh\n"
        "[ \"$1\" = --version ] && exit 0\n"
        "[ \"$1\" = auth ] && exit 0\n"
        "exit 1\n",
        encoding="utf-8",
    )
    gh.chmod(0o700)

    home = tmp_path / "home"
    home.mkdir()
    config = home / "config"
    data = home / "data"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    monkeypatch.setenv("XDG_DATA_HOME", str(data))
    monkeypatch.setenv("PATH", str(fake_bin))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        doctor,
        "execution_readiness",
        lambda: {
            "status": "ok",
            "host": "systemd-user",
            "linger_required": False,
            "reason": None,
        },
    )

    assert main(["doctor", "--json"]) == 0

    output_text = capsys.readouterr().out
    output = json.loads(output_text)
    assert output["result"] == "doctor"
    assert output["status"] == "issues"
    assert output["checks"]["git"]["status"] == "ok"
    assert output["checks"]["codex"]["status"] == "ok"
    assert output["checks"]["openssl"]["status"] == "ok"
    assert output["checks"]["bubblewrap"]["status"] == "ok"
    assert output["checks"]["github"]["logged_in"] is True
    assert output["checks"]["active_runner"]["status"] == "missing"
    assert output["checks"]["path"]["agent_run"] is None
    assert output["checks"]["worker_read_provider"] == {
        "provider": "host",
        "status": "ok",
    }
    assert output["installation_readiness"]["status"] == "issues"
    assert output["installation_readiness"]["checks"] == [
        "python",
        "codex",
        "active_runner",
        "path",
    ]
    assert output["execution_readiness"]["status"] == "ok"
    assert output["execution_readiness"]["host"] == "systemd-user"
    assert output["execution_readiness"]["linger_required"] is False
    assert "doctor-secret" not in output_text
    assert not config.exists()
    assert not data.exists()


@pytest.mark.parametrize(
    ("openssl_script", "expected_status"),
    [
        (None, "missing"),
        (
            '#!/bin/sh\n[ "$1" = version ] && exit 7\nexit 0\n',
            "unavailable",
        ),
        (
            f"#!{sys.executable}\nimport time\ntime.sleep(30)\n",
            "timeout",
        ),
    ],
)
def test_doctor_classifies_bounded_openssl_probe_states(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    openssl_script: str | None,
    expected_status: str,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    if openssl_script is not None:
        openssl = fake_bin / "openssl"
        openssl.write_text(openssl_script, encoding="utf-8")
        openssl.chmod(0o700)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("PATH", str(fake_bin))
    monkeypatch.chdir(tmp_path)

    assert main(["doctor", "--json"]) == 0

    output = json.loads(capsys.readouterr().out)
    assert output["checks"]["openssl"]["status"] == expected_status


def test_doctor_does_not_fallback_when_app_profile_is_invalid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "config" / "agent-run"
    config.mkdir(parents=True)
    profile = config / "github-app.json"
    profile.write_text("{not-json}\n", encoding="utf-8")
    profile.chmod(0o600)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config.parent))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("PATH", "")
    monkeypatch.chdir(tmp_path)

    assert main(["doctor", "--json"]) == 0

    output_text = capsys.readouterr().out
    output = json.loads(output_text)
    assert output["checks"]["worker_read_provider"] == {
        "provider": "app",
        "status": "invalid",
    }
    assert "not-json" not in output_text


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("app_id", "9" * 5000),
        ("installation_id", "9" * 5000),
        ("app_id", "not-a-number"),
        ("installation_id", "-1"),
    ],
)
def test_doctor_reports_invalid_app_identifiers_without_unbounded_conversion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    field: str,
    value: str,
) -> None:
    config = tmp_path / "config" / "agent-run"
    config.mkdir(parents=True)
    config.chmod(0o700)
    profile = {
        "app_id": "123",
        "installation_id": "456",
        "private_key_path": str(tmp_path / "secret-key.pem"),
    }
    profile[field] = value
    profile_path = config / "github-app.json"
    profile_path.write_text(json.dumps(profile), encoding="utf-8")
    profile_path.chmod(0o600)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config.parent))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("PATH", "")
    monkeypatch.chdir(tmp_path)

    assert main(["doctor", "--json"]) == 0

    captured = capsys.readouterr()
    output_text = captured.out
    output = json.loads(output_text)
    assert {
        "python",
        "git",
        "codex",
        "github",
        "openssl",
        "bubblewrap",
        "active_runner",
        "path",
    } <= output["checks"].keys()
    assert output["checks"]["worker_read_provider"] == {
        "provider": "app",
        "status": "invalid",
    }
    assert "command_failed" not in output_text
    assert "blocked" not in output_text
    assert "Traceback" not in output_text
    assert "secret-key.pem" not in output_text
    assert "Traceback" not in captured.err


def test_doctor_detects_a_non_managed_agent_run_path_collision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    conflicting_entry = fake_bin / "agent-run"
    conflicting_entry.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    conflicting_entry.chmod(0o700)
    home = tmp_path / "home"
    home.mkdir()
    user_bin = home / ".local" / "bin"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("PATH", os.pathsep.join((str(fake_bin), str(user_bin))))
    monkeypatch.chdir(tmp_path)

    assert main(["doctor", "--json"]) == 0

    output = json.loads(capsys.readouterr().out)
    assert output["checks"]["path"] == {
        "agent_run": str(conflicting_entry),
        "status": "conflict",
        "user_bin_on_path": True,
    }


@pytest.mark.parametrize(
    ("path_state", "human_detail"),
    [
        ("ok", "agent-run 可用"),
        ("needs_refresh", "需要刷新登录 shell"),
        ("conflict", "检测到非受管同名入口"),
        ("invalid", "受管入口无效"),
        ("missing", "未找到 agent-run 入口"),
    ],
)
def test_doctor_classifies_managed_path_states_and_human_messages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    path_state: str,
    human_detail: str,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    user_bin = home / ".local" / "bin"
    managed_target = home / "data" / "agent-run" / "active" / "current" / "bin" / "agent-run"
    managed_target.parent.mkdir(parents=True)
    managed_target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    managed_target.chmod(0o700)
    stable_entry = user_bin / "agent-run"

    if path_state in {"ok", "needs_refresh", "conflict"}:
        user_bin.mkdir(parents=True)
        stable_entry.symlink_to(managed_target)
    elif path_state == "invalid":
        user_bin.mkdir(parents=True)
        stable_entry.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        stable_entry.chmod(0o700)

    path_entries: list[str] = []
    if path_state == "conflict":
        conflict_bin = tmp_path / "conflict-bin"
        conflict_bin.mkdir()
        conflicting_entry = conflict_bin / "agent-run"
        conflicting_entry.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        conflicting_entry.chmod(0o700)
        path_entries.append(str(conflict_bin))
    if path_state == "ok" or path_state == "invalid":
        path_entries.append(str(user_bin))

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / "data"))
    monkeypatch.setenv("PATH", os.pathsep.join(path_entries))
    monkeypatch.chdir(tmp_path)

    assert main(["doctor", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["checks"]["path"]["status"] == path_state

    assert main(["doctor"]) == 0
    human = capsys.readouterr().out
    assert human_detail in human
    if path_state in {"conflict", "invalid"}:
        assert "agent-run 可用" not in human


def test_doctor_reports_a_corrupt_app_key_as_invalid_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("openssl is required to exercise corrupt key validation")
    key = tmp_path / "app.pem"
    key.write_text("not-a-private-key\n", encoding="utf-8")
    key.chmod(0o600)
    config = tmp_path / "config" / "agent-run"
    config.mkdir(parents=True)
    (config / "github-app.json").write_text(
        json.dumps(
            {
                "app_id": "123",
                "installation_id": "456",
                "private_key_path": str(key),
            }
        ),
        encoding="utf-8",
    )
    (config / "github-app.json").chmod(0o600)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config.parent))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("PATH", str(Path(openssl).parent))
    monkeypatch.chdir(tmp_path)

    assert main(["doctor", "--json"]) == 0

    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "issues"
    assert output["checks"]["worker_read_provider"] == {
        "provider": "app",
        "status": "invalid",
    }


def test_doctor_rejects_a_zero_exit_openssl_probe_without_a_signature(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    openssl = fake_bin / "openssl"
    openssl.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    openssl.chmod(0o700)
    key = tmp_path / "app.pem"
    key.write_text("not-used-by-fake-openssl\n", encoding="utf-8")
    key.chmod(0o600)
    config = tmp_path / "config" / "agent-run"
    config.mkdir(parents=True)
    config.chmod(0o700)
    profile = config / "github-app.json"
    profile.write_text(
        json.dumps(
            {
                "app_id": "123",
                "installation_id": "456",
                "private_key_path": str(key),
            }
        ),
        encoding="utf-8",
    )
    profile.chmod(0o600)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config.parent))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("PATH", str(fake_bin))
    monkeypatch.chdir(tmp_path)

    assert main(["doctor", "--json"]) == 0

    output_text = capsys.readouterr().out
    output = json.loads(output_text)
    assert output["checks"]["worker_read_provider"] == {
        "provider": "app",
        "status": "invalid",
    }
    assert "not-used-by-fake-openssl" not in output_text


def test_doctor_reports_a_broken_managed_entry_as_invalid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    stable_entry = home / ".local" / "bin" / "agent-run"
    target = home / "data" / "agent-run" / "active" / "current" / "bin" / "agent-run"
    target.parent.mkdir(parents=True)
    target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    target.chmod(0o700)
    stable_entry.parent.mkdir(parents=True)
    stable_entry.symlink_to(target)
    target.unlink()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / "data"))
    monkeypatch.setenv("PATH", str(stable_entry.parent))
    monkeypatch.chdir(tmp_path)

    assert main(["doctor", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["checks"]["path"]["status"] == "invalid"


def test_doctor_accepts_a_real_private_key_signature(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("openssl is required to exercise valid key validation")
    key = tmp_path / "app.pem"
    subprocess.run(
        [openssl, "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048", "-out", str(key)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    key.chmod(0o600)
    config = tmp_path / "config" / "agent-run"
    config.mkdir(parents=True)
    config.chmod(0o700)
    profile = config / "github-app.json"
    profile.write_text(
        json.dumps(
            {
                "app_id": "123",
                "installation_id": "456",
                "private_key_path": str(key),
            }
        ),
        encoding="utf-8",
    )
    profile.chmod(0o600)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config.parent))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("PATH", str(Path(openssl).parent))
    monkeypatch.chdir(tmp_path)

    assert main(["doctor", "--json"]) == 0

    output = json.loads(capsys.readouterr().out)
    assert output["checks"]["worker_read_provider"] == {
        "provider": "app",
        "status": "ok",
    }


def test_doctor_reaps_a_timed_out_dependency_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    child_pid = tmp_path / "child.pid"
    probe = tmp_path / "git"
    probe.write_text(
        f"#!{sys.executable}\n"
        "import subprocess\n"
        "import time\n"
        "from pathlib import Path\n"
        f"child = subprocess.Popen([{sys.executable!r}, '-c', 'import time; time.sleep(30)'])\n"
        f"Path({str(child_pid)!r}).write_text(str(child.pid))\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    probe.chmod(0o700)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.chdir(tmp_path)

    started = time.monotonic()
    assert main(["doctor", "--json"]) == 0
    elapsed = time.monotonic() - started
    output = json.loads(capsys.readouterr().out)

    assert elapsed < _DOCTOR_TIMEOUT_TEST_LIMIT_SECONDS
    assert output["checks"]["git"]["status"] == "timeout"
    child = int(child_pid.read_text(encoding="utf-8"))
    for _ in range(20):
        proc_stat = Path(f"/proc/{child}/stat")
        if not proc_stat.exists():
            break
        process_state = proc_stat.read_text(encoding="utf-8").split()[2]
        if process_state == "Z":
            break
        time.sleep(0.05)
    else:
        pytest.fail("timed-out doctor probe left its child process running")


def test_doctor_reaps_descendants_after_a_probe_exits_normally(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    child_pid = tmp_path / "child.pid"
    probe = tmp_path / "git"
    probe.write_text(
        f"#!{sys.executable}\n"
        "import subprocess\n"
        "from pathlib import Path\n"
        f"child = subprocess.Popen([{sys.executable!r}, '-c', 'import time; time.sleep(30)'])\n"
        f"Path({str(child_pid)!r}).write_text(str(child.pid))\n",
        encoding="utf-8",
    )
    probe.chmod(0o700)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.chdir(tmp_path)

    assert main(["doctor", "--json"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["checks"]["git"]["status"] == "ok"
    child = int(child_pid.read_text(encoding="utf-8"))
    for _ in range(20):
        proc_stat = Path(f"/proc/{child}/stat")
        if not proc_stat.exists():
            break
        process_state = proc_stat.read_text(encoding="utf-8").split()[2]
        if process_state == "Z":
            break
        time.sleep(0.05)
    else:
        pytest.fail("normally completed doctor probe left its child process running")


def test_status_exposes_current_candidate_and_pr_in_top_level_and_budget(
    capsys: pytest.CaptureFixture[str],
) -> None:
    state: dict[str, object] = {
        "run_id": "run-1",
        "status": "active",
        "active_ticket_job": {
            "phase": "candidate",
            "ticket_number": 3,
            "candidate_sha": "CANDIDATE-1",
            "pr_number": 17,
            "review_budget": _canonical_run_budget(),
        },
        "diagnostics": [],
    }

    cli.cli_presentation._print_status(state, as_json=True)
    output = json.loads(capsys.readouterr().out)

    assert output["candidate_sha"] == "CANDIDATE-1"
    assert output["pr_number"] == 17
    assert output["review_budget"]["candidate_sha"] == "CANDIDATE-1"
    assert output["review_budget"]["pr_number"] == 17


def test_status_distinguishes_semantic_invocation_output_budget_and_publication_retry(
    capsys: pytest.CaptureFixture[str],
) -> None:
    attempt = {
        "attempt_id": "attempt-publication-2",
        "role": "publication",
        "work_subject": "ticket:3",
        "generation": 2,
        "currentness_boundary_fingerprint": "sha256:boundary",
        "ordinal": 2,
        "budget_window": None,
        "status": "pending",
    }
    invocation = {
        "work_subject": "ticket:3",
        "role": "publication",
        "status": "failed",
        "attempt_count": 3,
        "started_at": "2026-08-24T00:00:00+00:00",
        "semantic_attempt": deepcopy(attempt),
    }
    state: dict[str, object] = {
        "run_id": "run-1",
        "status": "execution_failed",
        "active_ticket_job": {
            "ticket_number": 3,
            "phase": "publication_pending",
            "pending_semantic_attempt": deepcopy(attempt),
            "publication_operation_retry": {"attempts": 2, "limit": 4},
        },
        "active_agent_invocation": invocation,
        "diagnostics": [],
    }

    cli.cli_presentation._print_status(state, as_json=True)
    output = json.loads(capsys.readouterr().out)

    assert output["semantic_agent_attempt"] == attempt
    assert output["agent_invocation"] == invocation
    assert output["output_attempt"] == {
        "invocation_started_at": "2026-08-24T00:00:00+00:00",
        "attempt_count": 3,
    }
    assert output["budget_window"] is None
    assert output["publication_operation_retry"] == {
        "semantic_attempt_id": "attempt-publication-2",
        "work_subject": "ticket:3",
        "attempts": 2,
        "limit": 4,
    }

    cli.cli_presentation._print_status(state, as_json=False)
    human = capsys.readouterr().out
    assert "Status:     执行失败，可恢复" in human
    assert "Ticket #3" in human
    assert "Semantic Agent Attempt" not in human
    assert "attempt-publication-2" not in human
    assert "sha256:boundary" not in human


def test_history_deduplicates_attempt_mirrors_and_projects_each_counter(
    capsys: pytest.CaptureFixture[str],
) -> None:
    completed = {
        "attempt_id": "attempt-development-1",
        "role": "development",
        "work_subject": "ticket:3",
        "generation": 1,
        "currentness_boundary_fingerprint": "sha256:first",
        "ordinal": 1,
        "budget_window": 1,
        "status": "completed",
        "budget_consumed": True,
        "outcome": "candidate",
    }
    pending = {
        "attempt_id": "attempt-reviewer-1",
        "role": "reviewer",
        "work_subject": "ticket:3",
        "generation": 1,
        "currentness_boundary_fingerprint": "sha256:second",
        "ordinal": 1,
        "budget_window": 1,
        "status": "pending",
    }
    invocation = {
        "work_subject": "ticket:3",
        "role": "reviewer",
        "status": "failed",
        "attempt_count": 2,
        "started_at": "2026-08-24T00:01:00+00:00",
        "semantic_attempt": deepcopy(pending),
    }
    mirrored_job = {
        "ticket_number": 3,
        "phase": "reviewing",
        "semantic_attempt_history": [deepcopy(completed)],
        "pending_semantic_attempt": deepcopy(pending),
        "publication_operation_retry": {"attempts": 1, "limit": 3},
    }
    state: dict[str, object] = {
        "run_id": "run-1",
        "status": "execution_failed",
        "timeline": [
            {
                "at": "2026-08-24T00:01:00+00:00",
                "kind": "ticket_phase",
                "status": "execution_failed",
                "semantic_attempt_id": "attempt-reviewer-1",
                "semantic_attempt_role": "reviewer",
                "semantic_attempt_ordinal": 1,
                "budget_window": 1,
                "agent_invocation_started_at": "2026-08-24T00:01:00+00:00",
                "agent_invocation_status": "failed",
                "output_attempt": 2,
                "publication_operation_retry_attempts": 1,
                "publication_operation_retry_limit": 3,
            }
        ],
        "active_ticket_job": deepcopy(mirrored_job),
        "ticket_jobs": {"3": deepcopy(mirrored_job)},
        "agent_invocation_history": [invocation],
        "diagnostics": [],
    }

    cli.cli_presentation._print_history(state, as_json=True)
    output = json.loads(capsys.readouterr().out)

    assert output["semantic_agent_attempts"] == [completed, pending]
    assert output["agent_invocations"] == [invocation]
    assert output["output_attempts"] == [
        {
            "invocation_started_at": "2026-08-24T00:01:00+00:00",
            "work_subject": "ticket:3",
            "attempt_count": 2,
        }
    ]
    assert output["budget_windows"] == [
        {"work_subject": "ticket:3", "role": "development", "window": 1},
        {"work_subject": "ticket:3", "role": "reviewer", "window": 1},
    ]
    assert output["publication_operation_retries"] == [
        {
            "semantic_attempt_id": None,
            "work_subject": "ticket:3",
            "attempts": 1,
            "limit": 3,
        }
    ]

    cli.cli_presentation._print_history(state, as_json=False)
    human = capsys.readouterr().out
    assert "Review Agent 第 1 轮" in human
    assert "Semantic Agent Attempt" not in human
    assert "attempt-reviewer-1" not in human
    assert "sha256:second" not in human


def test_timeline_projects_semantic_invocation_output_and_retry_counters(
    tmp_path: Path,
) -> None:
    attempt = {
        "attempt_id": "attempt-publication-2",
        "role": "publication",
        "work_subject": "ticket:3",
        "generation": 2,
        "currentness_boundary_fingerprint": "sha256:boundary",
        "ordinal": 2,
        "budget_window": None,
        "status": "pending",
    }
    state: dict[str, Any] = {
        "run_id": "run-1",
        "status": "execution_failed",
        "active_ticket_job": {
            "ticket_number": 3,
            "phase": "publication_pending",
            "pending_semantic_attempt": attempt,
            "publication_operation_retry": {"attempts": 2, "limit": 4},
        },
        "active_agent_invocation": {
            "work_subject": "ticket:3",
            "role": "publication",
            "status": "failed",
            "attempt_count": 1,
            "started_at": "2026-08-24T00:00:00+00:00",
            "semantic_attempt": deepcopy(attempt),
        },
        "timeline": [],
    }
    store = StateStore(tmp_path)

    store.save_run("run-1", state)
    state["active_agent_invocation"]["attempt_count"] = 2
    store.save_run("run-1", state)

    timeline = store.load_run("run-1")["timeline"]
    assert [event["output_attempt"] for event in timeline] == [1, 2]
    assert timeline[-1] | {"at": "ignored"} == {
        "at": "ignored",
        "kind": "ticket_phase",
        "status": "execution_failed",
        "ticket": 3,
        "worker": "发布工作代理",
        "phase": "publication_pending",
        "semantic_attempt_id": "attempt-publication-2",
        "semantic_attempt_role": "publication",
        "semantic_attempt_ordinal": 2,
        "budget_window": None,
        "agent_invocation_started_at": "2026-08-24T00:00:00+00:00",
        "agent_invocation_status": "failed",
        "output_attempt": 2,
        "publication_operation_retry_attempts": 2,
        "publication_operation_retry_limit": 4,
    }


def test_status_exposes_preserved_dirty_checkout_and_recovery_action(
    capsys: pytest.CaptureFixture[str],
) -> None:
    state: dict[str, object] = {
        "run_id": "run-1",
        "status": "completed",
        "diagnostics": [],
        "delivery_cleanup": {
            "status": "cleanup_pending",
            "last_error": "preserved dirty checkout",
            "items": {
                "agent-run/ticket-3": {
                    "kind": "ticket",
                    "branch": "agent-run/ticket-3",
                    "checkout": "/repo/.agent-run/worktrees/run-1/ticket-3",
                    "attempts": 3,
                    "status": "cleanup_pending",
                    "last_error": "tracked modifications; agent-run resume run-1",
                }
            },
        },
    }

    cli.cli_presentation._print_status(state, as_json=True)
    output = json.loads(capsys.readouterr().out)

    assert output["delivery_cleanup"] == {
        "status": "cleanup_pending",
        "last_error": "preserved dirty checkout",
        "items": [
            {
                "kind": "ticket",
                "branch": "agent-run/ticket-3",
                "checkout": "/repo/.agent-run/worktrees/run-1/ticket-3",
                "status": "cleanup_pending",
                "last_error": "tracked modifications; agent-run resume run-1",
                "recovery_action": "agent-run resume run-1",
            }
        ],
    }
    assert output["next_action"] == "agent-run resume run-1"

    cli.cli_presentation._print_status(state, as_json=False)
    human = capsys.readouterr().out
    assert "已保留 1 个受管工作区" in human
    assert "完整诊断与恢复操作见 --json" in human
    assert "/repo/.agent-run/worktrees/run-1/ticket-3" not in human
    assert "tracked modifications" not in human
    assert "run-1" not in human


def test_operator_action_keeps_repository_names_starting_with_run(
    capsys: pytest.CaptureFixture[str],
) -> None:
    print_operator_action(
        {
            "type": "Human Blocker",
            "object": "Ticket #2",
            "phase": "candidate",
            "reasons": ["需要操作者确认"],
            "trigger_invocation": None,
            "preserved": "当前状态与已有审计证据",
            "next_action": (
                "agent-run resume 1 --repo run-1-1234567890abcdef/project"
            ),
        }
    )

    output = capsys.readouterr().out
    assert "agent-run resume 1 --repo run-1-1234567890abcdef/project" in output
    assert "<run-id>/project" not in output
    assert human_next_action(
        "agent-run resume run-1-1234567890abcdef-2",
        run_id="run-1-1234567890abcdef-2",
    ) == "agent-run resume <run-id>"
    print_operator_action(
        {
            "type": "Deterministic Contradiction",
            "object": "Ticket #2",
            "phase": "blocked",
            "reasons": ["Candidate mismatch"],
            "trigger_invocation": None,
            "preserved": "当前状态与已有审计证据",
            "next_action": "agent-run abandon run-1-1234567890abcdef-2",
        },
        run_id="run-1-1234567890abcdef-2",
    )
    contradiction = capsys.readouterr().out
    assert "agent-run abandon <run-id>" in contradiction
    assert "run-1-1234567890abcdef-2" not in contradiction


def test_history_matches_responses_and_findings_to_their_subject_and_window(
    capsys: pytest.CaptureFixture[str],
) -> None:
    def artifact(finding: str) -> dict[str, object]:
        return {
            "checks": {
                "e2e": {"findings": [finding]},
                "standards": {"findings": []},
                "spec": {"findings": []},
            }
        }

    state: dict[str, object] = {
        "run_id": "run-1-1234567890abcdef",
        "repository": "example/project",
        "parent": {"number": 1, "title": "Parent spec"},
        "created_at": "2026-08-30T00:00:00+00:00",
        "status": "active",
        "timeline": [],
        "ticket_jobs": {
            "2": {
                "human_response_history": [{"response": "response-for-ticket-2"}],
                "review_budget_history": [
                    {
                        "review_budget": {
                            "window": 1,
                            "review_artifacts": [{"artifact": artifact("old-window")}],
                        }
                    }
                ],
                "review_budget": {
                    "window": 2,
                    "review_artifacts": [{"artifact": artifact("current-window")}],
                },
            },
            "3": {
                "human_response_history": [{"response": "response-for-ticket-3"}],
                "review_budget_history": [],
                "review_budget": {"window": 1, "review_artifacts": []},
            },
        },
        "agent_invocation_history": [
            {
                "started_at": "2026-08-30T00:01:00+00:00",
                "ended_at": "2026-08-30T00:02:00+00:00",
                "status": "completed",
                "phase": "candidate",
                "work_subject": "ticket:2",
                "invocation_role": "reviewer",
                "semantic_attempt": {
                    "role": "reviewer",
                    "ordinal": 1,
                    "budget_window": 2,
                },
            }
        ],
        "resume_audit": {
            "history": [
                {
                    "requested_at": "2026-08-30T00:03:00+00:00",
                    "work_subject": "ticket:3",
                    "source_status": "ready_for_human",
                    "human_response_supplied": True,
                },
                {
                    "requested_at": "2026-08-30T00:04:00+00:00",
                    "work_subject": "ticket:2",
                    "source_status": "ready_for_human",
                    "human_response_supplied": True,
                },
            ]
        },
        "diagnostics": [],
    }

    cli.cli_presentation._print_history(state, as_json=True)
    output = json.loads(capsys.readouterr().out)
    review = next(event for event in output["events"] if event["kind"] == "review")
    resumes = [event for event in output["events"] if event["kind"] == "resume"]

    assert review["details"] == ["current-window"]
    assert [(event["object"], event["details"]) for event in resumes] == [
        ("Ticket #3", ["response-for-ticket-3"]),
        ("Ticket #2", ["response-for-ticket-2"]),
    ]


def test_terminal_status_and_history_share_a_stable_elapsed_time(
    capsys: pytest.CaptureFixture[str],
) -> None:
    state: dict[str, object] = {
        "run_id": "run-1-1234567890abcdef",
        "repository": "example/project",
        "parent": {"number": 1, "title": "Parent spec"},
        "created_at": "2026-08-30T00:00:00+00:00",
        "status": "completed",
        "timeline": [
            {
                "at": "2026-08-30T00:00:10+00:00",
                "kind": "run_status",
                "status": "completed",
            }
        ],
        "diagnostics": [],
    }

    cli.cli_presentation._print_status(state, as_json=True)
    first_status = json.loads(capsys.readouterr().out)
    cli.cli_presentation._print_status(state, as_json=True)
    second_status = json.loads(capsys.readouterr().out)
    cli.cli_presentation._print_history(state, as_json=True)
    history = json.loads(capsys.readouterr().out)

    assert first_status["elapsed_seconds"] == 10
    assert second_status["elapsed_seconds"] == 10
    assert history["summary"]["elapsed_seconds"] == 10


def test_history_does_not_assign_a_new_generation_response_to_an_old_resume(
    capsys: pytest.CaptureFixture[str],
) -> None:
    state: dict[str, object] = {
        "run_id": "run-1-1234567890abcdef",
        "repository": "example/project",
        "parent": {"number": 1, "title": "Parent spec"},
        "created_at": "2026-08-30T00:00:00+00:00",
        "status": "active",
        "timeline": [],
        "ticket_jobs": {
            "2": {
                "human_response_history": [
                    {"generation": 2, "response": "new-generation-response"}
                ]
            }
        },
        "resume_audit": {
            "history": [
                {
                    "requested_at": "2026-08-30T00:01:00+00:00",
                    "work_subject": "ticket:2",
                    "generation": 1,
                    "source_status": "ready_for_human",
                    "human_response_supplied": True,
                },
                {
                    "requested_at": "2026-08-30T00:02:00+00:00",
                    "work_subject": "ticket:2",
                    "generation": 2,
                    "source_status": "ready_for_human",
                    "human_response_supplied": True,
                },
            ]
        },
        "diagnostics": [],
    }

    cli.cli_presentation._print_history(state, as_json=True)
    output = json.loads(capsys.readouterr().out)
    resumes = [event for event in output["events"] if event["kind"] == "resume"]

    assert [event["details"] for event in resumes] == [
        [],
        ["new-generation-response"],
    ]


def test_status_labels_the_latest_agent_with_its_own_ticket(
    capsys: pytest.CaptureFixture[str],
) -> None:
    state: dict[str, object] = {
        "run_id": "run-1-1234567890abcdef",
        "repository": "example/project",
        "parent": {"number": 1, "title": "Parent spec"},
        "created_at": "2026-08-30T00:00:00+00:00",
        "status": "active",
        "active_ticket_job": {"ticket_number": 3, "phase": "developing"},
        "ticket_jobs": {
            "2": {"ticket_number": 2, "phase": "completed"},
            "3": {"ticket_number": 3, "phase": "developing"},
        },
        "agent_invocation_history": [
            {
                "started_at": "2026-08-30T00:01:00+00:00",
                "ended_at": "2026-08-30T00:02:00+00:00",
                "status": "completed",
                "work_subject": "ticket:2",
                "role": "publication",
                "model": "fixture-agent",
                "reasoning_effort": "high",
            }
        ],
        "diagnostics": [],
    }

    cli.cli_presentation._print_status(state, as_json=False)
    output = capsys.readouterr().out

    assert "当前对象:   Ticket #3" in output
    assert "最近 Agent: Publication Agent · Ticket #2" in output
    assert "Publication Agent · Ticket #3" not in output


def test_status_distinguishes_stale_dirty_checkout_from_resumable_work(
    capsys: pytest.CaptureFixture[str],
) -> None:
    checkout = "/repo/.agent-run/worktrees/run-1/run-repair"
    state: dict[str, object] = {
        "run_id": "run-1",
        "parent": {"number": 1},
        "status": "run_acceptance_pending",
        "diagnostics": [],
        "delivery_cleanup": {
            "status": "cleanup_pending",
            "last_error": "untracked files",
            "items": {
                "agent-run-repair/run-1/1": {
                    "kind": "run_repair",
                    "branch": "agent-run-repair/run-1/1",
                    "checkout": checkout,
                    "attempts": 0,
                    "status": "cleanup_pending",
                    "last_error": "untracked files",
                    "recovery_kind": "stale_dirty_checkout",
                }
            },
        },
    }

    cli.cli_presentation._print_status(state, as_json=True)
    output = json.loads(capsys.readouterr().out)

    recovery = output["delivery_cleanup"]["items"][0]["recovery_action"]
    assert f"copy/salvage {checkout}" in recovery
    assert "run 1 to retire it and continue fresh Run Acceptance" in recovery
    assert "abandon run-1 --discard-worktree" in recovery
    assert output["next_action"].startswith("先检查并把 stale")
    assert "agent-run run 1" in output["next_action"]


@pytest.mark.parametrize("command", ["deliver", "accept-run", "publish-run"])
def test_removed_stage_commands_are_rejected_by_the_real_cli(command: str) -> None:
    environment = os.environ.copy()
    source_path = str(PROJECT_ROOT / "src")
    environment["PYTHONPATH"] = (
        source_path
        if not environment.get("PYTHONPATH")
        else f"{source_path}{os.pathsep}{environment['PYTHONPATH']}"
    )
    result = subprocess.run(
        [sys.executable, "-m", "agent_run", command, "run-id"],
        cwd=PROJECT_ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "invalid choice" in result.stderr


def test_removed_promotion_command_is_rejected_by_the_public_cli(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(PROJECT_ROOT)

    with pytest.raises(SystemExit) as error:
        main(["promotion-handshake", "not-a-sha"])

    assert error.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_source_runner_fixture_allows_lifecycle_commands_without_promotion_gate(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    monkeypatch.chdir(git_repo)

    assert main(["start", "1", "--github-fixture", str(fixture)]) == 0


def test_source_checkout_cannot_run_production_lifecycle_without_active_runner(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(git_repo)

    assert main(["start", "1", "--repo", "example/project"]) == 2

    output = json.loads(capsys.readouterr().out)
    assert output["result"] == "error"
    assert output["status"] == "blocked"
    assert not (git_repo / ".agent-run").exists()


def test_production_run_without_a_real_executor_host_fails_before_writes(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class UnavailableSystemdHost:
        def __init__(self, **_options: object) -> None:
            pass

        def check_readiness(self, *, command: object = None) -> None:
            raise cli.ExecutionReadinessError("user systemd unavailable")

    monkeypatch.chdir(git_repo)
    monkeypatch.setenv("XDG_DATA_HOME", str(git_repo / "runner-data"))
    monkeypatch.setattr(cli, "_running_active_runner", lambda: True)
    monkeypatch.setattr(cli, "SystemdUserExecutorHost", UnavailableSystemdHost)
    monkeypatch.setattr(
        cli,
        "FakeExecutorHost",
        lambda: pytest.fail("production run must not construct the fixture host"),
    )

    assert main(["run", "1", "--repo", "example/project"]) == 2

    output = json.loads(capsys.readouterr().out)
    assert output["diagnostics"][0]["code"] == "execution_readiness"
    assert not (git_repo / ".agent-run").exists()


def test_production_run_defers_environment_capture_to_action_admission(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class AvailableSystemdHost:
        def __init__(self, **_options: object) -> None:
            self.readiness_commands: list[object] = []
            self.prepared_commands: list[tuple[str, ...]] = []
            hosts.append(self)

        def check_readiness(self, *, command: object = None) -> None:
            self.readiness_commands.append(command)

        def prepare_environment(self, command: Sequence[str]) -> None:
            self.prepared_commands.append(tuple(command))
            raise cli.SystemdExecutionReadinessError("发起终端环境过大")

    hosts: list[AvailableSystemdHost] = []

    def enter_lifecycle(*_args: object, **options: object) -> None:
        prepare = options.get("prepare_executor_session")
        assert callable(prepare)
        prepare()
        pytest.fail("environment rejection must stop lifecycle admission")

    monkeypatch.chdir(git_repo)
    monkeypatch.setenv("XDG_DATA_HOME", str(git_repo / "runner-data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(git_repo / "runner-state"))
    monkeypatch.setattr(cli, "_running_active_runner", lambda: True)
    monkeypatch.setattr(cli, "SystemdUserExecutorHost", AvailableSystemdHost)
    monkeypatch.setattr(cli, "_run_lifecycle", enter_lifecycle)

    assert main(["run", "1", "--repo", "example/project"]) == 2

    output = json.loads(capsys.readouterr().out)
    assert output["diagnostics"][0]["code"] == "execution_readiness"
    assert len(hosts) == 1
    assert hosts[0].readiness_commands == [None]
    assert hosts[0].prepared_commands == [("run", "1", "--repo", "example/project")]
    assert not (git_repo / ".agent-run" / "task-control").exists()


def issue(
    number: int,
    *,
    state: str = "OPEN",
    labels: list[str] | None = None,
    blocked_by: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "number": number,
        "title": f"Ticket {number}",
        "body": f"Implement ticket {number}.",
        "state": state,
        "labels": labels if labels is not None else ["ready-for-agent"],
        "blocked_by": blocked_by or [],
    }


def run_cli(
    repo: Path,
    fixture: Path,
    *arguments: str,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    source_path = str(PROJECT_ROOT / "src")
    environment["PYTHONPATH"] = (
        source_path
        if not environment.get("PYTHONPATH")
        else f"{source_path}{os.pathsep}{environment['PYTHONPATH']}"
    )
    environment.setdefault("XDG_STATE_HOME", str(repo / ".agent-run-test-state"))
    if extra_env:
        environment.update(extra_env)
    return subprocess.run(
        [sys.executable, "-m", "agent_run", *arguments, "--github-fixture", str(fixture)],
        cwd=repo,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


def run_policy_cli(
    repo: Path,
    *arguments: str,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    source_path = str(PROJECT_ROOT / "src")
    environment["PYTHONPATH"] = (
        source_path
        if not environment.get("PYTHONPATH")
        else f"{source_path}{os.pathsep}{environment['PYTHONPATH']}"
    )
    if extra_env:
        environment.update(extra_env)
    return subprocess.run(
        [sys.executable, "-m", "agent_run", "policy", *arguments],
        cwd=repo,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


def run_internal_stage(
    repo: Path, fixture: Path, stage: str, run_id: str, *arguments: str
) -> subprocess.CompletedProcess[str]:
    """Exercise a legacy stage's engine seam without reviving its CLI command."""

    agent_fixture = (
        Path(arguments[arguments.index("--agent-fixture") + 1])
        if "--agent-fixture" in arguments
        else fixture
    )
    crash_after_save = (
        int(arguments[arguments.index("--crash-after-save") + 1])
        if "--crash-after-save" in arguments
        else None
    )
    git = GitRepository.discover(repo)
    states = (
        FaultInjectingStateStore(
            repo / ".agent-run", crash_after_save=crash_after_save
        )
        if crash_after_save is not None
        else StateStore(repo / ".agent-run")
    )
    reader = FixtureGitHubReader(fixture)
    controller = Controller(reader, git, states)
    operations = DirectRunOperations(
        controller=controller,
        states=states,
        git=git,
        github_reader=reader,
        publisher_factory=lambda: FixtureGitHubPublisher(fixture, git),
        agents=FixtureAgentBackend(agent_fixture),
    )
    step = {
        "deliver": RunStep.DELIVER,
        "accept-run": RunStep.ACCEPT,
        "publish-run": RunStep.PUBLISH,
    }[stage]
    current = states.load_current_run(run_id)
    invocation = current.get("active_agent_invocation") if isinstance(current, dict) else None
    if (
        isinstance(current, dict)
        and current.get("status") == "execution_failed"
        and isinstance(invocation, dict)
        and invocation.get("status") in {"failed", "completed"}
    ):
        controller.resume(run_id)
    try:
        state = operations.dispatch(step, run_id).state
    except (
        CodexProcessError,
        GitHubReadError,
        OSError,
        ValueError,
        WorkerSandboxError,
    ) as error:
        assert controller.record_execution_failure(run_id, str(error))
        state = states.load_current_run(run_id)
        assert state is not None
    active_ticket = state.get("active_ticket_job")
    output = {
        "result": "resumed",
        "run_id": state["run_id"],
        "status": state["status"],
        "run_branch": state.get("run_branch", state.get("parent_branch")),
        "active_ticket": (
            active_ticket.get("ticket_number")
            if isinstance(active_ticket, dict)
            else None
        ),
        "diagnostics": state.get("diagnostics", []),
        "scope_change": state.get("unsupported_scope_change"),
        "next_action": cli.cli_presentation._next_action(state),
    }
    return subprocess.CompletedProcess(
        args=["internal-stage", stage, run_id],
        returncode=0 if state.get("status") in cli._SUCCESSFUL_FOREGROUND_STATUSES else 2,
        stdout=json.dumps(output, ensure_ascii=False),
        stderr="",
    )


def git_fetch_failure_wrapper(
    directory: Path, *, failures: int
) -> tuple[Path, dict[str, str]]:
    real_git = shutil.which("git")
    assert real_git is not None
    counter = directory / "git-fetch-failures"
    counter.write_text(str(failures), encoding="utf-8")
    wrapper = directory / "git"
    wrapper.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"fetch\" ] && [ \"$(cat \"$AGENT_RUN_FETCH_COUNTER\")\" -gt 0 ]; then\n"
        "  remaining=$(cat \"$AGENT_RUN_FETCH_COUNTER\")\n"
        "  echo $((remaining - 1)) > \"$AGENT_RUN_FETCH_COUNTER\"\n"
        "  echo 'dial tcp: i/o timeout' >&2\n"
        "  exit 1\n"
        "fi\n"
        "exec \"$AGENT_RUN_REAL_GIT\" \"$@\"\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    return counter, {
        "AGENT_RUN_FETCH_COUNTER": str(counter),
        "AGENT_RUN_REAL_GIT": real_git,
        "PATH": f"{directory}{os.pathsep}{os.environ['PATH']}",
    }


def stdout_json(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    assert result.stdout, result.stderr
    loaded: object = json.loads(result.stdout)
    assert isinstance(loaded, dict)
    return loaded


def load_only_run_state(repo: Path) -> dict[str, Any]:
    run_files = list((repo / ".agent-run" / "runs").glob("*.json"))
    assert len(run_files) == 1
    loaded: object = json.loads(run_files[0].read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def failed_invocation(
    *,
    work_subject: str,
    role: str,
    phase: str,
    generation: int = 1,
    status: str = "failed",
) -> dict[str, Any]:
    semantic_role = (
        "development"
        if role == "development"
        else "reviewer" if role in {"fresh_acceptance", "reviewer"} else "publication"
    )
    boundary_fingerprint = canonical_fingerprint({})
    identity = {
        "role": semantic_role,
        "work_subject": work_subject,
        "generation": generation,
        "currentness_boundary_fingerprint": boundary_fingerprint,
        "ordinal": 1,
        "budget_window": 1 if semantic_role in {"development", "reviewer"} else None,
    }
    semantic_attempt = {
        "attempt_id": canonical_fingerprint(identity),
        **identity,
        "status": "pending",
    }
    return {
        "work_subject": work_subject,
        "generation": generation,
        "role": role,
        "phase": phase,
        "mode": "fresh",
        "input_fingerprint": "fixture",
        "currentness_boundary": {},
        "semantic_attempt": semantic_attempt,
        "status": status,
        "requested_thread_id": None,
        "reported_thread_id": None,
        "attempt_count": 1,
        "started_at": "2026-08-13T00:00:00+00:00",
        "ended_at": "2026-08-13T00:00:01+00:00",
        "error": "fixture failure",
        "return_code": 1,
        "signal": None,
    }


def test_start_creates_one_run_branch_and_resume_is_idempotent(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"2": issue(2), "3": issue(3)},
    )

    first = run_cli(git_repo, fixture, "start", "1")
    assert first.returncode == 0, first.stderr
    first_output = stdout_json(first)
    state = load_only_run_state(git_repo)

    second = run_cli(git_repo, fixture, "start", "1")
    assert second.returncode == 0, second.stderr
    second_output = stdout_json(second)

    assert first_output["result"] == "started"
    assert second_output["result"] == "resumed"
    assert first_output["run_id"] == second_output["run_id"] == state["run_id"]
    assert state["parent"]["number"] == 1
    assert state["base"]["branch"] == "main"
    assert "schema_version" not in state
    assert state["active_agent_invocation"] is None
    assert state["agent_invocation_history"] == []
    assert state["ticket_graph"]["ordered_ticket_numbers"] == [2, 3]
    assert state["frontier"] == [2, 3]
    assert state["active_ticket_job"]["ticket_number"] == 2
    branches = subprocess.run(
        ["git", "for-each-ref", "--format=%(refname:short)", "refs/heads/agent-run/"],
        cwd=git_repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.splitlines()
    assert branches == [state["run_branch"]]
    assert len(list((git_repo / ".agent-run" / "runs").glob("*.json"))) == 1


def test_start_contract_documents_its_managed_run_branch_side_effect(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"2": issue(2)})

    started = run_cli(git_repo, fixture, "start", "1")

    assert started.returncode == 0, started.stderr
    state = load_only_run_state(git_repo)
    branches = subprocess.run(
        ["git", "branch", "--format=%(refname:short)"],
        cwd=git_repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.splitlines()
    assert state["run_branch"] in branches
    assert "受管 Run Branch" in build_parser().format_help()
    for path in (PROJECT_ROOT / "README.md", PROJECT_ROOT / "docs" / "agent-run.md", PROJECT_ROOT / "CONTEXT.md"):
        assert "受管 Run Branch" in path.read_text(encoding="utf-8")


def test_run_repair_docs_describe_job_rotation_and_candidate_promotion() -> None:
    context = (PROJECT_ROOT / "CONTEXT.md").read_text(encoding="utf-8")
    operator_guide = (PROJECT_ROOT / "docs" / "agent-run.md").read_text(
        encoding="utf-8"
    )

    assert "一个 Cycle 可依次包含多个 Run Repair Job" in context
    assert "轮转出后继 Job" in context
    assert "Candidate Run Acceptance → 严格 promotion" in operator_guide
    assert "归档旧 Job/PR 并轮转新的 branch/PR" in operator_guide
    assert "一个活跃 Run Repair Job 实现一个 Repair Cycle" not in context
    assert "Run Repair → fresh Run Acceptance → 新 PR" not in operator_guide


def test_status_and_history_locate_a_new_run_from_an_unrelated_directory(
    git_repo: Path, tmp_path: Path
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    locator_home = tmp_path / "locator-home"
    locator_env = {"XDG_STATE_HOME": str(locator_home)}
    started = run_cli(git_repo, fixture, "start", "1", extra_env=locator_env)
    run_id = stdout_json(started)["run_id"]
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=elsewhere, check=True)

    status = run_cli(
        elsewhere, fixture, "status", run_id, "--json", extra_env=locator_env
    )
    history = run_cli(
        elsewhere, fixture, "history", run_id, "--json", extra_env=locator_env
    )

    assert status.returncode == history.returncode == 0
    assert stdout_json(status)["run_id"] == run_id
    assert stdout_json(history)["run_id"] == run_id
    assert not (elsewhere / ".agent-run").exists()
    locator = json.loads(
        (locator_home / "agent-run" / "run-locator.json").read_text(encoding="utf-8")
    )
    assert locator == {
        "entries": [
            {
                "run_id": run_id,
                "repository_root": str(git_repo.resolve()),
                "state_dir": str((git_repo / ".agent-run").resolve()),
                "updated_at": locator["entries"][0]["updated_at"],
            }
        ]
    }


def test_status_and_history_do_not_initialize_online_dependencies(
    git_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    locator_home = tmp_path / "locator-home"
    locator_env = {"XDG_STATE_HOME": str(locator_home)}
    run_id = stdout_json(
        run_cli(git_repo, fixture, "start", "1", extra_env=locator_env)
    )["run_id"]

    def unavailable(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("offline command initialized an online dependency")

    monkeypatch.chdir(git_repo)
    monkeypatch.setenv("XDG_STATE_HOME", str(locator_home))
    monkeypatch.setattr(cli, "SystemdUserExecutorHost", unavailable)
    monkeypatch.setattr(cli, "CodexCliBackend", unavailable)
    monkeypatch.setattr(cli, "GhGitHubReader", unavailable)

    assert main(["status", run_id, "--json"]) == 0
    assert main(["history", run_id, "--json"]) == 0
    output = capsys.readouterr().out.splitlines()
    assert json.loads(output[-2])["run_id"] == run_id
    assert json.loads(output[-1])["run_id"] == run_id


def test_new_runs_from_separate_clones_have_distinct_locator_ids(
    git_repo: Path, tmp_path: Path
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    locator_env = {"XDG_STATE_HOME": str(tmp_path / "locator-home")}
    first_id = stdout_json(
        run_cli(git_repo, fixture, "start", "1", extra_env=locator_env)
    )["run_id"]
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "--quiet", str(git_repo), str(clone)], check=True)
    clone_fixture = write_fixture(clone / "github.json", issues={})

    second = run_cli(clone, clone_fixture, "start", "1", extra_env=locator_env)

    assert second.returncode == 0, second.stderr
    second_id = stdout_json(second)["run_id"]
    assert second_id != first_id
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    assert run_cli(
        elsewhere, fixture, "status", first_id, "--json", extra_env=locator_env
    ).returncode == 0
    assert run_cli(
        elsewhere, clone_fixture, "history", second_id, "--json", extra_env=locator_env
    ).returncode == 0


def test_status_prefers_current_directory_state_and_explicit_state_dir(
    git_repo: Path, tmp_path: Path
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    locator_env = {"XDG_STATE_HOME": str(tmp_path / "locator-home")}
    run_id = stdout_json(
        run_cli(git_repo, fixture, "start", "1", extra_env=locator_env)
    )["run_id"]
    local_repo = tmp_path / "local-repo"
    local_repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=local_repo, check=True)
    local_state_dir = local_repo / ".agent-run" / "runs"
    local_state_dir.mkdir(parents=True)
    local_state = load_only_run_state(git_repo)
    local_state["diagnostics"] = [{"code": "local_priority", "message": "local"}]
    (local_state_dir / f"{run_id}.json").write_text(
        json.dumps(local_state), encoding="utf-8"
    )
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    local = run_cli(
        local_repo, fixture, "status", run_id, "--json", extra_env=locator_env
    )
    explicit = run_cli(
        elsewhere,
        fixture,
        "status",
        run_id,
        "--state-dir",
        str(git_repo / ".agent-run"),
        "--json",
        extra_env=locator_env,
    )

    assert stdout_json(local)["diagnostics"][0]["code"] == "local_priority"
    assert stdout_json(explicit)["diagnostics"] == []


@pytest.mark.parametrize("command", ["status", "history"])
def test_read_only_locator_errors_are_actionable_and_do_not_mutate_run_history(
    git_repo: Path, tmp_path: Path, command: str
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    locator_home = tmp_path / "locator-home"
    locator_env = {"XDG_STATE_HOME": str(locator_home)}
    run_id = stdout_json(
        run_cli(git_repo, fixture, "start", "1", extra_env=locator_env)
    )["run_id"]
    state_path = next((git_repo / ".agent-run" / "runs").glob("*.json"))
    before = state_path.read_text(encoding="utf-8")
    locator_path = locator_home / "agent-run" / "run-locator.json"
    locator_path.parent.mkdir(parents=True, exist_ok=True)
    locator_path.write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "run_id": run_id,
                        "repository_root": str(git_repo.resolve()),
                        "state_dir": str(tmp_path / "missing-state"),
                        "updated_at": "2026-08-15T00:00:00+00:00",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    result = run_cli(
        elsewhere, fixture, command, run_id, "--json", extra_env=locator_env
    )

    assert result.returncode == 2
    output = stdout_json(result)
    assert output["status"] == "blocked"
    assert output["diagnostics"][0]["code"] == "run_locator_stale"
    assert "--state-dir" in output["diagnostics"][0]["message"]
    assert state_path.read_text(encoding="utf-8") == before


def test_missing_and_conflicting_locators_return_dedicated_read_only_errors(
    git_repo: Path, tmp_path: Path
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    locator_home = tmp_path / "locator-home"
    locator_env = {"XDG_STATE_HOME": str(locator_home)}
    run_id = stdout_json(
        run_cli(git_repo, fixture, "start", "1", extra_env=locator_env)
    )["run_id"]
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    locator_path = locator_home / "agent-run" / "run-locator.json"
    locator_path.unlink()

    missing = run_cli(
        elsewhere, fixture, "status", run_id, "--json", extra_env=locator_env
    )

    assert stdout_json(missing)["diagnostics"][0]["code"] == "run_locator_missing"
    assert not locator_path.exists()
    source_state = next((git_repo / ".agent-run" / "runs").glob("*.json")).read_text(
        encoding="utf-8"
    )
    first_state_dir = tmp_path / "first-state"
    second_state_dir = tmp_path / "second-state"
    for state_dir in (first_state_dir, second_state_dir):
        (state_dir / "runs").mkdir(parents=True)
        (state_dir / "runs" / f"{run_id}.json").write_text(
            source_state, encoding="utf-8"
        )
    locator_path.parent.mkdir(parents=True, exist_ok=True)
    locator_path.write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "run_id": run_id,
                        "repository_root": str(git_repo.resolve()),
                        "state_dir": str(first_state_dir),
                        "updated_at": "2026-08-15T00:00:00+00:00",
                    },
                    {
                        "run_id": run_id,
                        "repository_root": str(git_repo.resolve()),
                        "state_dir": str(second_state_dir),
                        "updated_at": "2026-08-15T00:00:01+00:00",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    conflict = run_cli(
        elsewhere, fixture, "history", run_id, "--json", extra_env=locator_env
    )

    assert conflict.returncode == 2
    assert stdout_json(conflict)["diagnostics"][0]["code"] == "run_locator_conflict"


@pytest.mark.parametrize(
    ("command", "arguments"),
    [
        ("run", ("1",)),
        ("resume", ("{run_id}",)),
        ("requeue", ("{run_id}",)),
        ("approve", ("{run_id}",)),
        ("revise", ("{run_id}", "--message", "feedback")),
        ("abandon", ("{run_id}",)),
    ],
)
def test_lifecycle_commands_do_not_use_cross_directory_locator(
    git_repo: Path,
    tmp_path: Path,
    command: str,
    arguments: tuple[str, ...],
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    locator_env = {"XDG_STATE_HOME": str(tmp_path / "locator-home")}
    run_id = stdout_json(
        run_cli(git_repo, fixture, "start", "1", extra_env=locator_env)
    )["run_id"]
    state_path = next((git_repo / ".agent-run" / "runs").glob("*.json"))
    before = state_path.read_text(encoding="utf-8")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    command_arguments = tuple(argument.format(run_id=run_id) for argument in arguments)

    result = run_cli(
        elsewhere, fixture, command, *command_arguments, extra_env=locator_env
    )

    assert result.returncode == 2
    assert stdout_json(result)["diagnostics"][0]["code"] == "command_failed"
    assert state_path.read_text(encoding="utf-8") == before


def test_status_and_history_select_the_unique_current_run_without_run_id(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    subprocess.run(
        ["git", "remote", "add", "origin", "git@github.com:example/project.git"],
        cwd=git_repo,
        check=True,
    )
    started = run_cli(git_repo, fixture, "start", "1")
    run_id = stdout_json(started)["run_id"]

    status = run_cli(git_repo, fixture, "status", "--json")
    history = run_cli(git_repo, fixture, "history", "--json")

    assert status.returncode == history.returncode == 0
    assert stdout_json(status)["run_id"] == run_id
    assert stdout_json(history)["run_id"] == run_id


def test_parent_selector_works_in_a_repo_and_across_directories(
    git_repo: Path, tmp_path: Path
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    subprocess.run(
        ["git", "remote", "add", "origin", "git@github.com:example/project.git"],
        cwd=git_repo,
        check=True,
    )
    locator_env = {"XDG_STATE_HOME": str(tmp_path / "locator-home")}
    run_id = stdout_json(
        run_cli(git_repo, fixture, "start", "1", extra_env=locator_env)
    )["run_id"]
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    local = run_cli(
        git_repo, fixture, "status", "--parent", "1", "--json", extra_env=locator_env
    )
    cross_directory = run_cli(
        elsewhere,
        fixture,
        "history",
        "--repo",
        "example/project",
        "--parent",
        "1",
        "--json",
        extra_env=locator_env,
    )

    assert local.returncode == cross_directory.returncode == 0
    assert stdout_json(local)["run_id"] == run_id
    assert stdout_json(cross_directory)["run_id"] == run_id


def test_parent_selector_fails_closed_when_same_repository_has_multiple_clones(
    git_repo: Path, tmp_path: Path
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    locator_env = {"XDG_STATE_HOME": str(tmp_path / "locator-home")}
    subprocess.run(
        ["git", "remote", "add", "origin", "git@github.com:example/project.git"],
        cwd=git_repo,
        check=True,
    )
    first_id = stdout_json(
        run_cli(git_repo, fixture, "start", "1", extra_env=locator_env)
    )["run_id"]

    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "--quiet", str(git_repo), str(clone)], check=True)
    subprocess.run(
        ["git", "remote", "set-url", "origin", "git@github.com:example/project.git"],
        cwd=clone,
        check=True,
    )
    clone_fixture = write_fixture(clone / "github.json", issues={})
    second_id = stdout_json(
        run_cli(clone, clone_fixture, "start", "1", extra_env=locator_env)
    )["run_id"]

    result = run_cli(
        git_repo,
        fixture,
        "status",
        "--parent",
        "1",
        "--json",
        extra_env=locator_env,
    )

    assert result.returncode == 2
    diagnostic = stdout_json(result)["diagnostics"][0]
    assert diagnostic["code"] == "run_selector_ambiguous"
    assert {candidate["run_id"] for candidate in diagnostic["candidates"]} == {
        first_id,
        second_id,
    }


def test_repository_parent_selector_does_not_collide_on_same_issue_number(
    git_repo: Path, tmp_path: Path
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    subprocess.run(
        ["git", "remote", "add", "origin", "git@github.com:example/project.git"],
        cwd=git_repo,
        check=True,
    )
    locator_env = {"XDG_STATE_HOME": str(tmp_path / "locator-home")}
    first_id = stdout_json(
        run_cli(git_repo, fixture, "start", "1", extra_env=locator_env)
    )["run_id"]

    other_repo = tmp_path / "other-repo"
    subprocess.run(["git", "clone", "--quiet", str(git_repo), str(other_repo)], check=True)
    subprocess.run(
        ["git", "remote", "set-url", "origin", "git@github.com:other/project.git"],
        cwd=other_repo,
        check=True,
    )
    other_fixture = write_fixture(
        other_repo / "github.json", issues={}, repository="other/project"
    )
    second_id = stdout_json(
        run_cli(other_repo, other_fixture, "start", "1", extra_env=locator_env)
    )["run_id"]
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    first = run_cli(
        elsewhere,
        fixture,
        "status",
        "--repo",
        "example/project",
        "--parent",
        "1",
        "--json",
        extra_env=locator_env,
    )
    second = run_cli(
        elsewhere,
        other_fixture,
        "history",
        "--repo",
        "other/project",
        "--parent",
        "1",
        "--json",
        extra_env=locator_env,
    )

    assert first.returncode == second.returncode == 0
    assert stdout_json(first)["run_id"] == first_id
    assert stdout_json(second)["run_id"] == second_id


def test_repository_parent_selector_ignores_unrelated_stale_locator(
    git_repo: Path, tmp_path: Path
) -> None:
    fixture = write_fixture(
        git_repo / "github.json", issues={}, repository="a/project"
    )
    subprocess.run(
        ["git", "remote", "add", "origin", "git@github.com:a/project.git"],
        cwd=git_repo,
        check=True,
    )
    locator_env = {"XDG_STATE_HOME": str(tmp_path / "locator-home")}
    first_id = stdout_json(
        run_cli(git_repo, fixture, "start", "1", extra_env=locator_env)
    )["run_id"]
    first_state_path = next((git_repo / ".agent-run" / "runs").glob("*.json"))

    other_repo = tmp_path / "other-repo"
    subprocess.run(["git", "clone", "--quiet", str(git_repo), str(other_repo)], check=True)
    subprocess.run(
        ["git", "remote", "set-url", "origin", "git@github.com:b/project.git"],
        cwd=other_repo,
        check=True,
    )
    other_fixture = write_fixture(
        other_repo / "github.json", issues={}, repository="b/project"
    )
    second_id = stdout_json(
        run_cli(other_repo, other_fixture, "start", "1", extra_env=locator_env)
    )["run_id"]
    second_state_path = other_repo / ".agent-run" / "runs" / f"{second_id}.json"
    second_state_path.unlink()
    locator_path = tmp_path / "locator-home" / "agent-run" / "run-locator.json"
    before_first_state = first_state_path.read_text(encoding="utf-8")
    before_locator = locator_path.read_text(encoding="utf-8")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    status = run_cli(
        elsewhere,
        fixture,
        "status",
        "--repo",
        "a/project",
        "--parent",
        "1",
        "--json",
        extra_env=locator_env,
    )
    history = run_cli(
        elsewhere,
        fixture,
        "history",
        "--repo",
        "a/project",
        "--parent",
        "1",
        "--json",
        extra_env=locator_env,
    )

    assert status.returncode == history.returncode == 0
    assert stdout_json(status)["run_id"] == first_id
    assert stdout_json(history)["run_id"] == first_id

    for command in ("status", "history"):
        unrelated = run_cli(
            elsewhere,
            other_fixture,
            command,
            "--repo",
            "b/project",
            "--parent",
            "1",
            "--json",
            extra_env=locator_env,
        )

        assert unrelated.returncode == 2
        diagnostic = stdout_json(unrelated)["diagnostics"][0]
        assert diagnostic["code"] == "run_locator_stale"
    assert first_state_path.read_text(encoding="utf-8") == before_first_state
    assert locator_path.read_text(encoding="utf-8") == before_locator
    assert not second_state_path.exists()


def test_runs_discovers_bounded_human_run_candidates(
    git_repo: Path, tmp_path: Path
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    subprocess.run(
        ["git", "remote", "add", "origin", "git@github.com:example/project.git"],
        cwd=git_repo,
        check=True,
    )
    locator_env = {"XDG_STATE_HOME": str(tmp_path / "locator-home")}
    run_id = stdout_json(
        run_cli(git_repo, fixture, "start", "1", extra_env=locator_env)
    )["run_id"]
    state = load_only_run_state(git_repo)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    discovered = run_cli(
        elsewhere,
        fixture,
        "runs",
        "--repo",
        "example/project",
        "--json",
        extra_env=locator_env,
    )

    assert discovered.returncode == 0, discovered.stderr
    output = stdout_json(discovered)
    assert output["result"] == "runs"
    assert output["repository"] == "example/project"
    assert output["runs"] == [
        {
            "parent": 1,
            "repository": "example/project",
            "repository_root": str(git_repo.resolve()),
            "run_id": run_id,
            "started_at": state["created_at"],
            "state_dir": str((git_repo / ".agent-run").resolve()),
            "status": state["status"],
        }
    ]


def test_runs_with_default_state_dir_reports_the_checkout_root(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    subprocess.run(
        ["git", "remote", "add", "origin", "git@github.com:example/project.git"],
        cwd=git_repo,
        check=True,
    )
    run_id = stdout_json(run_cli(git_repo, fixture, "start", "1"))["run_id"]

    discovered = run_cli(
        git_repo,
        fixture,
        "runs",
        "--state-dir",
        str(git_repo / ".agent-run"),
        "--json",
    )

    assert discovered.returncode == 0, discovered.stderr
    candidate = stdout_json(discovered)["runs"][0]
    assert candidate["run_id"] == run_id
    assert candidate["repository_root"] == str(git_repo.resolve())
    assert candidate["state_dir"] == str((git_repo / ".agent-run").resolve())


def test_runs_with_registered_custom_state_dir_reports_the_checkout_root(
    git_repo: Path, tmp_path: Path
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    subprocess.run(
        ["git", "remote", "add", "origin", "git@github.com:example/project.git"],
        cwd=git_repo,
        check=True,
    )
    custom_state_dir = tmp_path / "custom-state"
    locator_env = {"XDG_STATE_HOME": str(tmp_path / "locator-home")}
    run_id = stdout_json(
        run_cli(
            git_repo,
            fixture,
            "start",
            "1",
            "--state-dir",
            str(custom_state_dir),
            extra_env=locator_env,
        )
    )["run_id"]

    discovered = run_cli(
        git_repo,
        fixture,
        "runs",
        "--state-dir",
        str(custom_state_dir),
        "--json",
        extra_env=locator_env,
    )

    assert discovered.returncode == 0, discovered.stderr
    candidate = stdout_json(discovered)["runs"][0]
    assert candidate["run_id"] == run_id
    assert candidate["repository_root"] == str(git_repo.resolve())
    assert candidate["state_dir"] == str(custom_state_dir.resolve())


def test_registered_state_dir_without_checkout_identity_is_unavailable(
    git_repo: Path, tmp_path: Path
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    custom_state_dir = tmp_path / "custom-state"
    locator_home = tmp_path / "locator-home"
    locator_env = {"XDG_STATE_HOME": str(locator_home)}
    run_id = stdout_json(
        run_cli(
            git_repo,
            fixture,
            "start",
            "1",
            "--state-dir",
            str(custom_state_dir),
            extra_env=locator_env,
        )
    )["run_id"]
    state_path = custom_state_dir / "runs" / f"{run_id}.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state.pop("checkout_identity", None)
    state_path.write_text(json.dumps(state), encoding="utf-8")
    locator_path = locator_home / "agent-run" / "run-locator.json"
    before_state = state_path.read_text(encoding="utf-8")
    before_locator = locator_path.read_text(encoding="utf-8")

    discovered = run_cli(
        git_repo,
        fixture,
        "runs",
        "--state-dir",
        str(custom_state_dir),
        "--json",
        extra_env=locator_env,
    )

    assert discovered.returncode == 0, discovered.stderr
    candidate = stdout_json(discovered)["runs"][0]
    assert candidate["run_id"] == run_id
    assert candidate["repository_root"] == "unavailable"
    for command in ("status", "history"):
        result = run_cli(
            git_repo,
            fixture,
            command,
            "--parent",
            "1",
            "--state-dir",
            str(custom_state_dir),
            "--json",
            extra_env=locator_env,
        )
        assert result.returncode == 2
        diagnostic = stdout_json(result)["diagnostics"][0]
        assert diagnostic["code"] == "run_locator_stale"
    assert state_path.read_text(encoding="utf-8") == before_state
    assert locator_path.read_text(encoding="utf-8") == before_locator


def test_explicit_repository_run_without_origin_remains_selectable(
    git_repo: Path, tmp_path: Path
) -> None:
    fixture = write_fixture(
        git_repo / "github.json", issues={}, repository="a/project"
    )
    locator_env = {"XDG_STATE_HOME": str(tmp_path / "locator-home")}

    started = run_cli(
        git_repo,
        fixture,
        "start",
        "1",
        "--repo",
        "a/project",
        extra_env=locator_env,
    )

    assert started.returncode == 0, started.stderr
    run_id = stdout_json(started)["run_id"]
    state_path = next((git_repo / ".agent-run" / "runs").glob("*.json"))
    locator_path = tmp_path / "locator-home" / "agent-run" / "run-locator.json"
    before_state = state_path.read_text(encoding="utf-8")
    before_locator = locator_path.read_text(encoding="utf-8")

    status = run_cli(
        git_repo, fixture, "status", "--json", extra_env=locator_env
    )
    history = run_cli(
        git_repo,
        fixture,
        "history",
        "--parent",
        "1",
        "--json",
        extra_env=locator_env,
    )

    assert status.returncode == history.returncode == 0
    assert stdout_json(status)["run_id"] == run_id
    assert stdout_json(history)["run_id"] == run_id
    assert state_path.read_text(encoding="utf-8") == before_state
    assert locator_path.read_text(encoding="utf-8") == before_locator


def test_runs_with_unknown_custom_state_dir_marks_worktree_unavailable(
    git_repo: Path, tmp_path: Path
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    run_id = stdout_json(run_cli(git_repo, fixture, "start", "1"))["run_id"]
    source = git_repo / ".agent-run" / "runs" / f"{run_id}.json"
    custom_state_dir = tmp_path / "custom-state"
    custom_runs = custom_state_dir / "runs"
    custom_runs.mkdir(parents=True)
    shutil.copy2(source, custom_runs / source.name)
    before = (custom_runs / source.name).read_text(encoding="utf-8")

    discovered = run_cli(
        git_repo,
        fixture,
        "runs",
        "--state-dir",
        str(custom_state_dir),
        "--json",
    )

    assert discovered.returncode == 0, discovered.stderr
    candidate = stdout_json(discovered)["runs"][0]
    assert candidate["run_id"] == run_id
    assert candidate["repository_root"] == "unavailable"
    assert candidate["state_dir"] == str(custom_state_dir.resolve())
    assert (custom_runs / source.name).read_text(encoding="utf-8") == before


def test_runs_with_nested_dot_agent_run_state_dir_marks_worktree_unavailable(
    git_repo: Path
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    run_id = stdout_json(run_cli(git_repo, fixture, "start", "1"))["run_id"]
    source = git_repo / ".agent-run" / "runs" / f"{run_id}.json"
    custom_state_dir = git_repo / "nested" / ".agent-run"
    custom_runs = custom_state_dir / "runs"
    custom_runs.mkdir(parents=True)
    shutil.copy2(source, custom_runs / source.name)
    before = (custom_runs / source.name).read_text(encoding="utf-8")

    discovered = run_cli(
        git_repo,
        fixture,
        "runs",
        "--state-dir",
        str(custom_state_dir),
        "--json",
    )

    assert discovered.returncode == 0, discovered.stderr
    candidate = stdout_json(discovered)["runs"][0]
    assert candidate["run_id"] == run_id
    assert candidate["repository_root"] == "unavailable"
    assert candidate["state_dir"] == str(custom_state_dir.resolve())
    assert (custom_runs / source.name).read_text(encoding="utf-8") == before


def test_locator_root_reuse_by_another_repository_fails_closed_and_preserves_inputs(
    git_repo: Path, tmp_path: Path
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    subprocess.run(
        ["git", "remote", "add", "origin", "git@github.com:example/project.git"],
        cwd=git_repo,
        check=True,
    )
    locator_home = tmp_path / "locator-home"
    locator_env = {"XDG_STATE_HOME": str(locator_home)}
    custom_state_dir = tmp_path / "custom-state"
    run_id = stdout_json(
        run_cli(
            git_repo,
            fixture,
            "start",
            "1",
            "--state-dir",
            str(custom_state_dir),
            extra_env=locator_env,
        )
    )["run_id"]
    state_path = custom_state_dir / "runs" / f"{run_id}.json"
    locator_path = locator_home / "agent-run" / "run-locator.json"
    before_state = state_path.read_text(encoding="utf-8")
    before_locator = locator_path.read_text(encoding="utf-8")

    original = tmp_path / "original"
    git_repo.rename(original)
    subprocess.run(["git", "clone", "--quiet", str(original), str(git_repo)], check=True)
    subprocess.run(
        ["git", "remote", "set-url", "origin", "git@github.com:other/project.git"],
        cwd=git_repo,
        check=True,
    )
    replacement_fixture = write_fixture(
        git_repo / "github.json", issues={}, repository="other/project"
    )
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    discovered = run_cli(
        elsewhere,
        replacement_fixture,
        "runs",
        "--state-dir",
        str(custom_state_dir),
        "--json",
        extra_env=locator_env,
    )

    assert discovered.returncode == 0, discovered.stderr
    candidate = stdout_json(discovered)["runs"][0]
    assert candidate["run_id"] == run_id
    assert candidate["repository_root"] == "unavailable"
    for command in ("status", "history"):
        result = run_cli(
            git_repo,
            replacement_fixture,
            command,
            "--parent",
            "1",
            "--json",
            extra_env=locator_env,
        )
        assert result.returncode == 2
        diagnostic = stdout_json(result)["diagnostics"][0]
        assert diagnostic["code"] == "run_locator_stale"
    assert state_path.read_text(encoding="utf-8") == before_state
    assert locator_path.read_text(encoding="utf-8") == before_locator
    assert not (git_repo / ".agent-run").exists()


def test_same_repository_replacement_clone_fails_closed_for_all_selectors(
    git_repo: Path, tmp_path: Path
) -> None:
    from cli_fixtures import run_agents

    fixture = write_fixture(git_repo / "github.json", issues={})
    subprocess.run(
        ["git", "remote", "add", "origin", "git@github.com:example/project.git"],
        cwd=git_repo,
        check=True,
    )
    locator_home = tmp_path / "locator-home"
    locator_env = {"XDG_STATE_HOME": str(locator_home)}
    custom_state_dir = tmp_path / "custom-state"
    run_id = stdout_json(
        run_cli(
            git_repo,
            fixture,
            "start",
            "1",
            "--state-dir",
            str(custom_state_dir),
            extra_env=locator_env,
        )
    )["run_id"]
    state_path = custom_state_dir / "runs" / f"{run_id}.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state.update(
        {
            "status": "supervision_timeout",
            "terminal_kind": "supervision_timeout",
            "supervision_wait": {"resume_status": "waiting_external"},
        }
    )
    state_path.write_text(json.dumps(state), encoding="utf-8")
    locator_path = locator_home / "agent-run" / "run-locator.json"
    before_state = state_path.read_text(encoding="utf-8")
    before_locator = locator_path.read_text(encoding="utf-8")

    original = tmp_path / "original"
    git_repo.rename(original)
    subprocess.run(["git", "clone", "--quiet", str(original), str(git_repo)], check=True)
    replacement_fixture = write_fixture(
        git_repo / "github.json", issues={}, repository="example/project"
    )
    agents = run_agents(git_repo / "agents.json")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    discovered = run_cli(
        elsewhere,
        replacement_fixture,
        "runs",
        "--state-dir",
        str(custom_state_dir),
        "--json",
        extra_env=locator_env,
    )

    assert discovered.returncode == 0, discovered.stderr
    candidate = stdout_json(discovered)["runs"][0]
    assert candidate["run_id"] == run_id
    assert candidate["repository_root"] == "unavailable"
    for command, arguments in (
        ("start", ("1", "--state-dir", str(custom_state_dir))),
        (
            "run",
            (
                "1",
                "--state-dir",
                str(custom_state_dir),
                "--agent-fixture",
                str(agents),
            ),
        ),
        ("status", ("--parent", "1", "--json")),
        ("history", ("--parent", "1", "--json")),
        (
            "resume",
            (
                "1",
                "--state-dir",
                str(custom_state_dir),
                "--agent-fixture",
                str(agents),
            ),
        ),
        (
            "resume",
            (
                run_id,
                "--state-dir",
                str(custom_state_dir),
                "--agent-fixture",
                str(agents),
            ),
        ),
    ):
        result = run_cli(
            git_repo,
            replacement_fixture,
            command,
            *arguments,
            extra_env=locator_env,
        )
        assert result.returncode == 2, result.stderr
        diagnostic = stdout_json(result)["diagnostics"][0]
        assert diagnostic["code"] == "run_locator_stale"
        assert state_path.read_text(encoding="utf-8") == before_state
        assert locator_path.read_text(encoding="utf-8") == before_locator
    assert state_path.read_text(encoding="utf-8") == before_state
    assert locator_path.read_text(encoding="utf-8") == before_locator
    assert not (git_repo / ".agent-run").exists()


def test_selector_does_not_cross_select_a_run_from_another_clone(
    git_repo: Path, tmp_path: Path
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    subprocess.run(
        ["git", "remote", "add", "origin", "git@github.com:example/project.git"],
        cwd=git_repo,
        check=True,
    )
    locator_home = tmp_path / "locator-home"
    locator_env = {"XDG_STATE_HOME": str(locator_home)}
    custom_state_dir = tmp_path / "custom-state"
    run_id = stdout_json(
        run_cli(
            git_repo,
            fixture,
            "start",
            "1",
            "--state-dir",
            str(custom_state_dir),
            extra_env=locator_env,
        )
    )["run_id"]
    state_path = custom_state_dir / "runs" / f"{run_id}.json"
    locator_path = locator_home / "agent-run" / "run-locator.json"
    before_state = state_path.read_text(encoding="utf-8")
    before_locator = locator_path.read_text(encoding="utf-8")

    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "--quiet", str(git_repo), str(clone)], check=True)
    subprocess.run(
        ["git", "remote", "set-url", "origin", "git@github.com:example/project.git"],
        cwd=clone,
        check=True,
    )
    clone_fixture = write_fixture(clone / "github.json", issues={})

    for command in ("status", "history"):
        result = run_cli(
            clone,
            clone_fixture,
            command,
            "--parent",
            "1",
            "--json",
            extra_env=locator_env,
        )
        assert result.returncode == 2, result.stderr
        diagnostic = stdout_json(result)["diagnostics"][0]
        assert diagnostic["code"] == "run_selector_requires_checkout"
    assert state_path.read_text(encoding="utf-8") == before_state
    assert locator_path.read_text(encoding="utf-8") == before_locator
    assert not (clone / ".agent-run").exists()


def test_resume_with_explicit_state_dir_does_not_cross_select_another_clone(
    git_repo: Path, tmp_path: Path
) -> None:
    from cli_fixtures import run_agents

    fixture = write_fixture(git_repo / "github.json", issues={})
    subprocess.run(
        ["git", "remote", "add", "origin", "git@github.com:example/project.git"],
        cwd=git_repo,
        check=True,
    )
    locator_home = tmp_path / "locator-home"
    locator_env = {"XDG_STATE_HOME": str(locator_home)}
    custom_state_dir = tmp_path / "custom-state"
    run_id = stdout_json(
        run_cli(
            git_repo,
            fixture,
            "start",
            "1",
            "--state-dir",
            str(custom_state_dir),
            extra_env=locator_env,
        )
    )["run_id"]
    state_path = custom_state_dir / "runs" / f"{run_id}.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state.update(
        {
            "status": "supervision_timeout",
            "terminal_kind": "supervision_timeout",
            "supervision_wait": {"resume_status": "waiting_external"},
        }
    )
    state_path.write_text(json.dumps(state), encoding="utf-8")
    locator_path = locator_home / "agent-run" / "run-locator.json"
    before_state = state_path.read_text(encoding="utf-8")
    before_locator = locator_path.read_text(encoding="utf-8")

    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "--quiet", str(git_repo), str(clone)], check=True)
    subprocess.run(
        ["git", "remote", "set-url", "origin", "git@github.com:example/project.git"],
        cwd=clone,
        check=True,
    )
    clone_fixture = write_fixture(clone / "github.json", issues={})
    agents = run_agents(clone / "agents.json")

    result = run_cli(
        clone,
        clone_fixture,
        "resume",
        "1",
        "--state-dir",
        str(custom_state_dir),
        "--agent-fixture",
        str(agents),
        extra_env=locator_env,
    )

    assert result.returncode == 2
    diagnostic = stdout_json(result)["diagnostics"][0]
    assert diagnostic["code"] == "run_selector_requires_checkout"
    assert state_path.read_text(encoding="utf-8") == before_state
    assert locator_path.read_text(encoding="utf-8") == before_locator
    assert not (clone / ".agent-run").exists()


def test_parent_selector_fails_closed_with_all_ambiguous_candidates(
    git_repo: Path, tmp_path: Path
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    subprocess.run(
        ["git", "remote", "add", "origin", "git@github.com:example/project.git"],
        cwd=git_repo,
        check=True,
    )
    locator_env = {"XDG_STATE_HOME": str(tmp_path / "locator-home")}
    first = run_cli(git_repo, fixture, "start", "1", extra_env=locator_env)
    first_id = stdout_json(first)["run_id"]
    second = run_cli(
        git_repo, fixture, "start", "1", "--new-run", extra_env=locator_env
    )
    second_id = stdout_json(second)["run_id"]
    state_paths = sorted((git_repo / ".agent-run" / "runs").glob("*.json"))
    before = [path.read_text(encoding="utf-8") for path in state_paths]

    result = run_cli(
        git_repo,
        fixture,
        "status",
        "--parent",
        "1",
        "--json",
        extra_env=locator_env,
    )

    assert result.returncode == 2
    diagnostic = stdout_json(result)["diagnostics"][0]
    assert diagnostic["code"] == "run_selector_ambiguous"
    assert {candidate["run_id"] for candidate in diagnostic["candidates"]} == {
        first_id,
        second_id,
    }
    assert [path.read_text(encoding="utf-8") for path in state_paths] == before


def test_parent_selector_reports_stale_index_candidates_without_mutation(
    git_repo: Path, tmp_path: Path
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    locator_home = tmp_path / "locator-home"
    locator_env = {"XDG_STATE_HOME": str(locator_home)}
    run_id = stdout_json(
        run_cli(git_repo, fixture, "start", "1", extra_env=locator_env)
    )["run_id"]
    state_path = next((git_repo / ".agent-run" / "runs").glob("*.json"))
    before = state_path.read_text(encoding="utf-8")
    locator_path = locator_home / "agent-run" / "run-locator.json"
    locator = json.loads(locator_path.read_text(encoding="utf-8"))
    locator["entries"][0]["state_dir"] = str(tmp_path / "missing-state")
    locator_path.write_text(json.dumps(locator), encoding="utf-8")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    result = run_cli(
        elsewhere,
        fixture,
        "status",
        "--repo",
        "example/project",
        "--parent",
        "1",
        "--json",
        extra_env=locator_env,
    )

    assert result.returncode == 2
    diagnostic = stdout_json(result)["diagnostics"][0]
    assert diagnostic["code"] == "run_locator_stale"
    assert diagnostic["candidates"][0]["run_id"] == run_id
    assert diagnostic["candidates"][0]["state_dir"] == str(tmp_path / "missing-state")
    assert state_path.read_text(encoding="utf-8") == before


def test_resume_parent_selector_never_creates_a_run_when_no_recoverable_match(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})

    result = run_cli(git_repo, fixture, "resume", "1")

    assert result.returncode == 2
    diagnostic = stdout_json(result)["diagnostics"][0]
    assert diagnostic["code"] == "run_selector_not_found"
    assert not (git_repo / ".agent-run").exists()


def test_resume_parent_selector_reuses_one_existing_recoverable_run(
    git_repo: Path,
) -> None:
    from cli_fixtures import run_agents

    fixture = write_fixture(git_repo / "github.json", issues={})
    subprocess.run(
        ["git", "remote", "add", "origin", "git@github.com:example/project.git"],
        cwd=git_repo,
        check=True,
    )
    agents = run_agents(git_repo / "agents.json")
    run_id = stdout_json(run_cli(git_repo, fixture, "start", "1"))["run_id"]
    run_file = next((git_repo / ".agent-run" / "runs").glob("*.json"))
    state = load_only_run_state(git_repo)
    state.update(
        {
            "status": "supervision_timeout",
            "terminal_kind": "supervision_timeout",
            "supervision_wait": {"resume_status": "waiting_external"},
        }
    )
    run_file.write_text(json.dumps(state), encoding="utf-8")

    resumed = run_cli(
        git_repo,
        fixture,
        "resume",
        "1",
        "--repo",
        "example/project",
        "--agent-fixture",
        str(agents),
    )

    assert resumed.returncode == 0, resumed.stderr
    assert stdout_json(resumed)["run_id"] == run_id
    resumed_state = load_only_run_state(git_repo)
    assert resumed_state["run_id"] == run_id
    assert resumed_state["resume_audit"]["total"] == 1
    assert len(list((git_repo / ".agent-run" / "runs").glob("*.json"))) == 1


def test_frontier_uses_native_dependencies_labels_state_and_parent_order(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={
            "9": issue(9, blocked_by=[{"number": 20, "state": "OPEN"}]),
            "8": issue(8, labels=["ready-for-agent", "needs-info"]),
            "7": issue(7, labels=[]),
            "6": issue(6, state="CLOSED"),
            "10": issue(10, blocked_by=[{"number": 20, "state": "CLOSED"}]),
            "5": issue(5),
            "2": issue(2),
        },
    )

    result = run_cli(git_repo, fixture, "start", "1")

    assert result.returncode == 0, result.stderr
    state = load_only_run_state(git_repo)
    assert state["frontier"] == [10, 5, 2]
    assert state["active_ticket_job"] == {
        "ticket_number": 10,
        "selection_reason": "first eligible ticket by parent sub-issue order, then issue number",
    }
    tickets = state["ticket_graph"]["tickets"]
    assert tickets["9"]["eligibility"]["reason"] == "blocked_by_open_issues"
    assert tickets["8"]["eligibility"]["reason"] == "disqualifying_label:needs-info"
    assert tickets["7"]["eligibility"]["reason"] == "missing_ready_for_agent"
    assert tickets["6"]["eligibility"]["reason"] == "ticket_closed"
    assert tickets["10"]["eligibility"]["reason"] == "eligible"


def test_unreliable_sub_issue_order_falls_back_to_issue_number(git_repo: Path) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"9": issue(9), "2": issue(2), "5": issue(5)},
        parent={
            "number": 1,
            "title": "Parent spec",
            "body": "Deliver the ticket set.",
            "sub_issues": [9, 2, 5],
            "sub_issue_order_reliable": False,
        },
    )

    result = run_cli(git_repo, fixture, "start", "1")

    assert result.returncode == 0, result.stderr
    state = load_only_run_state(git_repo)
    assert state["frontier"] == [2, 5, 9]
    assert state["active_ticket_job"]["ticket_number"] == 2


def test_resume_rejects_a_run_without_an_agent_boundary(git_repo: Path) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"2": issue(2)})
    started = run_cli(git_repo, fixture, "start", "1")
    run_id = stdout_json(started)["run_id"]

    resumed = run_cli(git_repo, fixture, "resume", run_id)

    assert resumed.returncode == 2
    assert stdout_json(resumed)["result"] == "resumed"
    assert stdout_json(resumed)["run_id"] == run_id
    assert stdout_json(resumed)["status"] == "active"


def test_run_reports_an_incompatible_legacy_state_without_recording_a_failure(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"2": issue(2)})
    started = run_cli(git_repo, fixture, "start", "1")
    state = load_only_run_state(git_repo)
    state["schema_version"] = 1
    state_path = next((git_repo / ".agent-run" / "runs").glob("*.json"))
    state_path.write_text(json.dumps(state), encoding="utf-8")
    before = deepcopy(state)

    result = run_cli(git_repo, fixture, "run", "1")

    assert result.returncode == 2
    assert stdout_json(result)["status"] == "incompatible_run_state"
    assert stdout_json(result)["diagnostics"][0]["code"] == "incompatible_run_state"
    assert json.loads(state_path.read_text(encoding="utf-8")) == before


@pytest.mark.parametrize("command", ["status", "history"])
def test_status_and_history_show_incompatible_legacy_evidence(
    git_repo: Path, command: str
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"2": issue(2)})
    started = run_cli(git_repo, fixture, "start", "1")
    run_id = stdout_json(started)["run_id"]
    state = load_only_run_state(git_repo)
    state["schema_version"] = 1
    state_path = next((git_repo / ".agent-run" / "runs").glob("*.json"))
    state_path.write_text(json.dumps(state), encoding="utf-8")
    before = deepcopy(state)

    result = run_cli(git_repo, fixture, command, run_id, "--json")

    assert result.returncode == 0
    output = stdout_json(result)
    assert output["run_id"] == run_id
    if command == "status":
        assert output["status"] == before["status"]
    assert json.loads(state_path.read_text(encoding="utf-8")) == before


@pytest.mark.parametrize("command", ["status", "history"])
@pytest.mark.parametrize("timeline", [None, {}, "not an event list"])
def test_status_and_history_reject_an_invalid_timeline_without_mutation(
    git_repo: Path, command: str, timeline: object
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"2": issue(2)})
    run_id = stdout_json(run_cli(git_repo, fixture, "start", "1"))["run_id"]
    state = load_only_run_state(git_repo)
    if timeline is None:
        state.pop("timeline")
    else:
        state["timeline"] = timeline
    state_path = next((git_repo / ".agent-run" / "runs").glob("*.json"))
    state_path.write_text(json.dumps(state), encoding="utf-8")
    before = deepcopy(state)

    result = run_cli(git_repo, fixture, command, run_id, "--json")

    assert result.returncode == 2
    assert stdout_json(result)["status"] == "incompatible_run_state"
    assert json.loads(state_path.read_text(encoding="utf-8")) == before


def test_history_rejects_a_timeline_with_a_non_event_without_mutation(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"2": issue(2)})
    run_id = stdout_json(run_cli(git_repo, fixture, "start", "1"))["run_id"]
    state = load_only_run_state(git_repo)
    state["timeline"] = ["not an event"]
    state_path = next((git_repo / ".agent-run" / "runs").glob("*.json"))
    state_path.write_text(json.dumps(state), encoding="utf-8")
    before = deepcopy(state)

    result = run_cli(git_repo, fixture, "history", run_id, "--json")

    assert result.returncode == 2
    assert stdout_json(result)["status"] == "incompatible_run_state"
    assert json.loads(state_path.read_text(encoding="utf-8")) == before


@pytest.mark.parametrize("command", ["status", "history", "resume"])
def test_cli_rejects_a_malformed_canonical_nested_state(
    git_repo: Path, command: str
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"2": issue(2)})
    run_id = stdout_json(run_cli(git_repo, fixture, "start", "1"))["run_id"]
    state = load_only_run_state(git_repo)
    state["parent"].pop("number")
    state_path = next((git_repo / ".agent-run" / "runs").glob("*.json"))
    state_path.write_text(json.dumps(state), encoding="utf-8")
    before = deepcopy(state)
    arguments = (command, run_id, "--json") if command != "resume" else (command, run_id)

    result = run_cli(git_repo, fixture, *arguments)

    assert result.returncode == 2
    assert stdout_json(result)["status"] == "incompatible_run_state"
    assert stdout_json(result)["diagnostics"][0]["code"] == "incompatible_run_state"
    assert json.loads(state_path.read_text(encoding="utf-8")) == before


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("active_agent_invocation", {"status": ["failed"]}),
        ("agent_invocation_history", ["not an invocation record"]),
    ],
)
def test_status_rejects_malformed_invocation_records(
    git_repo: Path, field: str, value: object
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"2": issue(2)})
    run_id = stdout_json(run_cli(git_repo, fixture, "start", "1"))["run_id"]
    state = load_only_run_state(git_repo)
    state["status"] = "execution_failed"
    state[field] = value
    state_path = next((git_repo / ".agent-run" / "runs").glob("*.json"))
    state_path.write_text(json.dumps(state), encoding="utf-8")
    before = deepcopy(state)

    result = run_cli(git_repo, fixture, "status", run_id, "--json")

    assert result.returncode == 2
    assert stdout_json(result)["status"] == "incompatible_run_state"
    assert json.loads(state_path.read_text(encoding="utf-8")) == before


def test_resume_rejects_unknown_invocation_role_without_mutation(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"2": issue(2)})
    run_id = stdout_json(run_cli(git_repo, fixture, "start", "1"))["run_id"]
    state = load_only_run_state(git_repo)
    state["status"] = "execution_failed"
    state["active_agent_invocation"] = failed_invocation(
        work_subject="ticket:2", role="unknown", phase="developing"
    )
    state_path = next((git_repo / ".agent-run" / "runs").glob("*.json"))
    state_path.write_text(json.dumps(state), encoding="utf-8")
    before = deepcopy(state)

    result = run_cli(git_repo, fixture, "resume", run_id)

    assert result.returncode == 2
    assert stdout_json(result)["status"] == "incompatible_run_state"
    assert json.loads(state_path.read_text(encoding="utf-8")) == before


def test_resume_rejects_active_invocation_for_missing_ticket(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"2": issue(2)})
    run_id = stdout_json(run_cli(git_repo, fixture, "start", "1"))["run_id"]
    state = load_only_run_state(git_repo)
    state["status"] = "execution_failed"
    state["active_agent_invocation"] = failed_invocation(
        work_subject="ticket:999", role="development", phase="developing"
    )
    state_path = next((git_repo / ".agent-run" / "runs").glob("*.json"))
    state_path.write_text(json.dumps(state), encoding="utf-8")
    before = deepcopy(state)

    result = run_cli(git_repo, fixture, "resume", run_id)

    assert result.returncode == 2
    assert stdout_json(result)["status"] == "incompatible_run_state"
    assert json.loads(state_path.read_text(encoding="utf-8")) == before


@pytest.mark.parametrize("owner", [None, {}, {"phase": "unknown"}])
def test_resume_rejects_final_publication_without_a_valid_owner(
    git_repo: Path,
    owner: dict[str, str] | None,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"2": issue(2)})
    run_id = stdout_json(run_cli(git_repo, fixture, "start", "1"))["run_id"]
    state = load_only_run_state(git_repo)
    state.update(
        {
            "status": "execution_failed",
            "run_acceptance": {"acceptance_generation": 1},
            **({"run_publication": owner} if owner is not None else {}),
            "active_agent_invocation": failed_invocation(
                work_subject=f"run-publication:{run_id}",
                role="final_publication",
                phase="run_publication",
            ),
        }
    )
    state_path = next((git_repo / ".agent-run" / "runs").glob("*.json"))
    state_path.write_text(json.dumps(state), encoding="utf-8")
    before = deepcopy(state)

    result = run_cli(git_repo, fixture, "resume", run_id)

    assert result.returncode == 2
    assert stdout_json(result)["status"] == "incompatible_run_state"
    assert json.loads(state_path.read_text(encoding="utf-8")) == before


@pytest.mark.parametrize("command", ["status", "history", "resume"])
@pytest.mark.parametrize(
    "owner",
    [
        {"validation_attempts": 1},
        {"acceptance_generation": 1},
        {"acceptance_generation": 1, "validation_attempts": 1},
    ],
)
def test_resume_rejects_an_incomplete_run_acceptance_owner(
    git_repo: Path,
    owner: dict[str, int | str],
    command: str,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"2": issue(2)})
    run_id = stdout_json(run_cli(git_repo, fixture, "start", "1"))["run_id"]
    state = load_only_run_state(git_repo)
    state.update(
        {
            "status": "execution_failed",
            "run_acceptance": owner,
            "active_agent_invocation": failed_invocation(
                work_subject=f"run-acceptance:{run_id}",
                role="reviewer",
                phase="run_acceptance",
            ),
        }
    )
    state_path = next((git_repo / ".agent-run" / "runs").glob("*.json"))
    state_path.write_text(json.dumps(state), encoding="utf-8")
    before = deepcopy(state)

    arguments = (command, run_id, "--json") if command != "resume" else (command, run_id)
    result = run_cli(git_repo, fixture, *arguments)

    assert result.returncode == 2
    assert stdout_json(result)["status"] == "incompatible_run_state"
    assert json.loads(state_path.read_text(encoding="utf-8")) == before


@pytest.mark.parametrize(
    ("role", "phase", "owner"),
    [
        ("reviewer", "run_acceptance", {"phase": "accepted", "acceptance_generation": 1, "validation_attempts": 1}),
        ("final_publication", "run_publication", {"phase": "waiting_checks"}),
    ],
)
def test_resume_rejects_an_owner_that_has_already_advanced(
    git_repo: Path, role: str, phase: str, owner: dict[str, int | str]
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"2": issue(2)})
    run_id = stdout_json(run_cli(git_repo, fixture, "start", "1"))["run_id"]
    state = load_only_run_state(git_repo)
    state.update(
        {
            "status": "execution_failed",
            "run_acceptance": {
                "phase": "accepted",
                "policy_snapshot": deepcopy(state["policy_snapshot"]),
                "acceptance_generation": 1,
                "validation_attempts": 1,
                "review_budget": _canonical_run_budget(),
                "review_budget_history": [],
            },
            "active_agent_invocation": failed_invocation(
                work_subject=(
                    f"run-acceptance:{run_id}"
                    if role == "reviewer"
                    else f"run-publication:{run_id}"
                ),
                role=role,
                phase=phase,
            ),
        }
    )
    if role == "final_publication":
        state["run_publication"] = owner
    else:
        state["run_acceptance"] = owner
    state_path = next((git_repo / ".agent-run" / "runs").glob("*.json"))
    state_path.write_text(json.dumps(state), encoding="utf-8")
    before = deepcopy(state)

    result = run_cli(git_repo, fixture, "resume", run_id)

    assert result.returncode == 2
    assert stdout_json(result)["status"] == "incompatible_run_state"
    assert json.loads(state_path.read_text(encoding="utf-8")) == before


@pytest.mark.parametrize("command", ["status", "history", "resume"])
@pytest.mark.parametrize(
    ("role", "invocation_phase", "owner_phase"),
    [
        ("development", "developing", "accepted"),
        ("fresh_acceptance", "reviewing", "accepted"),
        ("publication", "publication", "publishing"),
    ],
)
def test_change_resume_rejects_an_owner_that_has_already_advanced(
    git_repo: Path,
    command: str,
    role: str,
    invocation_phase: str,
    owner_phase: str,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"2": issue(2)})
    run_id = stdout_json(run_cli(git_repo, fixture, "start", "1"))["run_id"]
    state = load_only_run_state(git_repo)
    state["ticket_jobs"]["2"].update(
        {"ticket_branch_generation": 1, "phase": owner_phase}
    )
    state.update(
        {
            "status": "execution_failed",
            "active_agent_invocation": failed_invocation(
                work_subject="ticket:2", role=role, phase=invocation_phase
            ),
        }
    )
    state_path = next((git_repo / ".agent-run" / "runs").glob("*.json"))
    state_path.write_text(json.dumps(state), encoding="utf-8")
    before = deepcopy(state)

    arguments = (command, run_id, "--json") if command != "resume" else (command, run_id)
    result = run_cli(git_repo, fixture, *arguments)

    assert result.returncode == 2
    assert stdout_json(result)["status"] == "incompatible_run_state"
    assert json.loads(state_path.read_text(encoding="utf-8")) == before


@pytest.mark.parametrize("command", ["status", "history"])
def test_completed_invocation_remains_a_readable_audit_snapshot(
    git_repo: Path, command: str
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"2": issue(2)})
    run_id = stdout_json(run_cli(git_repo, fixture, "start", "1"))["run_id"]
    state = load_only_run_state(git_repo)
    state.update(
        {
            "status": "run_publication_pending",
            "run_acceptance": {
                "phase": "accepted",
                "policy_snapshot": deepcopy(state["policy_snapshot"]),
                "acceptance_generation": 1,
                "validation_attempts": 1,
                "review_budget": _canonical_run_budget(),
                "review_budget_history": [],
            },
            "active_agent_invocation": {
                **failed_invocation(
                    work_subject=f"run-acceptance:{run_id}",
                    role="reviewer",
                    phase="run_acceptance",
                    status="completed",
                ),
                "reported_thread_id": "reviewer-thread",
                "error": None,
                "return_code": 0,
            },
        }
    )
    state_path = next((git_repo / ".agent-run" / "runs").glob("*.json"))
    state_path.write_text(json.dumps(state), encoding="utf-8")
    before = deepcopy(state)

    result = run_cli(git_repo, fixture, command, run_id, "--json")

    assert result.returncode == 0
    assert json.loads(state_path.read_text(encoding="utf-8")) == before


def test_incompatible_state_does_not_replay_its_diagnostics(git_repo: Path) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"2": issue(2)})
    run_id = stdout_json(run_cli(git_repo, fixture, "start", "1"))["run_id"]
    state = load_only_run_state(git_repo)
    state["status"] = "blocked"
    state["diagnostics"] = [{"code": "attacker", "message": "not canonical"}]
    state["parent"].pop("number")
    state_path = next((git_repo / ".agent-run" / "runs").glob("*.json"))
    state_path.write_text(json.dumps(state), encoding="utf-8")
    before = deepcopy(state)

    result = run_cli(git_repo, fixture, "status", run_id, "--json")

    assert result.returncode == 2
    output = stdout_json(result)
    assert output["status"] == "incompatible_run_state"
    assert output["diagnostics"] == [
        {
            "code": "incompatible_run_state",
            "message": "本地 Run state 不符合当前唯一 Invocation/Generation 契约；"
            "不会迁移、兼容读取或执行任何 mutation，请重新创建或清理该 Run",
        }
    ]
    assert json.loads(state_path.read_text(encoding="utf-8")) == before


def test_cli_rejects_malformed_human_blocker_without_mutation(git_repo: Path) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"2": issue(2)})
    run_id = stdout_json(run_cli(git_repo, fixture, "start", "1"))["run_id"]
    state = load_only_run_state(git_repo)
    state.update(
        {
            "status": "ready_for_human",
            "parent_job": {
                "phase": "blocked",
                "blocked_reason": "agent_requires_human",
                "human_blockers": [1],
            },
        }
    )
    state_path = next((git_repo / ".agent-run" / "runs").glob("*.json"))
    state_path.write_text(json.dumps(state), encoding="utf-8")
    before = deepcopy(state)

    result = run_cli(git_repo, fixture, "resume", run_id)

    assert result.returncode == 2
    assert stdout_json(result)["status"] == "incompatible_run_state"
    assert json.loads(state_path.read_text(encoding="utf-8")) == before


@pytest.mark.parametrize("command", ["status", "history", "resume"])
def test_cli_rejects_resolved_run_with_missing_observed_revisions(
    git_repo: Path, command: str
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"2": issue(2)})
    run_id = stdout_json(run_cli(git_repo, fixture, "start", "1"))["run_id"]
    state = load_only_run_state(git_repo)
    state["parent"]["revision"] = None
    state["ticket_graph"]["revision"] = None
    state_path = next((git_repo / ".agent-run" / "runs").glob("*.json"))
    state_path.write_text(json.dumps(state), encoding="utf-8")
    before = deepcopy(state)
    arguments = (command, run_id, "--json") if command != "resume" else (command, run_id)

    result = run_cli(git_repo, fixture, *arguments)

    assert result.returncode == 2
    assert stdout_json(result)["status"] == "incompatible_run_state"
    assert json.loads(state_path.read_text(encoding="utf-8")) == before


def test_status_prints_the_recovery_command_for_manual_boundaries(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"2": issue(2)})
    run_id = stdout_json(run_cli(git_repo, fixture, "start", "1"))["run_id"]
    state_path = next((git_repo / ".agent-run" / "runs").glob("*.json"))
    current = load_only_run_state(git_repo)
    cases = [
        (
            "execution_failed",
            {
                "active_agent_invocation": failed_invocation(
                    work_subject="ticket:2", role="development", phase="developing"
                )
            },
            "agent-run resume 1 --repo example/project",
        ),
        (
            "ready_for_human",
            {
                "active_ticket_job": None,
                "parent_job": {
                    "phase": "blocked",
                    "blocked_reason": "agent_requires_human",
                    "human_blockers": ["Need maintainer input."],
                    "review_budget": _canonical_run_budget(),
                    "review_budget_history": [],
                },
            },
            "agent-run resume 1 --repo example/project",
        ),
        (
            "requeue_required",
            {
                "requeue_required": {
                    "work_subject": "ticket:2",
                    "generation": 1,
                    "reason": "ticket_requirements_changed",
                }
            },
            f"agent-run requeue {run_id}",
        ),
    ]

    for status, additions, expected_action in cases:
        state = deepcopy(current)
        state["status"] = status
        state.update(additions)
        if state.get("active_agent_invocation") is not None:
            invocation = state["active_agent_invocation"]
            assert isinstance(invocation, dict)
            state["ticket_jobs"]["2"].update(
                {
                    "ticket_branch_generation": 1,
                    "phase": "developing",
                    "review_budget": _canonical_run_budget(),
                    "review_budget_history": [],
                    "pending_semantic_attempt": deepcopy(
                        invocation["semantic_attempt"]
                    ),
                }
            )
            state["active_ticket_job"] = deepcopy(state["ticket_jobs"]["2"])
        if status == "requeue_required":
            state["ticket_jobs"]["2"].update(
                {
                    "ticket_branch_generation": 1,
                    "phase": "developing",
                    "review_budget": _canonical_run_budget(),
                    "review_budget_history": [],
                }
            )
            state["active_ticket_job"] = deepcopy(state["ticket_jobs"]["2"])
            state["terminal_kind"] = "requeue_required"
            state["diagnostics"] = [
                {
                    "code": "ticket_requirements_changed",
                    "message": "Ticket requirements changed; run requeue",
                }
            ]
        state_path.write_text(json.dumps(state), encoding="utf-8")

        result = run_cli(git_repo, fixture, "status", run_id, "--json")

        assert result.returncode == 0
        assert stdout_json(result)["next_action"] == expected_action


def test_resume_does_not_refresh_a_run_without_an_agent_boundary(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"2": issue(2)})
    started = run_cli(git_repo, fixture, "start", "1")
    state = load_only_run_state(git_repo)
    tree = subprocess.run(
        ["git", "rev-parse", f"{state['run_branch']}^{{tree}}"],
        cwd=git_repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    advanced = subprocess.run(
        [
            "git",
            "commit-tree",
            tree,
            "-p",
            state["run_branch"],
            "-m",
            "integrate accepted ticket",
        ],
        cwd=git_repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    subprocess.run(
        ["git", "update-ref", f"refs/heads/{state['run_branch']}", advanced],
        cwd=git_repo,
        check=True,
    )

    resumed = run_cli(
        git_repo, fixture, "resume", stdout_json(started)["run_id"]
    )

    assert resumed.returncode == 2
    assert stdout_json(resumed)["status"] == "active"
    assert (
        subprocess.run(
            ["git", "rev-parse", state["run_branch"]],
            cwd=git_repo,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        == advanced
    )


def test_live_default_head_is_fetched_before_run_branch_creation(
    git_repo: Path,
    tmp_path: Path,
) -> None:
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "clone", "--bare", str(git_repo), str(remote)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "remote", "add", "origin", str(remote)],
        cwd=git_repo,
        check=True,
    )
    updater = tmp_path / "updater"
    subprocess.run(
        ["git", "clone", str(remote), str(updater)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Updater"], cwd=updater, check=True
    )
    subprocess.run(
        ["git", "config", "user.email", "updater@example.invalid"],
        cwd=updater,
        check=True,
    )
    (updater / "remote.txt").write_text("new live head\n", encoding="utf-8")
    subprocess.run(["git", "add", "remote.txt"], cwd=updater, check=True)
    subprocess.run(
        ["git", "commit", "-m", "advance remote"],
        cwd=updater,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "push", "origin", "main"],
        cwd=updater,
        check=True,
        capture_output=True,
    )
    live_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=updater,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"2": issue(2)},
        default_head_sha=live_head,
    )

    result = run_cli(git_repo, fixture, "start", "1")

    assert result.returncode == 0, result.stderr
    state = load_only_run_state(git_repo)
    assert state["base"]["sha"] == live_head
    branch_head = subprocess.run(
        ["git", "rev-parse", state["run_branch"]],
        cwd=git_repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    assert branch_head == live_head


def test_no_executable_ticket_is_progress_exhaustion_not_completion(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"2": issue(2, blocked_by=[{"number": 99, "state": "OPEN"}])},
    )

    result = run_cli(git_repo, fixture, "start", "1")

    assert result.returncode == 2
    output = stdout_json(result)
    state = load_only_run_state(git_repo)
    assert output["status"] == "progress_exhausted"
    assert state["status"] == "progress_exhausted"
    assert state["active_ticket_job"] is None
    assert state["diagnostics"][0]["code"] == "no_executable_ticket"


@pytest.mark.parametrize(
    "triage_label", ["needs-triage", "needs-info", "ready-for-human"]
)
def test_triage_ticket_does_not_create_an_operator_gate(
    git_repo: Path,
    triage_label: str,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={
            "2": issue(2, labels=["ready-for-agent", triage_label]),
        },
    )

    started = run_cli(git_repo, fixture, "start", "1")

    assert started.returncode == 2
    run_id = stdout_json(started)["run_id"]
    state = load_only_run_state(git_repo)
    assert state["status"] == "progress_exhausted"
    assert state["terminal_kind"] == "temporarily_no_work"
    assert state["diagnostics"][0]["remaining_tickets"] == [
        {
            "ticket_number": 2,
            "reason": f"disqualifying_label:{triage_label}",
        }
    ]
    for command in ("status", "history"):
        view = stdout_json(
            run_cli(git_repo, fixture, command, run_id, "--json")
        )
        assert view["operator_action"] is None


def test_needs_triage_ticket_does_not_block_an_eligible_ticket(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={
            "2": issue(2, labels=["ready-for-agent", "needs-triage"]),
            "3": issue(3),
        },
    )

    started = run_cli(git_repo, fixture, "start", "1")

    assert started.returncode == 0, started.stderr
    output = stdout_json(started)
    assert output["status"] == "active"
    assert output["active_ticket"] == 3
    state = load_only_run_state(git_repo)
    assert state["frontier"] == [3]
    assert state["active_ticket_job"]["ticket_number"] == 3


def test_cycle_is_persisted_as_blocked_with_diagnostic(git_repo: Path) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={
            "2": issue(2, blocked_by=[{"number": 3, "state": "OPEN"}]),
            "3": issue(3, blocked_by=[{"number": 2, "state": "OPEN"}]),
        },
    )

    result = run_cli(git_repo, fixture, "start", "1")

    assert result.returncode == 2
    state = load_only_run_state(git_repo)
    assert state["status"] == "blocked"
    assert state["terminal_kind"] == "permanent_blocked"
    assert state["diagnostics"][0]["code"] == "dependency_cycle"
    assert state["diagnostics"][0]["ticket_numbers"] == [2, 3]


def test_missing_ticket_is_persisted_as_blocked(git_repo: Path) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"2": issue(2)},
        parent={
            "number": 1,
            "title": "Parent spec",
            "body": "Deliver the ticket set.",
            "sub_issues": [2, 404],
            "sub_issue_order_reliable": True,
        },
    )

    result = run_cli(git_repo, fixture, "start", "1")

    assert result.returncode == 2
    state = load_only_run_state(git_repo)
    assert state["status"] == "blocked"
    assert state["terminal_kind"] == "permanent_blocked"
    assert state["diagnostics"][0] == {
        "code": "missing_ticket",
        "message": "GitHub did not return sub-issue #404",
        "ticket_number": 404,
    }


def test_github_read_failure_is_persisted_and_retryable(git_repo: Path) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"2": issue(2)},
        error={"code": "github_read_failed", "message": "simulated outage"},
    )

    waiting = run_cli(git_repo, fixture, "start", "1")

    assert waiting.returncode == 0, waiting.stderr
    assert stdout_json(waiting)["status"] == "waiting_external"
    state = load_only_run_state(git_repo)
    assert state["status"] == "waiting_external"
    assert state["terminal_kind"] == "waiting_external"
    assert state["diagnostics"][0]["code"] == "github_read_failed"

    fixture = write_fixture(git_repo / "github.json", issues={"2": issue(2)})
    retried = run_cli(git_repo, fixture, "start", "1")

    assert retried.returncode == 0, retried.stderr
    assert stdout_json(retried)["result"] == "resumed"
    assert load_only_run_state(git_repo)["status"] == "active"
