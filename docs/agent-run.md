# agent-run 使用说明

`agent-run` 是显式启动的本地 Delivery Run 控制器。当前实现支持：

- 从 Parent Spec 启动或恢复 Delivery Run；
- 按 GitHub 原生依赖图确定性选择且始终只运行一个 Active Ticket Job；
- 让持久 Development Thread 实现、修复和生成发布语义；
- 为每轮候选创建全新的 Fresh Validation Thread 和一次性 Validation Checkout；
- 通过 Required Checks 与 Published-Head Gate 后，将 Ticket PR squash merge
  到 Run Branch，并显式关闭唯一 Primary Ticket；
- 每张 Ticket 完成后重新读取 GitHub，继续推进其他可执行分支；
- 在 Ticket 集合或依赖边变化时暂停，给出 Ticket Graph Change Summary，并把人工确认
  绑定到准确的新图版本；
- Parent Spec 变化时由一次性 Codex 生成 Scope Impact Assessment；澄清自动吸收，
  结构性变化绑定准确版本等待确认；
- 全部 Ticket 完成后进入 `run_acceptance_pending`，由独立 Reviewer 整体验收；通过后
  才进入 `run_publication_pending`，不提前创建最终 Run PR。

各智能角色的目标 Prompt 与证据合同见
[`docs/agents/agent-prompts.md`](agents/agent-prompts.md)。

## 命令

```bash
agent-run start <parent-issue> --repo OWNER/REPO
agent-run resume <run-id> --repo OWNER/REPO
agent-run confirm-structure <run-id> --repo OWNER/REPO
agent-run accept-run <run-id> --repo OWNER/REPO
AGENT_RUN_GITHUB_APP_ID=<app-id> \
AGENT_RUN_GITHUB_APP_INSTALLATION_ID=<installation-id> \
AGENT_RUN_GITHUB_APP_PRIVATE_KEY="$(cat /secure/agent-run-app.pem)" \
  agent-run deliver <run-id> --repo OWNER/REPO
```

`start` 只创建 Run、Run Branch 和工作前沿；`deliver` 从当前 Active Ticket 开始，
在同一进程中逐张交付完整 DAG。每张 Ticket 完成后都会重新读取 GitHub 权威状态，
重新计算 frontier；某条分支等待人工时，不依赖它的其他可执行 Ticket 仍会继续。
Required Checks 仍为 pending 时，命令保存 `waiting_checks` 状态并退出；稍后再次
执行同一个 `deliver` 命令即可继续，不会重复创建 PR 或消耗修改预算。
Required Check 失败时，Controller 将失败 check 的名称、workflow、描述和链接作为
原始 CI Evidence 交回同一 Development Thread。

当 `deliver` 返回 `run_acceptance_pending` 后，执行 `accept-run`。它在一次性、可写的
Validation Checkout 中派发全新的 Run Reviewer；Reviewer 不得复用任意 Ticket 的
Development/Reviewer Thread，必须读取 Parent Spec、最终 Ticket 图、Ticket completion
evidence、基线到 Run Head 的累计 diff 与预期 merge 结果，并进行实际 E2E、Standards、
Spec 三条验证 lane。失败 findings 原样交给持久 Run Repair Development Thread；每次真实
代码修改形成新的 Run Branch commit、废弃旧验收，再由全新 Reviewer 重新检查完整累计结果。
无代码变化不消耗预算，十次仍不能通过或确实需要人工决定时才进入 `ready_for_human`。
通过只进入 `run_publication_pending`，不会创建最终 PR 或合并默认分支。

Ticket title/body 在运行中变化时，旧开发结果、Publication 和 Acceptance 会失效；
Controller 沿用同一个 Ticket Job、Ticket Branch 和 Development Thread，从最新 revision
自动重启。Issue 评论不参与 revision，Controller 自身产生的本地修改也不会改变 revision。
Parent title/body 澄清会自动吸收并更新 Effective Revision。

原生 Sub-issue 集合或 `blockedBy` 边发生变化时，Run 进入
`structure_change_pending`。耐久状态中的 `graph_change_summary` 会列出新增/移除
Ticket 与依赖边；纯执行顺序调整不会改变 Ticket Graph Revision。维护者确认当前提议
版本后执行 `confirm-structure`；如果确认期间 GitHub 图再次变化，旧确认不会放行新图，
Run 会继续暂停并生成新的变化摘要。

Parent title/body 变化时，Controller 派发一次性 Codex，把新旧 Parent Spec、
当前 Ticket Graph 与已完成工作交给它生成 Scope Impact Assessment。非结构性澄清自动
吸收；改变 Ticket 集合、依赖、整体交付边界或使已完成工作需要返工的变化会进入同一个
`structure_change_pending`，人工确认同样只绑定当前准确的 Parent Spec Revision。

没有任何可执行 Ticket 时，Run 进入 `progress_exhausted`，并在
`diagnostics[].remaining_tickets` 中列出每张剩余 Ticket 的原因。`terminal_kind` 进一步
区分 `waiting_human`、`temporarily_no_work`、`permanent_blocked`、
`structure_change_pending`、`execution_failed` 和 `all_tickets_completed`。最后一种对应
`run_acceptance_pending`，它只是 Issue #5 Run Acceptance 的交接边界。

## 权限边界

Controller 使用专属 GitHub App 的 ID、installation ID 与私钥，按 worker 启动次数
创建短期 installation token。创建请求只申请 `metadata: read`、`issues: read`、
`pull_requests: read`，且只接受 GitHub 在同一响应中返回完全一致 permissions 的 token。
不要复用 Publisher 的写 token。Controller 启动 Codex worker 时会移除 App 私钥、
Publisher GitHub token、SSH agent 和交互式凭据入口，只向 worker 注入该短期只读 token，
并要求系统安装 `bubblewrap`。Codex 使用 YOLO 模式，可以读写宿主文件系统、联网以及
使用完整真实的 Git/`gh` CLI。Controller 只向 Brief 提供准确的任务身份和 SHA；
Development、Publication 与 Fresh Validation Codex 使用 Git/`gh` 自行读取 diff、
历史、Issue 和 PR 事实。

外层 bubblewrap 不是通用安全沙箱。它保留宿主和 checkout 的正常读写，只把权威
literal `.git`、resolved gitdir 与 common-dir 挂载为只读，并隐藏 Publisher GitHub
凭证、SSH agent 与交互式凭据入口。启动 Worker 前还会检查 local Git config 的全部
remote URL 和 push URL；带 userinfo 的 HTTP(S) URL 会以固定脱敏错误拒绝，普通 HTTPS、
SSH、scp-like 与本地 remote 保持可用，权威 config 不会被改写。这个边界用于防止可信
Worker 误写权威历史，不承诺抵抗恶意进程、主动搜索其他宿主凭据、宿主污染或数据外泄。
Codex 可以返回 Development Summary、Publication Artifact 或 Acceptance Artifact，
但发布动作只能由 Publisher 执行。

Development Codex 使用 `skill:implement` 完成实现、自测和真实 E2E，并派发不同
subagent 使用 `skill:code-review` 分别执行 Standards 与 Spec Review。它不能自行替代
缺失审查。Fresh Validation 不接收 Development Summary 或开发侧验证结论，在独立可写
Validation Checkout 中派发三个不同 subagent，分别完成 E2E、Standards 和 Spec 验证；
subagent 派发失败时必须解决问题并重新派发。
Controller 不解析 Codex 内部事件流来审计 subagent 身份或 skill 调用；它信任上述
Prompt 合同，并确定性校验 Fresh 父 Reviewer 不复用 Development/旧 Reviewer Thread、
三个 lane 均有合法状态和证据，以及外层 SHA/Revision 绑定。

Acceptance Artifact 只包含 `verdict`、`checks`、`findings` 和 `human_blockers`。
base/head SHA、Effective Revision 和 Reviewer 身份由 Controller 写入外层 Acceptance
Record。`findings` 直接回传同一 Development Thread，不再生成独立 Repair Brief。

Publisher 是唯一 Git/GitHub Mutation Authority，负责：

- 创建 GitHub 原生关联的 Ticket Branch；
- 创建 Candidate Commit 与保持 tree 不变的 Publication Commit；
- push、创建或更新唯一 Ticket PR；
- 检查 Required Checks 和准确 live head；
- 使用 `--squash --match-head-commit` 合并，并验证 integrated commit 的
  parent、tree 和标题；
- 显式关闭 Primary Ticket并记录 Run、PR 与 integrated commit。

每个 Ticket revision 最多允许十次产生真实 tree 变化的 Development Attempt。没有
代码变化的 Attempt 不消耗预算，但该 Ticket 会停止自动重试，Controller 先完成其他
可执行分支；预算耗尽后 Ticket 会移除 `ready-for-agent` 并增加 `ready-for-human`。

## 本地状态与清理

耐久状态位于 `.agent-run/runs/`。稳定 Development Checkout 位于
`.agent-run/worktrees/`：Required Checks pending 或 Worker/Publisher 普通失败、超时、
进程异常时保留，以恢复未提交成果；Ticket 完成、明确阻塞终止或操作者显式取消后清理。
Checkout 尚未准备完成时产生的部分目录也会清理。每轮独立 Validation Checkout 在验收
结束后完整删除，允许验收期间创建构建、测试和诊断中间产物。Codex 的临时 schema、输出
文件和空 GitHub 配置目录也会随子进程调用清理。

结构确认从 Ticket Set 移除已启动 Ticket 时，Controller 通过 Publisher 删除其稳定
checkout 与本地 Ticket Branch，并记录已退役的 branch generation。同号 Ticket 后续
重新加入会使用新的 branch generation，从当前 Run Branch 创建干净 Job，不继承旧
revision 的未提交文件或提交。确认期间图再次变化时，待清理 Ticket 会作为耐久义务
保留：最新图重新包含它则取消清理，最终确认的图仍不包含它才执行幂等清理。

Run state 以 `ticket_jobs` 按 Ticket 编号保留 Job-local thread、reviewer、PR、merge 与
blocker 历史；`active_ticket_job` 继续作为当前执行指针。刷新 Ticket Graph 可以把 active
切到新的可执行 Ticket，但不会删除同一 Parent Ticket Set 内已经 blocked 或 completed 的
Job。进程在两张 Ticket 之间退出或失败时，下一次 `deliver` 会从耐久状态与 GitHub live
事实对账恢复；已完成 Ticket 的 PR、merge、评论和关闭动作不会重复。没有可执行 Ticket
时，顶层 diagnostics 会汇总全部剩余 Job 与 GitHub blocker。
