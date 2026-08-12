# agent-run 使用说明

`agent-run` 是显式启动的本地 Delivery Run 控制器。当前实现支持：

- 从 Parent Issue 启动或恢复 Delivery Run；
- 按 GitHub 原生依赖图确定性选择且始终只运行一个 Active Ticket Job；
- 让持久 Development Thread 实现、修复和生成发布语义；
- 为每轮首次候选验收创建全新的 Fresh Validation Thread 和一次性 Validation Checkout；Human Blocker 恢复时复用原 Reviewer Thread 并重新准备 checkout；
- 通过 Required Checks 与 Published-Head Gate 后，将 Ticket PR squash merge
  到 Run Branch，并显式关闭唯一 Primary Ticket；
- 每张 Ticket 完成后重新读取 GitHub，继续推进其他可执行分支；
- 在 Ticket 集合或依赖边变化时 fail closed，给出 Ticket Graph Change Summary，并禁止
  Agent、Thread、Requeue 与 Publisher mutation；
- Parent title/body revision 作为机械 currentness 输入保留，不再运行语义范围分类器；
- 全部 Ticket 完成后进入 `run_acceptance_pending`，由独立 Reviewer 整体验收；通过后
  才进入 `run_publication_pending`，随后由一次性只读 Codex 生成最终 Run PR 语义。

各智能角色的目标 Prompt 与证据合同见
[`docs/agents/agent-prompts.md`](agents/agent-prompts.md)。

## 命令

日常操作优先使用 `run`。它会创建或幂等恢复同一 Parent Issue 的未完成 Run，并自动推进
到下一处需要人工处理的门禁：

```bash
agent-run run <parent-issue> --repo OWNER/REPO
agent-run status <run-id> --repo OWNER/REPO
agent-run history <run-id> --repo OWNER/REPO
```

需要逐阶段排障或控制时，使用底层生命周期命令：

```bash
agent-run start <parent-issue> --repo OWNER/REPO
agent-run deliver <run-id> --repo OWNER/REPO
agent-run resume <run-id> [--new-thread] [--message "..."] --repo OWNER/REPO
agent-run accept-run <run-id> --repo OWNER/REPO
agent-run publish-run <run-id> --repo OWNER/REPO
agent-run approve <run-id> --repo OWNER/REPO
agent-run revise <run-id> --message '未经改写的维护者反馈' --repo OWNER/REPO
agent-run abandon <run-id> --repo OWNER/REPO
AGENT_RUN_GITHUB_APP_ID=<app-id> \
AGENT_RUN_GITHUB_APP_INSTALLATION_ID=<installation-id> \
AGENT_RUN_GITHUB_APP_PRIVATE_KEY="$(cat /secure/agent-run-app.pem)" \
  agent-run run <parent-issue> --repo OWNER/REPO
```

`status` 和 `history` 默认输出便于人阅读的摘要；加入 `--json` 可获得稳定的机器可读输出。
Publication Invocation 在首个 Codex 进程启动前写入状态；`thread.started` 会在进程仍运行时
立即保存。`history --json` 的 `agent_invocations` 保留每次调用的 Work Subject、Generation、
输入指纹、Currentness Boundary、模式、requested/reported Thread、Output Attempt 数量、时间和
有界错误；它不保存 Prompt、transcript 或 Acceptance Artifact。Ticket、Parent-only 和 Run Repair
的 Development、Fresh Acceptance 与 Publication 都使用同一 Invocation seam：非法结构化输出会在
同一 Thread、只读 checkout 中最多修复两次，且不增加领域 attempt；进程失败不会自动重试或替换
Thread。`resume` 默认复用已保存 Thread，`--new-thread` 明确丢弃当前失败或 Human Blocker 阶段的
Thread 身份并使用标准阶段 Prompt 新开 Thread。`--message` 只允许用于当前 Human Blocker；它 trim
后必须非空、最多 8 KiB，以不可变 Human Response 绑定当前 Job Generation，并进入后续 Development
和 Fresh Acceptance 的权威上下文。当前 Generation 的响应按顺序保存、不按容量截断；替换
Generation 从空响应序列开始，绝不向新 Generation 注入旧响应。它不修改 Issue、不触发 Requeue、
也不等同于 `revise` 的 Run Feedback。
`run` 不会执行最终人工批准：到达 `run_approval_pending` 或 `parent_approval_pending` 后仍须
维护者检查最终 PR，再显式执行 `approve`。

`start` 只创建 Run、Run Branch 和工作前沿；`deliver` 从当前 Active Ticket 开始，
在同一进程中逐张交付完整 DAG。每张 Ticket 完成后都会重新读取 GitHub 权威状态，
重新计算 frontier；某条分支等待人工时，不依赖它的其他可执行 Ticket 仍会继续。
Required Checks 仍为 pending 时，命令保存 `waiting_checks` 状态并退出；稍后再次
执行同一个 `deliver` 命令即可继续，不会重复创建 PR 或消耗修改预算。
Required Check 失败时，Controller 将失败 check 的名称、workflow、描述和链接作为
原始 CI Evidence 交回同一 Development Thread。

当 `deliver` 返回 `run_acceptance_pending` 后，执行 `accept-run`。正常 Run Acceptance
Attempt 在一次性、可写的 Validation Checkout 中派发全新的 Run Reviewer；Reviewer 不得
复用任意 Ticket 的 Development/Reviewer Thread。它从 Parent Issue 与 GitHub 独立读取
最终 Ticket 集合和依赖，
检查准备好的累计 diff，并进行实际 E2E、Standards、Spec 三条验证 lane；Completion Record、
Expected Merge Result 与 SHA/Revision 绑定只由 Controller 在验收外层校验，不进入 Codex
Prompt。失败 findings 原样交给持久 Run Repair Development Thread；每次真实
代码修改形成新的 Run Branch commit、废弃旧验收，再由全新 Reviewer 重新检查完整累计结果。
仅当 Run Reviewer 报告 Human Blocker 后执行 `resume` 时，Controller 复用刚刚被阻塞的
Reviewer Thread，但仍创建新的 Validation Checkout，并要求它重新读取权威状态和重新验收。
无代码变化不消耗预算，十次仍不能通过或确实需要人工决定时才进入 `ready_for_human`。
通过只进入 `run_publication_pending`，不会创建最终 PR 或合并默认分支。随后执行
`publish-run`：正常 Run Publication Attempt 由新的、只读的 Run Publication Codex 根据
Parent Issue、累计 diff 与 Fresh Run Acceptance 生成最终 PR 叙事；仅 Human Blocker resume
复用刚刚被阻塞的 Run Publication Thread，并要求它重新读取权威状态。Publisher 维护同一个
Run Branch → 默认分支的最终 PR，并渲染 Parent Issue、Delivery Type 及每张已完成 Ticket 的链接；它将
Parent/Graph revision、Run/default/PR head 与预期 merge tree 写入独立 Publication Record。
Required Checks 全部通过（或没有配置）后状态才变为
`run_approval_pending`；即使此时所有自动检查通过，也只有 `approve` 会执行普通 merge
commit。合并结果与已验收的预期 merge tree 一致后，Publisher 记录可重试的 Parent
closeout 审计评论并显式关闭 Parent Issue。

`approve` 每次都会重新读取 Parent/Graph revision、Run Branch、默认分支、PR head、Fresh
Acceptance 与 Required Checks。任一漂移都会拒绝旧批准：可合并的默认分支漂移回到 fresh
Run Acceptance；真实 merge conflict 与最终 PR Required Checks 失败会排入同一个有界 Run
Repair 引擎。`revise` 原样保存维护者反馈、重置一个新的十次实际变更预算，并同样回到
Run Repair → fresh Run Acceptance → 新 PR 语义。`abandon` 先把
`abandonment_pending` 与逐项恢复义务写入耐久状态，再幂等关闭未合并的自动化 PR、只重开
带有本 Run Publisher close 证据且尚未进入默认分支的 Ticket。任一步响应丢失后，其他生命周期
命令都不会恢复正常发布；重复 `abandon` 会在 GitHub 暴露精确 close 或外部 transition 后继续
收敛。若进程恰在耐久 dispatch boundary 与远端 close 之间退出、GitHub 又尚无可判定 event，
Run 保持 `abandonment_pending`，不会猜测 ownership 或制造新的 close。完成后保留 Run state、
已发布 PR 与其他远端审计事实，并通过 Git worktree 操作删除该 Run 的本地临时 worktrees。

Ticket title/body 在运行中变化时，旧开发结果、Publication 和 Acceptance 会失效；
Controller 沿用同一个 Ticket Job、Ticket Branch 和 Development Thread，从最新 revision
自动重启。Issue 评论不参与 revision，Controller 自身产生的本地修改也不会改变 revision。
Parent title/body 变化会更新 observed Parent revision，同时保留 accepted revision，供后续
Job Generation currentness 路由使用；Controller 不再派发 Scope Impact Assessment Codex。

原生 Sub-issue 集合或 `blockedBy` 边发生变化时，Run 进入
`unsupported_scope_change`。耐久状态中的 `unsupported_scope_change` 保存 accepted/observed
Graph revision、`graph_change_summary` 与 observed graph；纯执行顺序调整不会改变 Ticket
Graph Revision。`status --json` 与 `history --json` 可审计这些事实。

该状态不允许 `run`、`resume`、`deliver`、`accept-run`、`publish-run`、`approve` 或
`revise` 越过边界，不自动 Requeue，不创建 Codex Thread，也不执行 Git/GitHub Publisher
mutation。MVP 不提供 `confirm-structure`；操作者只能恢复 GitHub 原图后重新核验，或执行
`abandon` 停止 Run。

没有任何可执行 Ticket 时，Run 进入 `progress_exhausted`，并在
`diagnostics[].remaining_tickets` 中列出每张剩余 Ticket 的原因。`terminal_kind` 进一步
区分 `waiting_human`、`temporarily_no_work`、`permanent_blocked`、
`unsupported_scope_change`、`execution_failed` 和 `all_tickets_completed`。最后一种对应
`run_acceptance_pending`，它只是 Issue #5 Run Acceptance 的交接边界。

## 权限边界

Controller 使用专属 GitHub App 的 ID、installation ID 与私钥，按 worker 启动次数
创建短期 installation token。创建请求只申请 `metadata: read`、`issues: read`、
`pull_requests: read`，且只接受 GitHub 在同一响应中返回完全一致 permissions 的 token。
不要复用 Publisher 的写 token。Controller 启动 Codex worker 时会移除 App 私钥、
Publisher GitHub token、SSH agent 和交互式凭据入口，只向 worker 注入该短期只读 token，
并要求系统安装 `bubblewrap`。Codex 使用 YOLO 模式，可以读写宿主文件系统、联网以及
使用完整真实的 Git/`gh` CLI。Controller 只向 Brief 提供适用的 Parent/Ticket URL 和
不可重建的原始失败证据；Revision、SHA、Run/Thread/Attempt 等身份只留在确定性账本中。
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

## 自托管开发

使用 `agent-run` 开发本仓库时，运行中的 Controller 必须来自已验证且固定的 commit，
不得从正在被 Worker 修改的 editable checkout 导入代码。推荐把 Runner 安装到按 commit
SHA 命名的独立 Python 环境，并从专用干净 clone 启动；同一 Delivery Run 从开始到完成始终
使用同一个 Runner。最终 PR 合入默认分支并完成全量验证后，才创建下一版 Runner。

仓库 CI 的 Required Check 名称是 `quality`。GitHub Ruleset 应覆盖默认分支以及 Ticket PR、
Run Repair PR 所针对的 Run Branch。没有 Required Checks 时 Controller 会按无托管 CI 继续，
因此正式自托管前必须核验目标分支确实应用了该 Ruleset，而不是只确认 workflow 文件存在。

当前版本建议拆分两个 Active branch Ruleset：

- 默认分支规则显式包含 `refs/heads/main`，要求 PR 和 `quality`，禁止 force push 与删除；
- Run Branch 规则显式包含 `refs/heads/agent-run/**/run`，要求 `quality`，但允许分支创建时
  暂无 status check，并允许交付完成后的受控删除。

不要使用 `~DEFAULT_BRANCH` 代替显式 `main`；当前 Controller 只特殊支持 `~ALL`。Required
Check 暂时选择 Any source，不绑定 GitHub Actions App：当前版本遇到非空 `integration_id`
会以 `github_unsupported_ruleset` 安全拒绝继续。Any source 允许具备写权限的其他主体提交同名
status，因此仍应限制仓库写权限，并保持 Worker 只持有短期只读 App token。Ruleset 启用前先
让 `quality` 在仓库中成功运行一次，启用后再通过 API 读回实际条件和 Required Check。

GitHub App 的创建、安装和私钥保管不属于 Controller 自动化范围。App 必须只授予
`metadata: read`、`issues: read`、`pull_requests: read`，私钥保存在仓库和 Runner checkout
之外；Publisher 继续使用独立的宿主 `gh` 写凭据。

## 本地状态与清理

耐久状态位于 `.agent-run/runs/`。稳定 Development Checkout 位于
`.agent-run/worktrees/`：Required Checks pending 或 Worker/Publisher 普通失败、超时、
进程异常时保留，以恢复未提交成果；Development/Repair Codex 报告 Human Blocker 时也保留，供同一 Thread 在 `resume` 后重新核验并继续。Ticket 完成、非恢复性的明确终止或操作者显式取消后清理。
Checkout 尚未准备完成时产生的部分目录也会清理。每轮独立 Validation Checkout 在验收
结束后完整删除，允许验收期间创建构建、测试和诊断中间产物。Codex 的临时 schema、输出
文件和空 GitHub 配置目录也会随子进程调用清理。

Graph drift fail closed 时，Controller 保留当前 Candidate、Acceptance、checkout、branch、
PR 与 Ticket completion，不静默清理或改写。只有正常完成或 `abandon` 路径可以按既有
Publisher/cleanup 权限处理托管资源；观察到的新 Graph 本身不产生任何 mutation。

Run state 以 `ticket_jobs` 按 Ticket 编号保留 Job-local thread、reviewer、PR、merge 与
最近 16 次 blocker 历史；每次 Human Blocker 最多 8 条、每条最多 2000 个字符。成功恢复后
当前 `human_blockers`、phase 与 resume 输入会清除，timeline 只保留已经发生的历史事件且
继续受全局容量限制。`active_ticket_job` 继续作为当前执行指针。仅当 Ticket Graph revision
未变化时，Controller 才会在既定 Ticket Set 内把 active 切到新的可执行 Ticket；它不会删除
已经 blocked 或 completed 的 Job。Graph revision 变化按上文进入
`unsupported_scope_change`，不更新 active。进程在两张 Ticket 之间退出或失败时，下一次
`deliver` 会从耐久状态与 GitHub live 事实对账恢复；已完成 Ticket 的 PR、merge、评论和关闭
动作不会重复。没有可执行 Ticket 时，顶层 diagnostics 会汇总全部剩余 Job 与 GitHub blocker。
