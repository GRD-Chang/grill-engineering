---
status: accepted
---

# Codex 使用 YOLO，外层只保护 Git 权威和 Publisher 凭据

所有 Development、Repair、Fresh/Run Acceptance 与 Publication Codex 都在信任 Agent 的前提下以 YOLO 运行，可以访问宿主文件系统、联网并使用真实 Git CLI。系统不建设通用 sandbox、任务目录写入边界或 Worker 其他命令的 allowlist；唯独 Worker 的 `gh` 是临时 Controller-owned adapter，只接受固定的 GitHub 读取请求，并由 Controller 使用 ADR 0008 定义的活动凭据来源在有界时间内执行。这条窄规则保护凭据不进入 Worker，不把 YOLO 变成通用隔离。Execution Guard 还把权威 Git metadata 设为只读，确保 Publisher 写凭据不进入 Worker，并在 Worker 启动前以固定脱敏错误拒绝 local Git config 中带 userinfo 的 HTTP(S) remote URL，而不改写 config。YOLO 是宿主能力边界，不扩大角色合同：Development/Repair 可编辑当前 checkout，Fresh/Run Acceptance 只可产生和清理验证产物，Publication 保持只读。我们接受它不能抵抗恶意 Agent、prompt injection 主动寻找其他宿主凭据、宿主文件污染或数据外泄，以换取真实开发和 E2E 工具不受限制，以及更小的实现和维护成本。

Worker 只感知普通命令形态的受控只读 `gh`，不承担 adapter、socket、PATH 或挂载机制的知识。PATH 不构成该入口的权威绑定：Execution Guard 只检查 Worker PATH 与 Codex command-tool 的有效前置目录（`CODEX_INSTALL_DIR`，未设置时为 `~/.local/bin`），在其中收集当前存在且可执行的 `gh`，解析并去重 canonical regular-file target，再由 bubblewrap 把同一个 invocation-local adapter 只读绑定到这些 target；任一挂载失败都发生在 Codex payload 启动前。新 adapter 不再 prepend 到 PATH，只清理继承环境中失效的旧 adapter 项；bubblewrap 的原子挂载就是启动保障，不增加 sandbox launcher、`samefile` 或私有 probe，也不增加持久状态。Controller 位于该 mount namespace 外，继续使用原始真实 `gh` 或专用 GitHub App 执行 broker 已允许的读取。该机制不扫描 PATH 外文件、不建设通用 executable replacement、不处理 shell alias/function，也不防御可信 Agent 主动复制或下载替代客户端。普通测试使用 fake/local 边界；真实 Codex command-tool compatibility acceptance 只能显式 opt-in，不进入普通 pytest 或常规 CI。

Controller 同样不审计 Codex 内部 subagent 的事件流、身份或 skill 调用 provenance。
根据 Issue #125，Development 与 Repair Codex 先按 Review Boundary 做 scope triage，再根据实际
改动风险选择 self-preflight、定向 Reviewer 或 `skill:code-review`；普通局部改动和已有明确 Finding
的窄修复不固定派发整套预审，大型、跨模块或高风险改动仍应取得足够的开发侧审查。Fresh Acceptance
与 Run Acceptance 继续形成独立的 E2E、Standards、Spec 三条正式验收 lane；`skill:code-review`
是 Standards/Spec 可使用的推荐 SOP，但 Prompt 不规定固定 subagent 数量、精确调用次数、调用顺序
或嵌套层级。Controller 只校验父 Reviewer Thread 的新鲜性、固定三 lane 的状态/证据和外层
SHA/Revision 绑定。这是信任 Agent 的行为合同，不是由 Controller 重建的第二套 Agent 编排器。
