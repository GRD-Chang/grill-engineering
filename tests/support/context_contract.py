"""不依赖产品安装的 Run Repair 文档合同，供 pytest 与私有 CI 共用。"""

from __future__ import annotations


def validate_run_repair_document_contract(context: str, operator_guide: str) -> None:
    """保留已实现的直接交付与准确候选提升合同。"""
    for document, text, required, forbidden in (
        (
            "CONTEXT.md", context,
            (
                "程序准确更新 Run Branch，仅最终 Run PR 承担 Publish 文案、Hosted CI 与人工批准",
                "修复保留身份、受管工作区、必要本地分支、候选与历史",
            ),
            ("一个 Cycle 可依次包含多个 Run Repair Job", "轮转出后继 Job"),
        ),
        (
            "docs/agent-run.md", operator_guide,
            (
                "Candidate Run Acceptance → 严格 promotion",
                "不创建内部修复 PR、调用内部 Publish 或等待内部 CI",
                "未知推送先对账",
                "权限或规则拒绝时保全修复成果与写入意图",
            ),
            ("归档旧 Job/PR 并轮转新的 branch/PR", "Run Repair → fresh Run Acceptance → 新 PR"),
        ),
    ):
        for phrase in required:
            if phrase not in text:
                raise ValueError(f"Run Repair document contract: {document} missing {phrase!r}")
        for phrase in forbidden:
            if phrase in text:
                raise ValueError(f"Run Repair document contract: {document} contains {phrase!r}")
