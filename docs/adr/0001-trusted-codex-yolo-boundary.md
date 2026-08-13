---
status: accepted
---

# Codex 使用 YOLO，外层只保护 Git 权威和 Publisher 凭据

所有 Development、Repair、Fresh/Run Acceptance 与 Publication Codex 都在信任 Agent 的前提下以 YOLO 运行，可以访问宿主文件系统、联网并使用完整真实 Git/`gh` CLI。系统不建设通用 sandbox、命令 allowlist 或任务目录写入边界；Execution Guard 只把权威 Git metadata 设为只读，确保 Publisher 写凭据不进入 Worker，并在 Worker 启动前以固定脱敏错误拒绝 local Git config 中带 userinfo 的 HTTP(S) remote URL，而不改写 config。YOLO 是宿主能力边界，不扩大角色合同：Development/Repair 可编辑当前 checkout，Fresh/Run Acceptance 只可产生和清理验证产物，Publication 保持只读。我们接受它不能抵抗恶意 Agent、prompt injection 主动寻找其他宿主凭据、宿主文件污染或数据外泄，以换取真实开发和 E2E 工具不受限制，以及更小的实现和维护成本。

Controller 同样不审计 Codex 内部 subagent 的事件流、身份或 skill 调用 provenance。
Development 与 Repair Codex 必须按 Prompt 派发 Standards/Spec 预审 subagent，失败时自行解决并
重新派发；Fresh Validation 与 Run Acceptance Codex 必须派发独立 E2E、Standards、Spec 三条 lane，
不得用父 Reviewer 自签替代缺失 lane。Controller 只校验父 Reviewer Thread 的新鲜性、固定三 lane 的
状态/证据和外层 SHA/Revision 绑定。这是信任 Agent 的行为合同，不是由 Controller 重建的第二套 Agent 编排器。
