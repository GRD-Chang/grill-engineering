"""共享测试支撑：prompt_capture；每次调用保持独立状态。"""

from __future__ import annotations

import json
import subprocess
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from agent_run.codex import CodexCliBackend


PASS_EVIDENCE = {
    "e2e": "操作或命令：运行候选公开流程；退出码：0；结果：候选通过端到端复验。",
    "standards": "审查范围或基线：仓库编码规范与候选 diff；结论：未发现违反项。",
    "spec": "已核对的验收标准：请求中的全部验收标准；覆盖结论：候选完整覆盖。",
}


def _capture_public_prompt(
    tmp_path: Path,
    monkeypatch: Any,
    method: str,
    request: dict[str, Any],
    *,
    name: str,
    worker_calls: list[tuple[list[str], dict[str, Any]]] | None = None,
) -> str:
    checkout = tmp_path / name
    checkout.mkdir()
    captured: list[str] = []
    if method == "develop":
        result: dict[str, Any] = {
            "result_kind": "development",
            "summary": "Implemented and verified.",
            "human_blockers": None,
        }
    elif method == "review":
        result = {
            "checks": {
                lane: {
                    "status": "pass",
                    "evidence": PASS_EVIDENCE[lane],
                    "findings": [],
                }
                for lane in ("e2e", "standards", "spec")
            }
        }
    else:
        result = {
            "result_kind": "publication",
            "commit_message": "fix(agent): publish validated repair",
            "pr_title": "fix(agent): publish validated repair",
            "pr_body_markdown": (
                "## What Problem This Solves\n\nA validated change is ready.\n\n"
                "## Why This Change Was Made\n\nThe change follows the contract.\n\n"
                "## User Impact\n\nThe requested behavior is available.\n\n"
                "## Evidence\n\nIndependent validation passed."
            ),
            "human_blockers": None,
        }

    def fake_run(
        arguments: list[str], **options: Any
    ) -> subprocess.CompletedProcess[str]:
        captured.append(str(options["prompt"]))
        schema_index = arguments.index("--output-schema") + 1
        options["captured_schema"] = json.loads(
            Path(arguments[schema_index]).read_text(encoding="utf-8")
        )
        if worker_calls is not None:
            worker_calls.append((arguments, options))
        output_index = arguments.index("--output-last-message") + 1
        Path(arguments[output_index]).write_text(
            json.dumps(result, ensure_ascii=False), encoding="utf-8"
        )
        reported_thread = request.get("thread_id", "public-prompt-test")
        return subprocess.CompletedProcess(
            arguments,
            0,
            stdout=(
                '{"type":"thread.started","thread_id":"'
                + str(reported_thread)
                + '"}\n'
            ),
            stderr="",
        )

    # 这里的 Worker 已是 stdin/schema 捕获函数，不会发起 GitHub 请求。
    # 无消费者的凭据续签线程由 backend 专项真实边界测试负责，避免每个文案变体等待关闭。
    with monkeypatch.context() as scoped:
        scoped.setattr("agent_run.codex.run_worker_process", fake_run)
        scoped.setattr(
            "agent_run.codex.NativeCredentialDelivery",
            lambda *args, **kwargs: nullcontext(SimpleNamespace(start=lambda: None)),
        )
        backend = CodexCliBackend(credential_provider=lambda: "reader-secret")
        getattr(backend, method)({**request, "checkout": str(checkout)})
    return captured[0]
