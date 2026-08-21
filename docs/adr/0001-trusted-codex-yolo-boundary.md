---
status: accepted
---

# Codex 使用 YOLO，外层只保护 Git 权威和 Publisher 凭据

所有 Development、Repair、Fresh/Run Acceptance 与 Publication Codex 都在信任 Agent 的前提下以 YOLO 运行，可以访问宿主文件系统、联网并使用真实 Git CLI。系统不建设通用 sandbox、任务目录写入边界或 Worker 其他命令的 allowlist；唯独 Worker 的 `gh` 是临时 Controller-owned adapter，只接受固定的 GitHub 读取请求，并由 Controller 在有界时间内以短期只读 token 执行。这条窄规则保护 token 不进入 Worker，不把 YOLO 变成通用隔离。Execution Guard 还把权威 Git metadata 设为只读，确保 Publisher 写凭据不进入 Worker，并在 Worker 启动前以固定脱敏错误拒绝 local Git config 中带 userinfo 的 HTTP(S) remote URL，而不改写 config。YOLO 是宿主能力边界，不扩大角色合同：Development/Repair 可编辑当前 checkout，Fresh/Run Acceptance 只可产生和清理验证产物，Publication 保持只读。我们接受它不能抵抗恶意 Agent、prompt injection 主动寻找其他宿主凭据、宿主文件污染或数据外泄，以换取真实开发和 E2E 工具不受限制，以及更小的实现和维护成本。

Controller 同样不审计 Codex 内部 subagent 的事件流、身份或 skill 调用 provenance。
根据 Issue #125，Development 与 Repair Codex 先按 Review Boundary 做 scope triage，再根据实际
改动风险选择 self-preflight、定向 Reviewer 或 `skill:code-review`；普通局部改动和已有明确 Finding
的窄修复不固定派发整套预审，大型、跨模块或高风险改动仍应取得足够的开发侧审查。Fresh Acceptance
与 Run Acceptance 继续形成独立的 E2E、Standards、Spec 三条正式验收 lane；`skill:code-review`
是 Standards/Spec 可使用的推荐 SOP，但 Prompt 不规定固定 subagent 数量、精确调用次数、调用顺序
或嵌套层级。Controller 只校验父 Reviewer Thread 的新鲜性、固定三 lane 的状态/证据和外层
SHA/Revision 绑定。这是信任 Agent 的行为合同，不是由 Controller 重建的第二套 Agent 编排器。
