---
status: accepted
---

# Codex 使用 YOLO，外层只保护 Git 权威和 Publisher 凭据

所有 Development、Repair、Fresh/Run Acceptance 与 Publication Codex 都在信任 Agent 的前提下以 YOLO 运行，可以访问宿主文件系统、联网并使用真实 Git CLI。系统不建设通用 sandbox、任务目录写入边界或 Worker 其他命令的 allowlist；唯独 Worker 的 `gh` 是临时 Controller-owned adapter，只接受固定的 GitHub 读取请求，并由 Controller 使用 ADR 0008 定义的活动凭据来源在有界时间内执行。这条窄规则保护凭据不进入 Worker，不把 YOLO 变成通用隔离。Execution Guard 还把权威 Git metadata 设为只读，确保 Publisher 写凭据不进入 Worker，并在 Worker 启动前以固定脱敏错误拒绝 local Git config 中带 userinfo 的 HTTP(S) remote URL，而不改写 config。YOLO 是宿主能力边界，不扩大角色合同：Development/Repair 可编辑当前 checkout，Fresh/Run Acceptance 只可产生和清理验证产物，Publication 保持只读。我们接受它不能抵抗恶意 Agent、prompt injection 主动寻找其他宿主凭据、宿主文件污染或数据外泄，以换取真实开发和 E2E 工具不受限制，以及更小的实现和维护成本。

Worker 只感知普通命令形态的受控只读 `gh`，不承担 adapter、socket、PATH 或挂载机制的知识。PATH 不构成该入口的权威绑定：Execution Guard 只检查 Worker PATH 与 Codex command-tool 的有效前置目录（`CODEX_INSTALL_DIR`，未设置时为 `~/.local/bin`），在其中收集当前存在且可执行的 `gh`，解析并去重 canonical regular-file target，再由 bubblewrap 把同一个 invocation-local adapter 只读绑定到这些 target；任一挂载失败都发生在 Codex payload 启动前。新 adapter 不再 prepend 到 PATH，只清理继承环境中失效的旧 adapter 项；bubblewrap 的原子挂载就是启动保障，不增加 sandbox launcher、`samefile` 或私有 probe，也不增加持久状态。Controller 位于该 mount namespace 外，继续使用原始真实 `gh` 或专用 GitHub App 执行 broker 已允许的读取。该机制不扫描 PATH 外文件、不建设通用 executable replacement、不处理 shell alias/function，也不防御可信 Agent 主动复制或下载替代客户端。普通测试使用 fake/local 边界；真实 Codex command-tool compatibility acceptance 只能显式 opt-in，不进入普通 pytest 或常规 CI。

Reviewer 的交付工作区继续保持只读。优先将运行产物放在外部目录，必要时允许使用仓库外的临时运行副本；
副本必须对应当前实际验收内容，不获得修复源码、测试、配置或依赖声明的权限，使用后关闭进程并清理。
一致性核验与证据要求见 [Prompt 合同](../agents/agent-prompts.md#独立验收)。该能力由 Reviewer 按 Prompt 使用，
不引入 Controller 自动复制流程或额外状态机。

Controller 同样不审计 Codex 内部 subagent 的事件流、身份或 skill 调用 provenance。
Initial Development 在实现、验证和自行检查后，默认建议通过审查型 subagent 进行一轮内部预检；
本次开发最多一轮，按实际风险选择审查方向，一轮可以包含多个审查型 subagent。审查型 subagent
使用 `fork_turns: "none"`，由 Development 提供完成审查所需的中立任务事实、当前范围和真实证据；
明确 Repair source 的定向 Repair 默认不启动内部 Reviewer；只有修复既改变原方案、又引入此前未覆盖的
关键风险时，才允许一次定向审查，处理结果后自行检查与复测，不反复启动通用审查。该例外不扩大
开发权限，也不替代正式验收。Fresh Acceptance 与 Run Acceptance 继续形成
独立的 E2E、Standards、Spec 三条正式验收 lane，并必须调用 `skill:code-review`。Reviewer 对三条
lane 的最终判断负责；所有审查或评价型 subagent 使用 `fork_turns: "none"` 和中立任务事实。Reviewer 1 建立完整基线；Reviewer 2+ 优先核销原 Findings、检查 repair delta 与直接回归，
缺少具体风险依据时避免重复完整扫描，并可按当前证据和实际影响自主扩大范围。Prompt 不固定
每个维度对应的 subagent、任务包字段、检查命令、调用顺序或嵌套层级。Controller 只校验父 Reviewer Thread 的
新鲜性、固定三 lane 的状态/证据和外层 SHA/Revision 绑定，不增加内部 Review 状态机或调用图审计。
