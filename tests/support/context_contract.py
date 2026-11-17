"""不依赖产品安装的 Run Repair 文档合同，供 pytest 与私有 CI 共用。"""

from __future__ import annotations


def validate_run_repair_document_contract(context: str, operator_guide: str) -> None:
    """保留修复 PR 准入、原 Parent 额度与准确候选提升合同。"""
    for document, text, required, forbidden in (
        (
            "GLOSSARY.md", context,
            (
                "程序发布以 Run Branch 为 base 的 Run Repair PR",
                "读取该目标分支的实际 Required Checks",
                "门禁通过后自动普通合入",
                "最终 Run PR 仍承担默认分支检查与明确人工批准",
                "Repair 工作继续使用同一 Parent subject 的额度",
                "修复保留身份、受管工作区、必要本地分支、候选与历史",
            ),
            ("一个 Cycle 可依次包含多个 Run Repair Job", "轮转出后继 Job"),
        ),
        (
            "docs/agent-run.md", operator_guide,
            (
                "Candidate 通过后由程序核验准确源与默认分支必要祖先关系",
                "发布修复分支和以 Run Branch 为 base 的 Run Repair PR",
                "读取该 PR 目标分支的 Required Checks",
                "尚未合入的 PR 原地更新；已合入记录保留",
                "不重置累计额度",
                "Candidate tree、repair base、实际 Run tree",
                "成功写入不重复",
                "原边界仍有效且操作未发生时才继续；无法核实的结果保持有界等待和具体缺口",
            ),
            ("归档旧 Job/PR 并轮转新的 branch/PR", "Run Repair → fresh Run Acceptance → 新 PR",
             "不创建内部修复 PR、调用内部 Publish 或等待内部 CI"),
        ),
    ):
        for phrase in required:
            if phrase not in text:
                raise ValueError(f"Run Repair document contract: {document} missing {phrase!r}")
        for phrase in forbidden:
            if phrase in text:
                raise ValueError(f"Run Repair document contract: {document} contains {phrase!r}")
