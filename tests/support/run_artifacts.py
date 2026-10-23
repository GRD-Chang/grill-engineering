"""Run 测试使用的独立 Artifact 与预算值构造，不修正调用方原始状态。"""
from __future__ import annotations


_BLOCKED_EVIDENCE = (
    "发生：GitHub 拒绝访问 Parent Issue；尝试：执行 gh issue view；人必须：授予 Issue 读取权限。"
)


_PASS_EVIDENCE = {
    "e2e": "操作或命令：执行完整 Run 验收流程；退出码：0；结果：完整 Run 通过。",
    "standards": "审查范围或基线：仓库编码规范与完整 Run diff；结论：未发现违反项。",
    "spec": "已核对的验收标准：Parent Issue 的全部验收标准；覆盖结论：完整 Run 已覆盖。",
}


def _passing_artifact() -> dict[str, object]:
    return {
        "checks": {
            name: {"status": "pass", "evidence": _PASS_EVIDENCE[name], "findings": []}
            for name in ("e2e", "standards", "spec")
        },
    }


def _repair_artifact() -> dict[str, object]:
    artifact = _passing_artifact()
    checks = artifact["checks"]
    assert isinstance(checks, dict)
    checks["e2e"] = {
        "status": "fail",
        "evidence": "The accumulated flow loses the first Ticket behavior.",
        "findings": [
            "问题：集成流程不完整；证据：两个 Ticket 组合后端到端场景失败；必须修复：恢复完整组合流程；复验：运行完整累计场景。"
        ],
    }
    return artifact
