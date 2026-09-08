# agent-run 使用说明

`agent-run` 是显式启动的本地 Delivery Run 控制器。当前实现支持：

- 从 Parent Issue 启动或恢复 Delivery Run；
- 按 GitHub 原生依赖图确定性选择且始终只运行一个 Active Ticket Job；
- 让持久 Development Thread 实现和修复，并由独立、只读 Publication Codex 生成发布语义；
- 为每轮首次候选验收创建全新的 Fresh Acceptance Thread 和一次性 Validation Checkout；Human Blocker 恢复时复用原 Reviewer Thread 并重新准备 checkout；
- 以 [Acceptance Artifact Schema](acceptance-artifact-schema.md) 约束 Ticket 与 Run Reviewer 共用的三条验收 lane 输出；
- `run` 与 `resume` 都通过同一 Task Control 和独立 Executor 连续推进；恢复失败 Invocation 或 Human Blocker 后形成的 Candidate、PR 与 Required Checks 由同一 Executor 监督到下一真实边界，发起终端退出或 Ctrl-C 只离开观察；
- Required Checks 与 GitHub 事件等远端异步状态在单次等待预算到期后进入可恢复的监督超时暂停；GitHub 读取或对账的未知非零退出同样在不解析 stderr 原因的前提下进入有界、封顶退避监督。维护者显式执行同一 Parent 的 `run <parent-issue>` 或 `resume <parent-issue>` 开始新的等待窗口，不需要另启 watcher；
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
agent-run status --parent <parent-issue>
agent-run history --parent <parent-issue>
agent-run runs --repo OWNER/REPO
```

当前仓库只有一个进行中 Run 时，`status` 与 `history` 可以省略 Run 选择参数；在任意目录使用
`--repo OWNER/REPO` 时必须同时提供 `--parent <parent-issue>`。多个候选时命令会列出工作目录、
Parent、状态和开始时间并停止，不按最近时间猜测。

### Delivery Policy

Delivery Policy 的取值优先级是内置默认值、用户级默认值、单次命令覆盖；仓库内容不能覆盖
操作者的个人成本策略。用户级默认值保存在 `$XDG_CONFIG_HOME/agent-run/delivery-policy.json`
（未设置时为 `~/.config/agent-run/delivery-policy.json`），可用公开命令查看或配置：

```bash
agent-run policy show
agent-run policy configure --ticket-review-rounds 3 --parent-only-paired-rounds 10 --run-repair-rounds 10 --review-deadline 2h
agent-run run <parent-issue> --ticket-review-rounds 1 --development-deadline 30m
```

Ticket 的语义 Review 轮数为 `N` 时，普通 Development 为 `N+1` 次、Reviewer 为 `N` 次；默认
为 `4/3`，最后一次普通 Development 不再进入 Review，并保留有条件的一次 Final CI-fix。
Parent-only 的配对轮数为 `N` 时，Development 与 Reviewer 都最多执行 `N` 次并严格配对；默认
为 `10/10`，Reviewer 失败不会转入 Ticket Fallback，而是在当前窗口耗尽后等待显式 Resume。
Run Repair 的修复轮数为 `N` 时，首次整体 Reviewer 加最多 `N` 次 Development 和 `N+1` 次
Reviewer；默认准确执行 `D10/R11`。第 `N+1` 次 Reviewer 仍有 Finding 时进入 Review Budget
Checkpoint，不使用 Final CI-fix 或未经 Reviewer 验收的 Publication Authority。
正整数轮数和正 duration 在创建 Worker、PR 或部分状态之前校验。每个新 Run 以及显式开启的
新 Budget Window 都把实际生效的完整策略保存为 Policy Snapshot；之后修改用户级默认值不会
改变活动 Run 或活动预算窗口。缺少或不完整 Snapshot 的旧状态会 fail closed。
Invocation 默认 deadline 为 Development 5 小时、Review 2 小时、Publication 1 小时；同一
Invocation 内的初始调用和 Output Repair 共用该 deadline。

在人工边界使用对应的公开命令：

```bash
agent-run resume <parent-issue> [--new-thread] [--message "..."] --repo OWNER/REPO
agent-run requeue <parent-issue> --repo OWNER/REPO
agent-run approve <parent-issue> --repo OWNER/REPO
agent-run revise <parent-issue> --message '未经改写的维护者反馈' --repo OWNER/REPO
agent-run stop <parent-issue> --repo OWNER/REPO
agent-run abandon <parent-issue> [--discard-worktree] --repo OWNER/REPO
```

### GitHub 只读身份

没有 App profile 时，Worker GitHub Read Broker 默认使用宿主已经登录的 `gh`，不会启动登录、刷新或
读取 `gh auth token`。需要独立最小权限身份时，一次性保存 App 元数据和仓库外私钥路径：

```bash
agent-run auth status
agent-run auth app configure \
  --app-id <app-id> \
  --installation-id <installation-id> \
  --private-key /secure/agent-run-app.pem
agent-run auth app remove
```

配置文件位于 XDG 用户配置目录，只保存 App ID、Installation ID 和解析后的私钥绝对路径；私钥内容与
短期 installation token 不会复制或落盘。App profile 存在但损坏或私钥不可用时会 fail closed，不会回退
到宿主 `gh`。`auth` 命令可以从任意目录执行，不经过 Git discovery 或生命周期门禁。

### 顶层 Codex 执行配置

新 Run 默认使用 `economy`：Development 为 `gpt-5.6-luna/xhigh`，Review 为
`gpt-6-astra/low`，Publication 引用 Development。`premium` 的 Development 和 Review 均为
`gpt-6-astra/low`，Publication 独立使用 `gpt-5.6-luna/xhigh`。创建时选择预设：

```bash
agent-run run <parent-issue> --preset premium
```

已有 Run 的配置通过独立命令创建新的 Profile Revision；它不会推进 Run、启动 Agent 或改变已有
Thread。Publication 的初始引用关系由预设决定；独立配置即使切换 preset 也保持独立，只有明确
恢复引用才重新跟随 Development：

```bash
agent-run configure <parent-issue> --development-model gpt-5.6-luna
agent-run configure <parent-issue> --publication-model gpt-5.6-sol
agent-run configure <parent-issue> --publication-from-development
```

Profile Revision 同时保留各角色的 preset 与显式覆盖来源。Publication 从 Development
解除引用时，仍沿用的 Development 显式字段会记录在 `provenance.inherited` 中，避免仅凭当前
preset 误解其实际值来源。

同一 Thread 的 Resume 与 Output Repair 始终使用原绑定；配置只影响后来创建的 Thread。普通
`status` 在运行中的顶层 Codex 只显示面向操作者的角色、model 与 reasoning effort；Thread、Profile
Revision、绑定来源及其他内部身份只由 `status --json` 的 Machine Audit View 提供。空闲时普通视图
显示当前没有运行中的 Agent；`history --json` 保留每次启动的完整审计事实，包括仍在运行中的
Invocation。

会触发代码修复的 Required Check 必须由 GitHub Actions job API 准确绑定当前 PR head，且失败
只发生在仓库 `pyproject.toml` 显式列出的稳定 `workflow::name::step`；缺少或矛盾的
job/step 事实、runner/network 等平台步骤失败和未配置 step 都会 fail closed 地留在 Controller 监督：

```toml
[tool.agent-run.required-checks]
code-failure-steps = [
  "CI::quality::Run tests",
  "CI::quality::Run type checks",
]
```

`status` 和 `history` 默认输出便于人阅读的摘要；加入 `--json` 可获得稳定的机器可读输出。处于
外部等待或监督超时时，两种格式均显示等待种类、对象、head/base、窗口开始与截止、剩余时间、
重试次数、脱敏的最新观测，以及超时后的唯一恢复操作。前台等待每个轮询间隔至多输出一次同样
脱敏的进度记录，不会打印凭据或原始响应体。
在前台窗口仍运行时，这个恢复操作仅说明窗口到期或进程中断后的下一步，不要求维护者重复输入
`run`；当前进程会继续自行监督。
Semantic Agent Attempt 在预算门禁通过后、首个 Codex 进程启动前写入状态；Development 与 Reviewer
Attempt 绑定当时的 Review Budget Window，Publication Attempt 则不绑定该窗口。一次 Attempt 可包含
初始 Invocation、同 Thread 的 Output Repair、进程失败后的 successor Invocation、Human Blocker Resume
以及显式 `--new-thread`；这些恢复都沿用同一 Attempt ID 和领域计数，不能重新获得预算。
Publication Invocation 在首个 Codex 进程启动前写入状态；`thread.started` 会在进程仍运行时
立即保存。`history --json` 的 `agent_invocations` 保留每次调用的 Work Subject、Generation、
输入指纹、Currentness Boundary、模式、requested/reported Thread、Output Attempt 数量、时间和
有界错误；`semantic_agent_attempts`、`output_attempts`、`budget_windows` 与
`publication_operation_retries` 分别展示语义工作、输出修复、预算和外部发布操作重试，不把这些
层级混成一个计数。每次公共 `resume` 另存独立授权事件；`status --json` 的 `latest_resume` 显示
最近一次，`history --json` 的 `agent_resumes` 显示每次授权的原因、Thread、Attempt 与 successor
关联，`resume_audit` 则显示总数和滚动摘要。基础 Resume 授权审计保留每次 Resume 的完整小型事实且
不限制次数；独立的不可变 Human Response 审计按 Resume identity 保留维护者消息，统一 `events`
投影在 JSON 中保留完整响应，普通文本只显示确定性截断的摘要。审计不保存原始错误文本、Prompt、transcript 或
Acceptance Artifact。
Ticket、Parent-only 和 Run Repair
的 Development、Fresh Acceptance 与 Publication 都使用同一 Invocation seam：非法结构化输出会在
同一 Thread、只读 checkout 中最多修复两次，且不增加领域 attempt；进程失败不会自动重试或替换
Thread。`resume` 默认复用已保存 Thread，`--new-thread` 只替换 Invocation/Thread，不替换 Semantic
Attempt，并使用标准阶段 Prompt 新开 Thread。`--message` 只允许用于当前 Human Blocker；它 trim
后必须非空、最多 8 KiB，以不可变 Human Response 绑定当前 Job Generation，并进入后续 Development
和 Fresh Acceptance 的权威上下文。当前 Generation 的响应按顺序保存、不按容量截断；替换
Generation 从空响应序列开始，绝不向新 Generation 注入旧响应。它不修改 Issue、不触发 Requeue、
也不等同于 `revise` 的 Run Feedback。
`resume` 恢复当前 failed 或 Human Blocker Invocation，也可以从 `supervision_timeout` 为同一等待身份开启新窗口；后者不接受 `--new-thread` 或 `--message`，不会创建 Worker、PR 或 merge。Ticket 与 Parent-only Change Job 的
preflight 若发现 requirements 或 base/head 已 stale，绝不启动 Codex，而是进入
`requeue_required`。此状态下只允许 `status`、`history`、`requeue` 与 `abandon`。Run Repair 的
Parent、Graph、Ticket Completion 或 Run Branch 边界发生漂移时，不创建 replacement generation；
它会丢弃旧 Repair 并回到 fresh Run Acceptance，再判断 Repair 是否仍然必要。
`requeue` 在执行时重新读取 GitHub 和 Git 权威事实，封存旧 Job Generation、关闭其仍开放的
自动化 Change PR，并创建不携带旧 Candidate、Acceptance 或 Thread 的新 generation；它不自动
rebase，也不继续旧 worktree。Run Acceptance 或 Final Run Publication 不拥有 generation-local
branch/PR；它们的 Invocation 在 Parent、Graph、Ticket Completion、Run Branch 或 default base
漂移后会废弃旧结果并回到 `run_acceptance_pending`，由全新的 Run Reviewer 验收，而不是 Requeue
或只重写 PR 文案。
若 PR 的 live head/base、Candidate/Acceptance 绑定或外部状态无法与当前 Generation 对齐，控制器
fail closed 为 Human Blocker，不将未知外部变更误路由为 Requeue。Run Repair 还绑定 Parent、Graph、
Run Branch、Ticket Completion Records，以及触发它的 Final Run PR state/head/base 与 Required Checks
evidence；任一可重建输入变化都先回到 fresh Run Acceptance 判断 Repair 是否仍然必要。
`run` 不会执行最终人工批准：到达 `run_approval_pending` 或 `parent_approval_pending` 后仍须
维护者检查最终 PR，再显式执行 `approve`。

### 命令边界与状态轮转

下表是 `run/resume/requeue/status/history/approve/revise/stop/abandon` 的稳定操作合同。Controller
内部阶段不是维护者操作；它们不能越过下表中的人工边界。

| 命令 | 允许的起点 | 作用 | 不做什么 |
| --- | --- | --- | --- |
| `run` | 新 Run、正常可推进状态或监督超时暂停 | 创建或继续正常 Job Loop；在 checks、GitHub 读取/对账未收敛时在本次调用内监督，至 Human Blocker、`execution_failed`、`requeue_required`、范围变化或最终批准边界为止 | 不隐式恢复失败的 Agent Invocation、Requeue、批准或合并 |
| `resume` | 当前唯一 Agent Invocation 为 `execution_failed`、当前唯一对象为 Human Blocker、`operator_stopped`，或当前 `supervision_timeout` 有受支持等待边界 | 通过统一 Executor 在同一 Semantic Attempt 内创建 successor Invocation，并连续推进后续自动工作到下一真实边界；解除 Stop 时恢复原现场 | 不增加领域 attempt、不重置预算或 Publication Operation Retry；超时恢复不创建 Worker、PR 或 merge；发起 CLI 退出不停止 Executor |
| `requeue` | 仅 `requeue_required` | 从命令时读取的最新权威事实创建新 Generation，并封存旧 Generation | 不 rebase、不迁移 Candidate/Acceptance/Human Response/Thread/worktree |
| `status` / `history` | 任意已知 Run | 分层查看 Attempt、Invocation、Output Attempt、Budget Window、Publication Operation Retry 与下一步 | 不改变状态或恢复工作 |
| `approve` | `run_approval_pending` 或 Parent-only 的 `parent_approval_pending` | 重新核验当前事实后，授权 Publisher 合并最终 PR | 不跳过 Fresh/Run Acceptance、Required Checks 或 Published-Head Gate |
| `revise` | `ready_for_human` 或 `run_approval_pending` | 原样保存 Run 级维护者反馈，并进入 Run Repair | 不是 Human Blocker 的响应通道 |
| `stop` | 任意已知、未终止且有活动 Executor 的 Run | 原子撤销旧 Executor 写入权，定向终止其 Worker 进程组，并把 Run 置为可显式恢复的 `operator_stopped` | 不把 Stop 记为失败，不删除 checkout、Thread、Attempt 或 Artifact；确认无活动 Executor 及重复 Stop 都严格只读 |
| `abandon` | 未完成 Run（包括人工边界） | 先检查所有 Managed Development Checkout；干净时写入 durable abandonment，再执行受限的 PR 关闭、Ticket reopen 与本地清理恢复 | 不回滚默认分支；dirty checkout 默认不删且不执行 GitHub mutation，只有显式 `--discard-worktree` 才强制丢弃 |

### Output Repair、Resume 与 Requeue

三者按失败层级分开，不可替换：

- **Output Repair**：同一个 Invocation 的 Codex 进程零退出、Thread 身份正确，但最终结构化输出不符合完整阶段 contract 时自动执行。它最多追加两次同 Thread、只读的输出请求；不创建新的 Invocation，也不增加领域 attempt。
- **Resume**：进程、凭据、sandbox、timeout、signal、非零退出、缺少最终输出或 Thread mismatch 导致 `execution_failed`，或 Agent 成功给出 Human Blocker 时，由维护者显式在当前 Semantic Attempt 内启动 successor Invocation。
- **Requeue**：Currentness Boundary 已经 stale 时替换整个 Job Generation。它不是失败进程的 retry；Ticket 与 Parent-only Change Job 只有在 `requeue_required` 才能执行，Run Acceptance 与 Final Run Publication 的漂移则回到 fresh Run Acceptance。

默认 Resume 在仍 current 且保存了 Thread ID 时复用同一 Thread。日常恢复使用 Parent 位置参数：

```bash
agent-run status --repo OWNER/REPO --parent <parent-issue> --json
agent-run resume <parent-issue> --repo OWNER/REPO
agent-run approve <parent-issue> --repo OWNER/REPO
agent-run history --repo OWNER/REPO --parent <parent-issue> --json
```

`resume` 只选择唯一可恢复 Run，`approve` 只选择唯一处于最终批准门禁的 Run；零匹配或多匹配都
拒绝猜测。完整 Run ID 与显式 `--state-dir` 仍可用于自动化和精确排障；它们不会参与 Parent 的
模糊选择。

只有维护者明确要丢弃当前 Invocation 上下文，或没有可恢复 Thread ID 时才使用新 Thread；它仍属于
原 Semantic Attempt，因此不会增加 Development、Reviewer 或 Publication 计数。新 Thread
接收该阶段完整标准 Prompt，不会得到“接替上一位 Agent”的手工交接叙述：

```bash
agent-run resume <parent-issue> --new-thread --repo OWNER/REPO
```

Human Blocker 与失败共用 Resume UX，但只有 Human Blocker 可以附带不可变、未经改写的维护者响应：

```bash
agent-run resume <parent-issue> \
  --message '已授权使用内部测试仓库；继续当前验收。' \
  --repo OWNER/REPO
```

`--message` 会 trim 校验为非空、限制为 8 KiB，并按顺序绑定当前 Job Generation；它会进入后续
Development 与 Fresh Acceptance 的权威上下文。它不修改 Issue、不触发 Requeue，也不能替代
`revise --message` 的 Run 级反馈。若 Resume preflight 发现 stale，命令不会运行 Codex，而是返回
`requeue_required`；此时只能先查看状态/历史，再由维护者显式 Requeue：

```bash
agent-run status --repo OWNER/REPO --parent <parent-issue>
agent-run requeue <parent-issue> --repo OWNER/REPO
```

`run` 创建或恢复 Run、Run Branch 和工作前沿，并由准确的 Run Executor 逐张交付完整 DAG。每张 Ticket 完成后都会重新读取 GitHub 权威状态，
重新计算 frontier；某条分支等待人工时，不依赖它的其他可执行 Ticket 仍会继续。
Required Checks 仍为 pending 时，`run` 在有限窗口内监督；窗口到期后保存
`supervision_timeout`，状态提示的恢复操作是 `resume`（同一 Parent 的显式 `run` 同样允许）；两者均不会重复创建 PR 或消耗修改预算。
Required Check 失败时，Controller 读取失败 check 的名称、workflow、描述和链接，并读取其
Actions job 的当前 head、状态与逐 step conclusion；只有仓库配置明确声明的 code/test step
被该结构化事实证明失败时，才将原始 CI Evidence 交回同一 Development Thread，其他情况保持监督。

新版本创建 Run 时，本机 Run 定位索引记录其 Run ID、仓库根和 `.agent-run` state 目录，最多保留
最近 32 条，不回填或迁移历史 Run。因此，`runs` 可以按当前仓库或显式 `--repo` 发现候选；
`status`、`history` 可以按当前仓库的唯一进行中 Run、`--parent`，或任意目录的
`--repo + --parent` 选择。若没有唯一候选、存在多个 clone、索引失效或冲突，命令会列出候选并
停止，绝不按最近时间猜测或全盘搜索。普通 mutation 与 Run-scoped `configure` 同样使用 Parent
位置参数并执行零匹配、多匹配和 repository mismatch 检查；完整 Run ID 与显式 state 目录仅是
自动化和精确排障入口。改变 Run 的命令仍必须从目标仓库运行。

Lifecycle mutation 默认输出面向维护者的回执，只显示仓库、Parent、操作、是否附着原操作、动作是否
已应用、当前交付状态与下一步；动作应用完成不代表整个交付已经完成。Action ID、Run ID、Executor
generation、payload digest 等稳定机器审计事实只在显式 `--json` 输出中提供。

如果 merge 的写入响应出现网络错误或无法解析的响应，Publisher 不盲目重放：先在 GitHub 对账。PR 已
合并即恢复成功；PR 仍 OPEN 且 live head/base、Required Checks 与 mergeability 均保持当前时，最多重试
同一个带精确 head 绑定的 merge intent 三次；任一状态矛盾则停止等待人工处理。

当所有 Ticket 完成后，`run` 自动进入 `run_acceptance_pending` 并推进 Run Acceptance。
正常 Run Acceptance Attempt 在一次性、只读的 Validation Checkout 中派发全新的 Run Reviewer；Reviewer 不得
复用任意 Ticket 的 Development/Reviewer Thread。它从 Parent Issue 与 GitHub 独立读取
最终 Ticket 集合和依赖，
检查准备好的累计 diff，并形成实际 E2E、Standards、Spec 三条独立验证 lane。E2E 默认负责代码
稳定后的广泛运行验证，Standards 与 Spec 默认使用静态证据和验证具体问题所需的最小命令；
`skill:code-review` 是可使用的推荐 SOP，且不得由父 Reviewer 替代缺失 lane。Run Reviewer 不得修改 Validation Checkout；
需要写入的构建、测试与验证中间产物必须放在 checkout 外可定位、仅服务本轮且结束前清理的临时路径。
Reviewer 不得修复源码、测试、配置或 `.gitignore`。Ticket Completion
Revision 按 Ticket number 数值排序，且只绑定已集成 SHA、冻结 Effective Revision 与已验收
base/tree；已关闭 Ticket 后续 title/body 编辑不改变该版本，普通 reopen 则 fail closed。
Completion Record、
Expected Merge Result 与 SHA/Revision 绑定只由 Controller 在验收外层校验，不进入 Codex
Prompt。失败 findings 原样交给持久 Run Repair Development Thread；每次真实
代码修改先形成不可变 repair Candidate，由全新的 Candidate Run Acceptance Reviewer 按完整 Parent 范围
检查准确 default head 与 Candidate 的预期合并结果。Candidate 通过后仍须经过 repair PR Publication、
Required Checks、Published-Head Gate 与实际合入；Controller 只在 Candidate tree、repair base、实际 Run tree、
default head、Parent/Graph revision 和 Ticket Completion records 全部精确匹配时提升该结论。若仅 default head
前进，Controller 在原 Repair Cycle 重新预演并验收最新组合；其余权威边界失配才废弃 Candidate，并进入新的
Run Acceptance Generation。
仅当 Run Reviewer 报告 Human Blocker 后执行 `resume` 时，Controller 复用刚刚被阻塞的
Reviewer Thread，但仍创建新的 Validation Checkout，并要求它重新读取权威状态和重新验收。
无代码变化不消耗预算，配置的 `N` 次仍不能通过或确实需要人工决定时才进入 `ready_for_human`。
通过只进入 `run_publication_pending`，不会创建最终 PR 或合并默认分支。`run` 随后推进
正常 Run Publication Attempt：由新的、只读的 Run Publication Codex 根据
Parent Issue、累计 diff 与 Fresh Run Acceptance 生成最终 PR 叙事；仅 Human Blocker resume
复用刚刚被阻塞的 Run Publication Thread，并要求它重新读取权威状态。Publisher 维护同一个
Run Branch → 默认分支的最终 PR，并渲染 Parent Issue、Delivery Type 及每张已完成 Ticket 的链接；它将
Parent/Graph revision、Run/default/PR head 与预期 merge tree 写入独立 Publication Record。
Publication Agent 返回合法叙事时 Semantic Attempt 即完成；之后 GitHub 写入、读取与对账失败只增加
独立且有界的 Publication Operation Retry。该重试耗尽后进入硬 `publication_pending` 边界，`resume`
不会清零、绕过或重新生成叙事。
Required Checks 全部通过（或没有配置）后状态才变为
`run_approval_pending`；即使此时所有自动检查通过，也只有 `approve` 会执行普通 merge
commit。合并结果与已验收的预期 merge tree 一致后，Publisher 记录可重试的 Parent
closeout 审计评论并显式关闭 Parent Issue。

`approve` 每次都会重新读取 Parent/Graph revision、Run Branch、默认分支、PR head、Fresh
Acceptance 与 Required Checks。任一漂移都会拒绝旧批准：可合并的默认分支漂移重新验收准确最新组合，
真实 merge conflict 与最终 PR Required Checks 失败会排入同一个有界 Run Repair 引擎。Run Repair 遵循
Repair → Candidate Run Acceptance → 严格 promotion 的路径；若 promotion 的 Candidate、repair base、实际
Run tree、Parent、Graph 或 Completion 任一非 default 绑定失配，才回退到 fresh Run Acceptance。若只有
default head 前进，则在同一 Repair Cycle 验收最新组合且不重置代码修改预算；已集成 Job 在 revalidation 中又收到 Finding 时，
Controller 保留 Repair Thread、Integration-repair Worktree 与计数，归档旧 Job/PR 并轮转新的 branch/PR。
`revise` 原样保存维护者反馈、重置一个新的 `N` 次实际变更预算，并进入同一 Candidate/promotion 语义。`abandon` 先把
`abandonment_pending` 与逐项恢复义务写入耐久状态，再幂等关闭未合并的自动化 PR、只重开
带有本 Run Publisher close 证据且尚未进入默认分支的 Ticket。任一步响应丢失后，其他生命周期
命令都不会恢复正常发布；重复 `abandon` 会在 GitHub 暴露精确 close 或外部 transition 后继续
收敛。若进程恰在耐久 dispatch boundary 与远端 close 之间退出、GitHub 又尚无可判定 event，
Run 保持 `abandonment_pending`，不会猜测 ownership 或制造新的 close。完成后保留 Run state、
已发布 PR 与其他远端审计事实，并通过 Git worktree 操作删除该 Run 的本地临时 worktrees。

Ticket 或 Parent title/body、以及 Change Job 的适用 base/head 在运行中变化时，旧开发结果、
Publication 和 Acceptance 会失效。Controller 不再原地重置或让旧 Thread 解释变化：它进入
`requeue_required`，等待维护者显式执行 `requeue`。Issue 评论不参与 revision；Controller 自身
产生的本地修改也不会改变 revision。Parent title/body 变化会更新 observed Parent revision，
同时保留 accepted revision，供 Job Generation currentness 路由使用；Controller 不再派发
Scope Impact Assessment Codex。

原生 Sub-issue 集合或 `blockedBy` 边发生变化时，Run 进入
`unsupported_scope_change`。耐久状态中的 `unsupported_scope_change` 保存 accepted/observed
Graph revision、`graph_change_summary` 与 observed graph；纯执行顺序调整不会改变 Ticket
Graph Revision。`status --json` 与 `history --json` 可审计这些事实。

该状态不允许 `run`、`resume`、`approve` 或
`revise` 越过边界，不自动 Requeue，不创建 Codex Thread，也不执行 Git/GitHub Publisher
mutation。MVP 不提供 `confirm-structure`；操作者只能恢复 GitHub 原图后重新核验，或执行
`abandon` 停止 Run。

没有任何可执行 Ticket 时，Run 进入 `progress_exhausted`，并在
`diagnostics[].remaining_tickets` 中列出每张剩余 Ticket 的原因。`terminal_kind` 进一步
区分 `waiting_human`、`temporarily_no_work`、`permanent_blocked`、
`unsupported_scope_change`、`execution_failed` 和 `all_tickets_completed`。最后一种对应
`run_acceptance_pending`，它只是 Issue #5 Run Acceptance 的交接边界。

## 权限边界

没有 App profile 时，Controller 在现有 allowlist 校验后直接使用宿主 `gh` 执行固定只读请求；它不调用
`gh auth login`、`gh auth refresh` 或 `gh auth token`。存在有效 App profile 时，Controller 使用专属
GitHub App 的 ID、installation ID 与私钥，按 worker 启动次数创建短期 installation token。创建请求只申请
`actions: read`、`checks: read`、
`contents: read`、`issues: read`、`metadata: read`、`pull_requests: read` 和
`statuses: read`，且只接受 GitHub 在同一响应中返回完全一致 permissions 的 token。这些只读
权限使独立验收可以通过 `gh pr checks` 读取 GitHub Actions 产生的远端 Checks 与 commit
statuses，确认 Hosted CI 结果。
不要复用 Publisher 的写 token。Controller 启动 Codex worker 时会移除 App 私钥、
Publisher GitHub token、SSH agent 和交互式凭据入口，并要求系统安装 `bubblewrap`。每个最长三小时的 Worker 通过仅在本次 invocation 存活的
普通 `gh` 命令入口按读取请求获取宿主身份或短期 token；Worker 不需要知道该入口背后的 adapter、socket、PATH 或挂载机制。Execution Guard
只检查 Worker PATH 与 Codex command-tool 的有效前置目录（`CODEX_INSTALL_DIR`，未设置时为 `~/.local/bin`），将其中当前存在且可执行的 `gh` 解析为去重的 canonical target，并由 bubblewrap
在 Worker mount namespace 内把同一个 invocation-local adapter 只读绑定到这些 target。新 adapter 不再 prepend 到 PATH，只清理继承环境中失效的旧 adapter 项；
bubblewrap 的原子挂载就是启动保障，不增加 sandbox launcher、`samefile`、私有 probe 或持久状态。它不扫描 PATH 外文件，也不拦截 shell alias/function
或 Agent 主动取得的替代客户端；任一绑定失败都在 Codex 启动前以 `worker_gh_binding_failed` 执行失败结束。Controller 位于该 namespace 外，
继续使用原始真实 `gh` 或专用 GitHub App；宿主 token、App 私钥和 Publisher 凭据都不进入 Worker 环境、持久 Run state、诊断或日志。adapter 只接受固定的 GitHub 读取
请求，拒绝外部 hostname、写入参数和携带请求体的 API 调用；单次 Controller 读取有界超时，
Worker 结束或凭据续签耗尽时会清理尚未结束的读取进程。adapter 在到期认证读取失败时仅
续签并重试该读取一次；续签使用十分钟有界退避，耗尽后以
`worker_credential_renewal_failed` 的可恢复诊断保留 checkout 与 Thread，供 `resume` 创建
新 Worker。Codex 使用 YOLO 模式，可以读写宿主文件系统、联网以及
使用真实 Git CLI 与上述受限的临时 `gh` adapter。Controller 只向 Brief 提供适用的 Parent/Ticket URL 和
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

普通测试使用 fake broker、fake `gh` 和本地 bubblewrap，不调用真实 Codex。真实 Codex command-tool
compatibility acceptance 只允许显式 opt-in，不进入普通 pytest、每轮 Development 或常规 CI。

Development Codex 使用 `skill:implement` 完成实现、自测和真实核心路径验证，并根据实际
改动风险选择 self-preflight、定向 Reviewer 或 `skill:code-review`。低风险局部改动不固定
支付完整开发侧预审成本；大型、跨模块或高风险改动仍应取得足够审查。Fresh Acceptance
不接收 Development Summary 或开发侧验证结论，在独立只读 Validation Checkout 中形成
E2E、Standards 和 Spec 三条独立验收 lane；`skill:code-review` 是 Standards/Spec 可使用的
推荐 SOP。需要写入的验证中间产物必须放在 checkout 外可定位、只服务本轮并在结束前清理
的临时路径。Prompt 不规定固定 subagent 数量、精确调用次数、调用顺序或嵌套层级。
Controller 不解析 Codex 内部事件流来审计 subagent 身份或 skill 调用；它信任上述
Prompt 合同，并确定性校验 Fresh 父 Reviewer 不复用 Development/旧 Reviewer Thread、
三个 lane 均有合法状态和证据，以及外层 SHA/Revision 绑定。

Acceptance Artifact 根对象只包含 `checks`，其中固定 `e2e`、`standards`、`spec` 三条
lane；每条 lane 只含 `status`、`evidence` 和 `findings`。任何 lane 的 Finding 都随完整
Artifact 原样回传同一 Development Thread，不再生成独立 Repair Brief；任一 `fail` 回到开发，
无 `fail` 但有 `blocked` 才等待人工，三条 lane 全部 `pass` 才接受。完整 schema、Finding
格式和各 lane 的最低证据要求见 [Acceptance Artifact Schema](acceptance-artifact-schema.md)。
base/head SHA、Effective Revision 和 Reviewer 身份由 Controller 写入外层 Acceptance Record。

Publisher 是唯一 Git/GitHub Mutation Authority，负责：

- 创建 GitHub 原生关联的 Ticket Branch；
- 创建 Candidate Commit 与保持 tree 不变的 Publication Commit；
- push、创建或更新唯一 Ticket PR；
- 检查 Required Checks 和准确 live head；
- 使用 `--squash --match-head-commit` 合并，并验证 integrated commit 的
  parent、tree 和标题；
- 显式关闭 Primary Ticket并记录 Run、PR 与 integrated commit。

每个 Ticket Review Budget Window 默认最多允许四次普通 Development Attempt 和三次 Reviewer
Invocation；具体上限由该窗口冻结的 `N+1/N` Policy Snapshot 决定。普通 Development 耗尽后，
只有已发布 Ticket PR 首次出现由准确 CI Evidence 证明可修复的 Required Checks 失败时，才额外允许一次
Final CI-fix。Development Attempt 在首次
分配时占用预算；进程失败、Output Repair、Human Blocker Resume 和 `--new-thread` 只继续同一
Semantic Agent Attempt，不重复计数。普通预算与 Final CI-fix 均耗尽且仍需修改时进入
`modification_budget_exhausted`，维护者显式 `resume` 开启新的编号 Budget Window。Reviewer
额度耗尽后的 Candidate 则继续按 Fallback Publication Receipt 和实际 Required Checks 规则处理。

## Source Runner 安装

v0.1 的公开入口是用户在所选源码目录执行一次 `./install.sh`。release tag 是稳定使用路径；
branch、fork、dirty source 和没有 Git metadata 的目录也按当前实际文件构建。安装器要求 CPython
3.11+、`venv`、`pip` 和源码声明的 Python build backend；它不执行 `sudo`、系统包管理器、
`pipx`、daemon、cron 或后台更新。

安装流程在用户级 `$XDG_DATA_HOME/agent-run/`（未设置时 `~/.local/share/agent-run/`）中创建
隔离的 non-editable Runner Snapshot，按 `agent_run` runtime tree 的规范路径和文件内容计算
SHA-256 identity，并写入有界 manifest 与 Runner Provenance。源码目录之后的修改不会改变已安装
Snapshot。manifest 只用于识别和解释，不是 source trust、签名或生命周期授权。

激活前由独立 `RunnerProbeBackend` 在空临时目录调用当前 PATH 中的 `codex exec --output-schema`。
它只接受根对象、禁止额外字段且 `status` 为 `ok` 的固定结果，不复用 Worker、GitHub、bubblewrap
或既有 publication handshake，不读取 Codex 版本，也不写长期 audit。Codex、构建或文件系统失败
会清理候选并保留原 Active/previous、Run locator、用户配置和仓库 `.agent-run`。

Active Runner 由完整 generation symlink 原子选择，generation 内的 `current` 与可选 `previous`
成对保存；系统只保留当前和紧邻上一个 Snapshot。相同 Active identity 的重复安装不会重新 probe
或创建重复 Snapshot。管理操作共享固定、非阻塞的用户级 `install.lock`，卸载不会删除该锁；旧
generation/Snapshot 的清理失败只写出有界 warning，并在后续安装重试。

```bash
./install.sh
./install.sh --rollback
./install.sh --uninstall
```

rollback 只交换 `current`/`previous`，不重建、不 probe、不检查或修改 Delivery Run；没有 previous
时失败且 Active 不变。uninstall 删除受管 Snapshot、generation、入口和 `~/.profile` 中唯一的
受管 PATH 块，保留固定锁、GitHub App profile、Run locator、私钥文件和目标仓库 `.agent-run`。
同名非受管 `~/.local/bin/agent-run` 永不覆盖；用户替换入口时保留用户内容并报告清理未完成。

安装完成后，稳定入口只是指向 Active Snapshot console entry 的 symlink；安装器不驻留、不启动
Manager/Launcher/daemon。安装、更新、rollback 和 uninstall 不迁移、修改或绑定 Delivery Run。
生命周期命令不再要求完整 SHA、clean detached checkout、`origin/main` ancestor、Codex 版本绑定
或长期 promotion audit；旧 promotion 命令不是公开入口。规范生命周期入口是安装后得到的 Active
Runner，直接从 source 或 editable checkout 运行生产生命周期不受支持；Active Runner 不会因
branch、fork、dirty source 或非官方 provenance 被旧 gate 拒绝。目标仓库的 Required Checks、Worker
读取权限、Publisher 写凭据和运行状态合同仍按本文件前文执行。

## 开源用户 Quickstart 与 doctor

源码仓库只负责构建 Runner，目标交付仓库负责保存 `.agent-run`、Delivery Run 和 Run Branch；两者
应当是两个目录。稳定使用先选择 release tag，开发者才选择 branch、fork 或 dirty source：

```bash
git clone https://github.com/GRD-Chang/grill-engineering.git
cd grill-engineer
git checkout <release-tag>
./install.sh

# 重新打开登录 shell 后，可在任意目录执行
agent-run doctor --json
cd /path/to/delivery-repository
agent-run doctor
agent-run run <parent-issue> --repo OWNER/REPO
```

`agent-run doctor` 是可选、只读的诊断入口，不要求当前目录是 Git 仓库。它报告 Python 版本、Git、Codex、
宿主 `gh` 登录、OpenSSL、Linux `bubblewrap`、Active Runner、PATH 和 Worker read provider；JSON
输出只包含 Python 版本、路径、状态、provider 和布尔值等非敏感信息。缺少依赖只报告问题，不安装软件、不
修复 PATH、不触发生命周期，也不修改 shell、auth profile、Run locator、Delivery Run、Thread、PR、
branch 或目标仓库 `.agent-run`。

后续更新仍从源码目录显式执行 `./install.sh`。如果当前为 A，安装 B 后保留 B/A，再安装 C 后只保留
C/B；相同内容重复安装不会重新 probe 或增加 Snapshot，候选失败会保留旧 Active。一次回退执行
`./install.sh --rollback`，它不重建、不调用 Codex、不检查或修改 Delivery Run；卸载执行
`./install.sh --uninstall`，它清理受管 Runner、入口和 PATH 块但保留固定 `install.lock`、App profile、
私钥、Run locator 和目标仓库 `.agent-run`。PATH 变化需要重新打开登录 shell；重复卸载安全，用户替换
的同名入口会被保留并报告清理未完成。

没有 App profile 时 Worker read provider 默认是 host `gh`；`agent-run auth status`、
`agent-run auth app configure --app-id <id> --installation-id <id> --private-key /secure/app.pem` 和
`agent-run auth app remove` 是可选的公开配置路径。status 不显示 token 或私钥，remove 不删除用户的
私钥文件；App profile 损坏时 fail closed，不回退到 host `gh`。Worker 读取继续受固定 allowlist 与
凭据隔离约束，Publisher 仍使用宿主写身份。

v0.1 只支持 Linux/WSL、用户级 `~/.profile` 和单用户安装。用户必须自行提供 CPython 3.11+、`venv`、
`pip`、Git、Codex、OpenSSL、已登录的 `gh`、Linux `bubblewrap` 与目标仓库所需权限；Windows、macOS、
系统级/多用户安装、PyPI/pipx、常驻 Manager/Launcher、自动更新和跨平台支持不属于本版本。

## 本地状态与清理

耐久状态位于 `.agent-run/runs/`。本协议要求 `semantic_attempt_protocol: 1`；旧 Run 不迁移、
不兼容读取，也不会在拒绝前执行 lifecycle mutation，必须重新创建或明确清理。稳定 Development Checkout 位于
`.agent-run/worktrees/`：Required Checks pending 或 Worker/Publisher 普通失败、超时、
进程异常或 Ctrl-C 时保留，以恢复未提交成果；Development/Repair Codex 报告 Human Blocker 时也保留，供同一 Thread 在 `resume` 后重新核验并继续。普通 cleanup 只有在 Git 元数据证明 checkout 属于本 Run 且 `git status --porcelain` 为空时才删除。tracked/untracked 修改、元数据缺失或归属不一致都 fail closed，并在 `status`、`run`、`resume` JSON 中给出路径、原因和恢复命令。
`abandon` 在任何 GitHub mutation 之前执行同样的全 Run preflight；若维护者确认不再需要本地成果，必须显式使用 `abandon --discard-worktree`。Ticket 完成和明确 abandonment 才进入清理。
Checkout 尚未准备完成时产生的部分目录也会清理。每轮独立 Validation Checkout 在验收
结束后完整删除，允许验收期间创建构建、测试和诊断中间产物；Reviewer 只清理自身产物，不修改
交付内容。Codex 的临时 schema、输出
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
`run` 会从耐久状态与 GitHub live 事实对账恢复；已完成 Ticket 的 PR、merge、评论和关闭
动作不会重复。没有可执行 Ticket 时，顶层 diagnostics 会汇总全部剩余 Job 与 GitHub blocker。
