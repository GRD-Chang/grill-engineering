"""Read-only onboarding facts; existing identity providers own authentication."""
from __future__ import annotations

import os
from pathlib import Path
import stat
from typing import Any, Mapping

from agent_run.adapters.auth.identity import validate_worker_auth
from agent_run.adapters.auth.runner_credentials import validate_runner_auth
from agent_run.config.github_app import GitHubAppProfileStore
from agent_run.contracts.identity import validate_identity_snapshot


def identity_checks(
    github: Mapping[str, Any], *, online: bool, repository: str | None,
) -> dict[str, dict[str, Any]]:
    """Local checks inspect references, never PAT contents or private keys."""
    result: dict[str, dict[str, Any]] = {}
    for role in ("runner", "worker"):
        check: dict[str, Any] = {"source": "unknown", "status": "invalid",
                                 "authentication": "not_verified"}
        result[role] = check
        if github.get("migration") == "required":
            check.update(status="migration_required", next_step="agent-run settings configure --migrate-github-app")
            continue
        try:
            identity = validate_identity_snapshot(github.get(role))
            check.update(source=identity["source"], status="ok")
            if identity["source"] == "pat":
                reference = identity["pat"]
                if "env" in reference:
                    available = reference["env"] in os.environ
                else:
                    path = Path(reference["file"])
                    metadata = path.stat()
                    available = stat.S_ISREG(metadata.st_mode) and os.access(path, os.R_OK)
                if not available:
                    check["status"] = "unavailable"
            elif identity["source"] == "app":
                # This validates metadata and permissions, without reading the key.
                GitHubAppProfileStore._validate_values(**identity["app_profile"])
            if online and check["status"] == "ok":
                if role == "runner":
                    validate_runner_auth(identity)
                else:
                    validate_worker_auth(identity, repository=repository)
                check["authentication"] = "verified"
        except (ValueError, OSError, RuntimeError):
            check["status"] = "unavailable"
        if check["status"] != "ok":
            check.setdefault("next_step", "检查所选身份的原引用、授权或网络，再执行 agent-run doctor --online；不会回退 host")
    return result


def skills_check() -> dict[str, Any]:
    """Inspect user-level skill entry files without installing or rewriting them."""
    roots = (Path.home() / ".agents" / "skills",
             Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "skills")
    entries = {}
    for name in ("implement", "code-review"):
        found = next((root / name / "SKILL.md" for root in roots
                      if (root / name / "SKILL.md").is_file()
                      and os.access(root / name / "SKILL.md", os.R_OK)), None)
        entries[name] = {"status": "readable" if found else "missing",
                         "path": str(found) if found else None}
    return {"status": "ok" if all(value["status"] == "readable" for value in entries.values()) else "missing",
            "entries": entries, "dependencies": "not_verified", "worker_discovery": "not_verified",
            "next_step": "复用已有 Skills；核对其依赖与目标仓库 AGENTS.md，不覆盖定制、不自动安装"}


def next_steps() -> list[str]:
    return [
        "Windows/macOS 原生进程不支持 Worker；进入已准备的 WSL2 / Linux VM Guest 后执行 uname -s（应为 Linux）；Linux 输出本身不证明 Guest 已通过运行验收",
        "项目、Run 数据、Unix socket 与所选凭据引用留在同一 Linux Guest 用户环境；先执行 agent-run doctor --local --mode foreground --json；后台另查 agent-run doctor --local --mode background --json，布局见 docs/productization/support.md",
        "核对安装版本与本地环境：agent-run doctor --local",
        "保留已有身份、模型和预算：agent-run settings show；需要时才 settings configure",
        "核对 implement/code-review 及其依赖、项目工具链和 AGENTS.md；默认无需 prompts init",
        "显式在线验证所选身份及仓库读取：agent-run doctor --online --repo OWNER/REPO（不调用模型）",
        "模型兼容性探针可能收费，须单独确认；准备完成后 agent-run run PARENT --repo OWNER/REPO；最终仍须批准",
    ]
