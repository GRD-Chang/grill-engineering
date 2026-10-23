from __future__ import annotations


import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


import agent_run.install.doctor as doctor


PROJECT_ROOT = Path(__file__).parents[1]
_DOCTOR_TIMEOUT_TEST_LIMIT_SECONDS = 5


def test_public_operator_docs_describe_the_v01_quickstart() -> None:
    reference = (PROJECT_ROOT / "docs" / "agent-run.md").read_text(encoding="utf-8")
    for required in (
        "./install.sh", "release tag", "agent-run doctor", "--migrate-github-app",
        "--rollback", "--uninstall", "Linux", "./setup.sh", "独立克隆",
    ):
        assert required in reference

    # Public READMEs route agents to language-specific guides; installation
    # details belong there rather than being duplicated in the landing page.
    for readme, guide_name, install_name in (
        ("README.md", "agent-guide.en.md", "install.en.md"),
        ("README.zh-CN.md", "agent-guide.md", "install.md"),
    ):
        landing = (PROJECT_ROOT / readme).read_text(encoding="utf-8")
        guide = (PROJECT_ROOT / "docs" / guide_name).read_text(encoding="utf-8")
        install = (PROJECT_ROOT / "docs" / install_name).read_text(encoding="utf-8")
        assert f"](docs/{guide_name})" in landing
        assert f"]({install_name}#" in guide
        for required in (
            "./setup.sh", "./install.sh", "agent-run doctor --json",
            "--rollback", "--uninstall", "Linux", "gh auth login",
            "gh auth status", "agent-run settings show", "settings show/configure",
            "host", "PAT", "codex login status",
        ):
            assert required in install
        assert "](agent-run.md#github-身份)" in install
        assert "](user-defaults.md)" in install
        assert "](agent-run.md#权限边界)" in install


@pytest.mark.parametrize("source", ["host", "pat"])
def test_doctor_uses_unified_identity_and_ignores_old_app_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    config = tmp_path / "config" / "agent-run"
    config.mkdir(parents=True)
    legacy = config / "github-app.json"
    legacy.write_text("invalid legacy configuration")
    settings = config / "user-defaults.json"
    settings.write_text(json.dumps({"github": {
        "worker_source": source, "pat": {"env": "TEST_MISSING_PAT"},
    }}))
    original = settings.read_bytes()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config.parent))
    monkeypatch.delenv("TEST_MISSING_PAT", raising=False)
    assert doctor._provider_check() == {"provider": source, "status": "ok"}
    assert settings.read_bytes() == original
    assert legacy.read_text() == "invalid legacy configuration"


@pytest.mark.parametrize(
    "command", ["start", "deliver", "accept-run", "publish-run"]
)
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
    assert "无效选项" in result.stderr


def test_run_repair_docs_describe_direct_delivery_and_exact_candidate_promotion() -> None:
    context = (PROJECT_ROOT / "CONTEXT.md").read_text(encoding="utf-8")
    operator_guide = (PROJECT_ROOT / "docs" / "agent-run.md").read_text(
        encoding="utf-8"
    )

    assert "程序准确更新 Run Branch，仅最终 Run PR 承担 Publish 文案、Hosted CI 与人工批准" in context
    assert "修复保留身份、受管工作区、必要本地分支、候选与历史" in context
    assert "Candidate Run Acceptance → 严格 promotion" in operator_guide
    assert "不创建内部修复 PR、调用内部 Publish 或等待内部 CI" in operator_guide
    assert "未知推送先对账" in operator_guide
    assert "权限或规则拒绝时保全修复成果与写入意图" in operator_guide
    assert "一个 Cycle 可依次包含多个 Run Repair Job" not in context
    assert "轮转出后继 Job" not in context
    assert "归档旧 Job/PR 并轮转新的 branch/PR" not in operator_guide
    assert "Run Repair → fresh Run Acceptance → 新 PR" not in operator_guide
