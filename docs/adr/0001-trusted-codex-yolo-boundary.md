---
status: accepted
---

# Codex 使用 YOLO，外层只保护 Git 权威和 Publisher 凭据

所有 Development、Repair、Fresh/Run Acceptance 与 Publication Codex 都在信任 Agent 的前提下以 YOLO 运行，可以访问宿主文件系统、联网并使用真实 Git CLI。host/PAT/App Worker 使用原生 `gh`，不挂载替换可执行文件、不经过命令代理或本地 allowlist；GitHub 能力由操作者选择的凭据决定，需要较小权限时由操作者在 GitHub 设置凭据权限。Runner 程序端可独立选择 host/PAT/App，默认 host，不主动向 Worker 注入独立程序端秘密；两端选择同一来源时可能使用同一 GitHub 身份；共享 App 定义仍按调用上下文与范围隔离 token。

Execution Guard 继续保护权威 Git metadata 与 Runner 控制状态，并在 Worker 启动前以固定脱敏错误拒绝 local Git config 中带 userinfo 的 HTTP(S) remote URL，不改写 config。YOLO 和凭据写能力都不扩大角色职责：Development/Repair 只整理当前 checkout，不自行提交或发布；Fresh/Run Acceptance 只产生和清理验证产物，不修复交付内容；Publication 只生成文案。验收、Currentness、正式批准与完成判断属于 Controller，正式发布由 Publisher 程序执行。

App 私钥仅以文件路径引用，签发留在程序端；Worker 获得所选范围的短期 installation token。每次 Invocation 独立的 `GH_CONFIG_DIR/hosts.yml` 通过原子更新传递续签结果，同一 Agent 正常继承环境的后续 `gh` 子进程使用新 token。没有 gh 可执行文件替换、socket 请求协议或代理兼容入口。默认授权范围、收窄与迁移规则见 ADR 0008。临时失败有界重试；失效后保留进度进入 Execution Failure，修复同一引用后公开 resume，不自动换身份。

该设计不建设通用 sandbox，不承诺防御恶意 Agent、主动搜索宿主秘密、宿主文件污染或数据外泄。普通测试使用工具和服务替身；真实外部工具兼容性验证仍需另行执行，替身通过不代表真实工具兼容性已验证。

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
