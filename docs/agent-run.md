# agent-run 使用说明

`agent-run` 是显式启动的本地 Delivery Run 控制器。当前实现支持：

- 从 Parent Issue 启动或恢复 Delivery Run；
- 按 GitHub 原生依赖图选择可领取 Ticket；Agent 工作共享配置的并发上限和持久 FIFO，集成交付保持单席，CI 等待不占 Agent 名额；
- 让持久 Development Thread 实现和修复，并由独立、只读 Publication Codex 生成发布语义；
- 为每轮首次候选验收创建全新的 Fresh Acceptance Thread 和一次性 Validation Checkout；同一 Reviewer 因 `execution_failed` 或 Human Blocker 继续当前对象时复用原 Thread 并重新准备 checkout；
- 以 [Acceptance Artifact Schema](acceptance-artifact-schema.md) 约束 Ticket 与 Run Reviewer 共用的三条验收 lane 输出；
- `run` 与 `resume` 都通过同一 Task Control 和独立 Executor 连续推进；恢复失败 Invocation 或 Human Blocker 后形成的 Candidate、PR 与 Required Checks 由同一 Executor 监督到下一真实边界，后台模式下发起终端退出或 Ctrl-C 只离开观察；前台候选的区别见下节；
- Required Checks 的 pending 等待默认2700秒（45分钟），可通过 `--required-checks-wait-seconds 2h` 或同名个人 Policy 字段配置并在新 Run 冻结（详见 [个人默认](user-defaults.md#required-checks-等待时长)）；到期进入可恢复的监督超时暂停；其他 GitHub 异步状态、未知读取或对账按各自有界窗口监督。远端检查在超时后成功不会自动重启或续期；按 `status` 指引显式 `resume` 开始对应的新窗口，不另启 watcher，不以重复 `run` 清除超时或其他人工门禁；
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

## 运行模式与前台候选验收

默认后台模式仍由 systemd user manager 托管；关闭终端或 Ctrl-C 只离开命令观察，
停止工作使用 `stop`。跨注销持续能力仍取决于宿主配置，不承诺跨重启自动恢复。

本候选已接入显式 `--foreground`，例如 `agent-run run PARENT --repo OWNER/REPO --foreground`；
后续需新建前台执行时显式使用 `resume RUN_ID --repo OWNER/REPO --foreground`。
`approve`、`requeue`、`revise`、`abandon` 也接受该选项；`stop` 不接受它。
活动 Host 按已记录 backend 核验，不静默从后台回退前台，也不以新选项替换活动 Host。
前台共用原 Core、Executor、Git 只读、身份和批准链路，不需要 systemd user manager，
但仍须满足 bwrap、准确进程身份/退出观察及原有工具和认证前置。

**前台仍待验收，不是已发布可用能力。** 正常取消的验收合同是请求停止并等待收尾，
而非仅退出观察；不保证关闭终端、注销或会话结束后继续运行。
最终批准操作及恢复该操作的执行仍不可取消；未知外部写入必须沿原 intent 读回，不能重放。
异常或强制退出后须先确认原 Host/Worker 准确退出，再显式恢复；保留工作、Attempt 和预算。
基底 `6a0a50f4` 的 normal（29.394秒）与 stop-resume（30.920秒）已在明确批准的 savedcloud
执行上下文合成通过：2P/0F/0skip，非 editable 安装121文件一致；使用假 Codex/gh 与真实 Git。
原默认权限2F（uidmap 等）独立保留。后继 `854f0764` 的启动失败/中断 drain 退出留证修复已独立准入：
before/after hello 真实 namespace 合成2P、unknown 负例1P；这些不同提交的定向结果不等于最终组合全量通过。
真实 Codex 与父助手原云交付仍未验收。
不能把合成通过、上述合同或 doctor 的能力预检作为完整交付证明。环境实测边界统一见[支持矩阵](productization/support.md)。

本候选仅接受 repository v23；v22及更旧Run、早期未激活仓库候选保留原数据但拒绝续接，无迁移，见[状态存储](agents/state-storage.md)。

## 一条命令推进 Ticket 交付

先按[安装与接入](install.md)准备已激活的 Runner、后台所需的 user systemd 254+、
bubblewrap namespace、修复版 SQLite、Codex、所选 GitHub 身份及目标项目的构建/测试环境。
安装完成或 `doctor` 返回 ready 不能证明目标 Ticket 已通过真实交付验收。

Parent Issue 的 title/body 是整体需求；有 Ticket 时使用 GitHub 原生 sub-issues 与
`blockedBy` 关系，不从正文清单推断依赖。可领取 Issue 必须 open、有 `ready-for-agent`，
没有 `needs-triage`、`needs-info`、`ready-for-human`，且 blocker 已解除。
无子任务时走 Parent-only，同样需要整体 Review、准确检查和最终人工批准。

```bash
agent-run run <parent-issue> --repo OWNER/REPO
```

| 阶段 | 自动推进的工作 | 需要核对的结果 |
| --- | --- | --- |
| Ticket 开发与审查 | 领取可执行 Ticket，开发、自测、独立审核及有界修复；Ticket 普通审核额度末轮存在既定 fallback，不能把 fallback 称为审核通过 | 当前 Ticket 合同、真实开发产物、审核 Findings 与额度 |
| Ticket 集成与 CI | 程序核验本地集成、发布 Ticket PR；监督准确 PR/head 的必需检查，真实失败交回 Development；满足当前授权和检查后 squash 合入 Run Branch、关闭 Ticket | 准确提交、PR、检查观测、merge 与关闭事实；集成失败保留现场进入定向开发 |
| 完整 Parent 验收 | 全部 Ticket 完成后集成整体成果，对照完整 Parent 做独立 Review；Findings 和最终 CI 修复沿原 Run 工作循环推进 | E2E、Standards、Spec 三条 lane、准确候选与预期合并结果；需要更新 Run 时经修复 PR 检查并自动普通合入 |
| 最终发布 | 生成最终 PR 文案，发布到默认分支的 PR，监督适用检查 | `pending` / `unknown` 不等于通过；明确未配置必需检查也不等于 CI 通过 |
| 人工批准与完成 | 停在最终批准门禁；明确批准后重新核验当前事实，程序合并、关闭 Parent、完成必要清理 | 最终 PR/head/base 与验收证据适用，真实 merge/close/cleanup 已确认 |

`run` 提交或附着任务后由独立 Host/Executor 推进，CLI 返回只表示操作回执。
它不会替维护者作出最终批准。运行中需要权限、产品决定、缺失材料、预算续期或环境修复时，
按查询给出的边界处理；不保证任意 Ticket 都能无人工介入完成。

## 命令

日常启动使用 `run`，创建或附着同一 Parent Issue 的未完成 Run，在当前门禁允许时自动推进。
暂停恢复使用状态提示的专用动作：

```bash
agent-run run <parent-issue> --repo OWNER/REPO
agent-run status --parent <parent-issue> --repo OWNER/REPO
agent-run history --parent <parent-issue> --repo OWNER/REPO
agent-run runs --repo OWNER/REPO
```

当前仓库只有一个进行中 Run 时，`status` 与 `history` 可以省略 Run 选择参数；在任意目录使用
`--repo OWNER/REPO` 查询时须同时提供 `--parent <parent-issue>` 或完整 Run ID。多个候选时命令会列出工作目录、
Parent、状态和开始时间并停止，不按最近时间猜测。

### 语言与只读查询

```bash
agent-run settings configure --language en
agent-run --help
agent-run run <parent-issue> --repo OWNER/REPO
agent-run status <run-id> --plain
agent-run history <run-id> --details --plain
agent-run settings configure --language zh
agent-run status <run-id>
```

个人语言默认中文，只接受 `zh` 和 `en`，不跟随系统 locale。帮助和无 Run 的配置操作
使用个人语言。上例创建的 Run 固定为英文，切换回中文后，其状态、历史与适用操作回执
仍使用英文；后续新 Run 使用中文。

交互终端默认使用 Rich 排版，`--plain`、管道、`NO_COLOR` 或 `TERM=dumb` 使用朴素输出。
两种排版使用同一语言；`history --details` 展开逐轮执行配置、问题及原始证据。
角色、阶段、状态、人工待办、下一步、计数和时间等程序标签随 Run 语言展示；
Issue 标题、日志、用户反馈、Agent 摘要、Findings 和证据原样保留，可以混合语言。
`--json` 的机器字段、枚举、ID 与审计事实保持稳定，命令文本不翻译。

查询不会改变任务、历史或通知记录，也不会发送飞书。操作回执区分请求受理、正在应用、
已应用和失败；请求受理不表示开发、验收或交付成功。恢复后以实际状态和下一步为准：

```bash
agent-run resume <run-id>
agent-run status <run-id> --plain
agent-run history <run-id> --details
```

### Delivery Policy

Delivery Policy 的取值优先级是内置默认值、用户级默认值、单次命令覆盖；仓库内容不能覆盖
操作者的个人成本策略。统一个人文件为 `$XDG_CONFIG_HOME/agent-run/user-defaults.json`
（未设置时为 `~/.config/agent-run/user-defaults.json`）。完整字段、直接编辑、模型与推理强度、
引用关系及错误处理见[个人运行默认配置](user-defaults.md)。个人设置统一使用 `settings show/configure`：

```bash
agent-run settings show
agent-run settings configure --ticket-review-rounds 3 --parent-only-paired-rounds 10 --run-repair-rounds 3 --parent-ci-development-limit 3 --review-deadline 2h
agent-run run <parent-issue> --ticket-review-rounds 1 --development-deadline 30m
```

Ticket 的语义 Review 轮数为 `N` 时，普通 Development 为 `N+1` 次、Reviewer 为 `N` 次；默认
为 `4/3`，最后一次普通 Development 不再进入 Review。可修复 CI 失败使用独立额度。
Parent-only 的配对轮数为 `N` 时，Development 与 Reviewer 都最多执行 `N` 次并严格配对；默认
为 `10/10`，Reviewer 失败不会转入 Ticket Fallback，而是在当前窗口耗尽后等待显式 Resume。
Run Repair 的修复轮数为 `N` 时，首次整体 Reviewer 加最多 `N` 次 Development 和 `N+1` 次
Reviewer；普通 Parent 集成窗口默认为 `D3/R4`。第 `N+1` 次 Reviewer 仍有 Finding 时进入 Review Budget
Checkpoint，不使用未经 Reviewer 验收的 Publication Authority。
正整数轮数和正 duration 在创建 Worker、PR 或部分状态之前校验。每个新 Run 以及显式开启的
新 Budget Window 都把实际生效的完整策略保存为 Policy Snapshot；之后修改用户级默认值不会
改变已有 Run，包括之后获准开启的新预算窗口；只有检查点 Resume 的显式覆盖可修改新窗口策略。缺少或不完整 Snapshot 的旧状态会 fail closed。
Invocation 默认 deadline 为 Development 5 小时、Review 2 小时、Publication 1 小时；同一
Invocation 内的初始调用和 Output Repair 共用该 deadline。

在人工边界使用对应的公开命令：

```bash
agent-run resume <parent-issue> [--new-thread] [--message "..."] --repo OWNER/REPO
agent-run requeue <parent-issue> --repo OWNER/REPO
agent-run approve <parent-issue> --repo OWNER/REPO
agent-run revise <parent-issue> --message '未经改写的维护者反馈' --repo OWNER/REPO
agent-run stop <parent-issue> --repo OWNER/REPO
agent-run stop <parent-issue> --ticket <ticket-number> --repo OWNER/REPO
agent-run abandon <parent-issue> [--discard-worktree] --repo OWNER/REPO
```

### GitHub 身份

Worker 的 host/PAT/App 选择、凭据引用与 App 收窄选项通过 [个人运行配置](user-defaults.md) 管理。无配置默认使用宿主已登录的 `gh`；所有来源使用原生 `gh`，访问能力由所选凭据决定，不通过本地命令白名单限制。Runner 程序端独立选择 host/PAT/App，默认 host，复用同一 PAT 引用和 App 定义。

程序端 PAT/App 在首次克隆前校验；克隆、Controller 查询、Publisher 写入和读回、Git fetch/push 与远端分支清理均使用该选择。远端 Git 使用任务级 HTTPS 凭据，不修改全局登录或 Git 配置。`--runner-github-source` 仅覆盖新 Run；已有 Run 固定来源与引用，修复原引用后通过 `resume` 恢复。建立 Run 前失败只报告启动错误；已有 Run 保留进度与执行失败诊断。结果未知的写入继续按已有意图、准确 head 和发布者身份读回核验，不因凭据恢复而绕过批准或盲目重放写入。App 使用其 bot 操作身份核验评论、事件及发布结果，不要求 installation token 通过个人用户身份接口，也不删除作者校验；外部操作不能仅因内容相似被认领为本次正式发布。

Parent-only 与 Final Run 收尾前保存绑定 Run、PR、合并 SHA 和发布者的关闭意图及派发状态。评论与关闭请求分别在发送前保存派发状态；评论结果未知且暂不可见时只等待读回，不重复发送。恢复时同时核对完成评论与意图之后的关闭事件；只有本地已记录关闭派发且匹配发布者的关闭事件可完成收尾，同一 App 的其他调用方关闭也不能替代本地派发证据。其他作者关闭或后续重新打开属于冲突；事件尚不可见时保持待对账，不重发已派发的关闭操作。缺少可核验关闭意图的旧完成评论不能单独证明本次发布已关闭 Parent。

App 元数据与其他个人设置共同保存在 `user-defaults.json`；私钥只引用文件，由程序端签发短期 token，不主动下发私钥。App 默认采用安装已授权的权限和仓库范围；只读和当前仓库是仅作用于 Worker 的独立选项。Runner 使用完整安装授权，同一 App 定义下的两端 token 按调用上下文和范围隔离。示例：

```bash
agent-run settings configure --worker-github-source app --github-app-id 123 --github-installation-id 456 --github-private-key-path /secure/agent-run-app.pem
agent-run settings configure --runner-github-source app
agent-run settings configure --github-app-read-only
agent-run settings configure --migrate-github-app
agent-run settings show
```

旧独立 App 配置需要显式迁入统一配置，保持 App 来源并采用新默认范围；查询不隐式迁移。迁移保留其他设置，显式新配置优先，失败保留有效数据，不删除活动执行器引用的旧材料。缺少新身份与范围绑定的旧 Run 明确拒绝恢复并保留数据，不从个人默认猜测补齐。配置或认证失败不回退宿主身份。

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

Required Check 真实失败绑定当前 PR/head 的 Observation 与失败明细后，交给 Development 调查。
Actions job 的准确 head、完成状态与原始日志提供证据，不再读取仓库的 `code-failure-steps` 白名单。
平台、取消或未知类别的真实失败也可调查；pending、unknown、身份不匹配和证据尚未收敛继续有界监督。
两种 Parent 必须取得当前整体 Review，成果改变重新验证 CI 与人工批准。

`status` 和 `history` 默认输出便于人阅读的摘要；加入 `--json` 可获得当前 native 机器可读输出。处于
外部等待或监督超时时，两种格式均显示等待种类、对象、head/base、窗口开始与截止、剩余时间、
重试次数、脱敏的最新观测，以及超时后的唯一恢复操作。Lifecycle Action 已应用且 Executor 完成
握手后，发起 CLI 可以返回；后续轮询与自动推进由独立 Run Executor Session 承担，不依赖原终端
或发起命令继续存活。Executor 在窗口到期时保存 `supervision_timeout`，再由明确的恢复操作开启新窗口。
Semantic Agent Attempt 在预算门禁通过后、首个 Codex 进程启动前写入状态；Development 与 Reviewer
Attempt 绑定当时的 Review Budget Window，Publication Attempt 则不绑定该窗口。一次 Attempt 可包含
初始 Invocation、同 Thread 的 Output Repair、进程失败后的 successor Invocation、Human Blocker Resume
以及显式 `--new-thread`；这些恢复都沿用同一 Attempt ID 和领域计数，不能重新获得预算。
Publication Invocation 在首个 Codex 进程启动前写入状态；`thread.started` 会在进程仍运行时
立即保存。`status --json` 的 `subjects[].pending_attempt` 和 `history --json` 的
`history.attempts[]` 使用同一 native Attempt 结构：`result` 保存原结果，`invocations[]` 保留
准确调用、Thread/Profile 绑定与恢复关联，`outputs[]` 保留各次物理输出的原始返回、dispatch/exit
事实与活动计时。当前调用始终由 Attempt 的 `current_invocation` 定位，不能以最新记录替代。
领域预算位于 `subjects[].budgets`，实际发布失败位于 `history.effect_failures`，操作派发计数位于
`history.effects`；这些不同层级不能混成一个计数。当前焦点由 `focus_subject` 引用，席位、
并发上限和两条 FIFO 位于 `scheduling`，不再复制 Worker、Ticket usage 或 progress 镜像。

每次公共 `resume` 另存独立授权事件。`latest_resume` 显示当前最近的原 Action；
`history.actions[]` 保留所选历史页关联的原 Action、payload 与 receipt，其 `resume` 字段记录
是否已应用、successor Invocation、启动与准确目标进展，不能把已应用解释为目标已启动。
不可变 Human Response 按 Resume identity 保留维护者消息，`history.events[].evidence` 保留完整响应，
普通文本只显示确定性截断的摘要。历史事件只有这一条流，按原 `position` 排序；
`history.after/next_after/has_more` 表示分页范围，`history.rounds` 统计本页 Attempt 闭包的领域轮次。
当前概览与历史页在同一只读事务中读取，不将当前状态补作历史结论。
控制审计不另存 Prompt、transcript 或 Acceptance Artifact；原工作结果通过准确 Attempt 关联读取。
Ticket、Parent-only 和 Run Repair
的 Development 可返回 `{"result_kind":"incomplete","summary":"尚未完成的具体工作","human_blockers":null}`。
这不是格式错误或人工阻塞；准确最终返回、原 Thread 与进程退出可证明时，同一 Attempt 最多自动继续一次，
原绝对 Invocation 截止覆盖新准备和后续自动异常恢复，不增加 Development 轮次。到期、缺证或已用尽时明确暂停，
需要显式 `resume`；原材料、预算、Findings 和当前代 Human Response 保留，不能据回复文本跳过独立 Review。
完成但无代码变化时不制造空提交；需要独立 Review 而名额不足时进入原 Review Budget Checkpoint。
Ticket、Parent-only 和 Run Repair
的 Development、Fresh Acceptance 与 Publication 都使用同一 Invocation seam：非法结构化输出会在
同一 Thread、只读 checkout 中使用角色化短格式 Prompt 最多修复两次，且不增加领域 attempt。进程异常只在
ADR 0011 的边界内自动同 Thread 续接：普通异常最多一次，准确容量不足可持续恢复；续接使用当前角色的
短 Prompt，不自动创建替代 Thread。已有 Development 角色接收新的定向 Repair 时使用紧凑修复任务 Prompt，
保留当前边界、原始 Repair Evidence、完成条件与 Git 约束，不重复 Initial Development 专属说明。
`resume` 默认复用已保存 Thread，并使用当前 Development、Repair、Reviewer 或 Publication 角色的更短
Prompt，只补充继续同一次语义工作仍然需要的动态证据；`--new-thread` 只替换
Invocation/Thread，不替换 Semantic Attempt，并使用当前角色和任务模式的完整标准 Prompt 新开 Thread。
模型 Prompt 不说明 Thread、Resume、`execution_failed`、预算窗口或后继流程；Reviewer 与定向 Repair
只接收从现有预算投影的简短审查次数。`--message` 可补充任意当前恢复工作的人工处理结果；它 trim
后必须非空、最多 8 KiB，以不可变 Human Response 绑定当前 Job Generation，并进入后续 Development、
Review 和 Publish 的权威上下文。当前 Generation 的响应按顺序保存、不按容量截断；替换
Generation 从空响应序列开始，绝不向新 Generation 注入旧响应。它不修改 Issue、不触发 Requeue、
也不等同于 `revise` 的 Run Feedback。
`resume` 继续原对象的未完成工作，不以暂停 reason 白名单决定是否永久拒绝。程序复核原需求与任务图，
再按暂停前阶段、准确 pending Attempt 或已有 Candidate/Acceptance 进入对应后继；已完成角色成果由确定操作消费，
不机械重新调用 Agent。Ticket、Parent-only、Run Repair、Run Acceptance 与 Final Publication 共用该准入。
Run 级 Executor 恢复的消息按现有工作 FIFO、集成队列与名额选中的工作绑定，并进入该对象的实际 Agent 请求；
其他 Ticket 的局部暂停不接收这条消息。回复局部暂停的 Ticket 时使用 `--ticket` 指定对象。
缺少阶段或必要成果时，诊断列出需要补足/核验的事实，保留现场与同一入口；未知外部结果仍须先对账。
`supervision_timeout` 为同一等待身份开启新窗口；message 保存在该工作上下文中，后续需要 Agent 时传递，
纯等待恢复不创建 Worker。`--new-thread` 仅适用于已有 Agent 工作身份。Ticket 与 Parent-only Change Job 的
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
| `run` | 新 Task 或该 Task 已发布的未完成 Run | 提交或附着普通 Lifecycle Action；准确 Host 附着或交接后返回操作回执，由独立 Run Executor Session 监督 checks 与 GitHub 读取/对账，在当前门禁允许的范围内推进 | 不隐式恢复失败 Invocation、清除 Stop/超时或替代 Requeue、最终批准 |
| `resume` | 受支持 Run 的唯一暂停对象、准确局部 Ticket 或 Run 级停止边界 | 核验原工作与恢复条件；需要继续 Agent 时在原 Semantic Attempt 中接续，已有返回先消费，纯等待不创建 Worker | 保留适用 attempt 与额度；显式继续只续期对应耗尽窗口并留存历史，不替代最终批准或清除其他对象门禁 |
| `requeue` | `requeue_required`，或查询明确要求 Requeue 的最终需求刷新边界 | 按当前权威事实替换 Ticket/Parent-only Generation；完整 Parent 刷新沿其自身验收路径处理 | 不 rebase、不迁移旧 Candidate/Acceptance/Human Response/Thread/worktree |
| `status` / `history` | 任意已知 Run | 分层查看 Attempt、Invocation、Output Attempt、Budget Window、Publication Operation Retry 与下一步 | 不改变状态或恢复工作 |
| `approve` | `run_approval_pending` 或 Parent-only 的 `parent_approval_pending` | 重新核验当前事实后，授权 Publisher 合并最终 PR；两种 Parent 发现允许接纳的远端追加时先提示并自动更新、复验，再等待新批准 | 不跳过 Fresh/Run Acceptance、Required Checks 或 Published-Head Gate；更新启动不表示交付完成 |
| `revise` | 完整 Parent 的最终批准阶段，且当前证据与门禁允许修订 | 原样保存 Run 级维护者反馈，并进入 Run Repair | 不是 Human Blocker 的响应通道，也不用于 Parent-only |
| `stop` | 当前控制边界允许停止的未完成 Run，最终批准段除外 | 记录 Run 停止；有活动 Executor 时撤销旧执行代次并收口准确 Worker | 不把 Stop 记为失败，保留 checkout、Thread、Attempt 或 Artifact；按准确控制与退出事实收口 |
| `resume --ticket` | 指定 Ticket 位于合法局部暂停；可与其他 Ticket 的执行并存 | 复核需求、暂停授权和原 Host；有活动 Host 时交给原执行器，无活动 Host 时须先核验退出再准入交接 | 返回成功只表示已接受；不创建重复 Executor、不插队、不重置有效预算，最终批准和全局门禁仍有效 |
| `stop --ticket` | 未终止 Run 中尚未完成的指定 Ticket，最终批准期间除外 | 持久记录停止意图，立即中断准确归属该票的 Agent，取消未启动的工作排队；确认进程退出后释放名额，其他 Ticket 与 CI 监督继续 | 保留工作区、Attempt、适用 Thread 与独立预算门禁；在途写入先对账，状态展示等待收口原因；用 `resume --ticket` 显式继续 |
| `abandon` | 未完成 Run（包括人工边界） | 先检查所有 Managed Development Checkout；干净时写入 durable abandonment，再执行受限的 PR 关闭、Ticket reopen 与本地清理恢复 | 不回滚默认分支；dirty checkout 默认不删且不执行 GitHub mutation，只有显式 `--discard-worktree` 才强制丢弃 |

控制 Action、Host、Executor 与业务事实同在该仓库的唯一v23数据库中，不能依靠删除一个控制文件恢复准入。
生命周期命令先核验准确 Task 绑定、原 Host 和 Runner 使用租约，再决定附着或交接。
附着回执不表示本次新请求已应用；须核对 requested action 与实际应用结果。
Run 已到人工边界、PID 消失或原命令参数相同，都不能代替准确 Host/Worker 的退出证明。
未知启动、退出或写入结果先对账，不通过重复启动或编辑数据库猜测恢复。

Host 异常时先撤销原执行代次的准入，再排空原 Worker；这期间不能用新的或重复的 `resume` 准入替代 Host。
若撤销无法提交，原 Task 锁保留至该 Host 实际退出，原始返回/退出仍可按原归属结算，不能把提交失败写成已撤销。
创建 guardian 的进程在事务外等待并收割原 Popen；原启动 deadline 不变，额外3秒仅限退出回收宽限。
其他进程以准确身份/pidfd 观察原 guardian 退出，仍须同时满足 Host、cgroup 和 FD-store 证明；
不是看到僵尸、PID变化或 Future完成就释放租约或重新启动。

### Output Repair、Resume 与 Requeue

三者按失败层级分开，不可替换：

- **Output Repair**：同一个 Invocation 的 Codex 进程零退出、Thread 身份正确，但最终结构化输出不符合完整阶段 contract 时自动执行。它最多追加两次同 Thread、只读、按 Development/Reviewer/Publication 角色区分的短格式修复请求；只要求重发合法输出，不重新执行语义工作，不创建新的 Invocation，也不增加领域 attempt。
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

Human Blocker 与失败共用 Resume UX；`--message` 可传递原始回答或本次恢复所需的处理事实：

```bash
agent-run resume <parent-issue> \
  --message '已授权使用内部测试仓库；继续当前验收。' \
  --repo OWNER/REPO
```

`--message` 会 trim 校验为非空、限制为 8 KiB，并按顺序绑定当前 Job Generation；它会进入后续
Development、Review 与 Publish 的对应工作上下文。它不修改 Issue、不触发 Requeue，也不能替代
`revise --message` 的 Run 级反馈。若 Resume preflight 发现 stale，命令不会运行 Codex，而是返回
`requeue_required`；此时只能先查看状态/历史，再由维护者显式 Requeue：

```bash
agent-run status --repo OWNER/REPO --parent <parent-issue>
agent-run requeue <parent-issue> --repo OWNER/REPO
```

`run` 创建或附着 Run，由准确的 Run Executor 按依赖、并发名额和 FIFO 推进完整 DAG。每张 Ticket 完成后都会重新读取 GitHub 权威状态，
重新计算 frontier；某条分支等待人工时，不依赖它的其他可执行 Ticket 仍会继续。
Required Checks 仍为 pending 时，独立 Run Executor Session 在有限窗口内监督；窗口到期后保存
`supervision_timeout`，状态提示的恢复操作是 `resume`。普通 `run` 附着不会清除该门禁；
显式恢复只续期对应等待，不重复创建 PR，也不消耗修改预算。
Required Check 失败时，Controller 读取失败 check 的名称、workflow、描述和链接，并读取其
Actions job 的当前 head、状态及可取得的原始日志。准确失败不要求先被程序分类为代码可修：
Ticket 和两种 Parent 共用开发循环，由 Development 定向调查失败与直接影响；Parent 的 Reviewer
基于适用完整基线增量核验并对当前整体负责。pending、unknown 和证据尚未收敛继续有界监督。
同一 Parent 的修复工作、最终 PR、后继提交和目标追加共用累计 CI 开发额度，默认 3 次；
耗尽后显式 resume 仅续期该额度并保留历史，修复后的当前成果须重新通过准确 CI 和人工批准。

新Run与Task/current绑定同事务保存在该仓库唯一SQLite中，不再发布JSON位置记录。
业务身份只从准确Run的数据库关系核验。`runs` 可以按当前仓库或显式 `--repo` 发现候选；
`status`、`history` 可以按当前仓库唯一current Run、`--parent`，或任意目录的
`--repo + --parent` 选择。同一用户的不同启动目录和用户 clone 共用同一 Repository/Parent 的未完成任务。
候选不唯一或身份冲突时停止，不按最近时间猜测，不全盘搜索，不补写状态。
准确定位与有界发现规则见[Run 选择与定位](agents/run-selection.md)。
普通 mutation 与 Run-scoped `configure` 同样使用 Parent
位置参数并执行零匹配、多匹配和 repository mismatch 检查；完整 Run ID 用于自动化和精确排障。
查询和变更命令都可从任意目录通过 `--repo OWNER/REPO` 定位目标；省略时只读识别当前用户仓库的远端。
变更命令始终使用该仓库的统一 state 目录，`--state-dir` 指向其他位置时明确拒绝；`status`、`history` 可用该参数只读排障。数据主目录通过 `XDG_DATA_HOME` 选择。

Lifecycle mutation 默认输出面向维护者的回执，只显示仓库、Parent、操作、是否附着原操作、动作是否
已应用、当前交付状态与下一步；动作应用完成不代表整个交付已经完成。Action ID、Run ID、Executor
generation、payload digest 等稳定机器审计事实只在显式 `--json` 输出中提供。

如果 merge 的写入响应出现网络错误或无法解析的响应，Publisher 不盲目重放：先在 GitHub 对账。PR 已
合并即恢复成功；PR 仍 OPEN 且 live head/base、Required Checks 与 mergeability 均保持当前时，最多重试
同一个带精确 head 绑定的 merge intent 三次；任一状态矛盾则停止等待人工处理。

PR 原 `base` 是远端观测快照，可能落后于同一 target ref 的当前提交；程序保留原快照，
另外核对准确仓库、PR、source/head、target ref 与祖先关系，并将实时 target 绑定到审核、intent及批准。
当前 ref 分叉、缺证或身份不符仍拒绝。CLOSED PR 还须证明本代次准确的原 `retire_pr` receipt，
不能仅凭关闭状态或祖先 base 接管外部操作；MERGED 的结果仍按原合并证据核验。
这些读取和写前复核不是 GitHub 服务端原子 base-CAS 承诺，不能据此跳过检查或最终批准。

当所有 Ticket 完成后，`run` 自动进入 `run_acceptance_pending` 并推进 Run Acceptance。
正常 Run Acceptance Attempt 在一次性、只读的 Validation Checkout 中派发全新的 Run Reviewer；Reviewer 不得
复用任意 Ticket 的 Development/Reviewer Thread。它从 Parent Issue 与 GitHub 独立读取
最终 Ticket 集合和依赖，
检查准备好的累计 diff，并形成实际 E2E、Standards、Spec 三条独立验证 lane。Reviewer 必须调用
`skill:code-review`，并对三个维度的最终判断负责；Prompt 不额外要求每个维度对应一个 subagent，
所有审查或评价型 subagent 使用 `fork_turns: "none"` 和中立任务事实。Run Reviewer 不得修改 Validation Checkout；
需要写入的构建、测试与验证中间产物必须放在 checkout 外可定位、仅服务本轮且结束前清理的临时路径。
Reviewer 不得修复源码、测试、配置或 `.gitignore`。Ticket Completion
Revision 按 Ticket number 数值排序，且只绑定已集成 SHA、冻结 Effective Revision 与已验收
base/tree；已关闭 Ticket 后续 title/body 编辑不改变该版本，普通 reopen 则 fail closed。
Completion Record、
Expected Merge Result 与 SHA/Revision 绑定只由 Controller 在验收外层校验，不进入 Codex
Prompt。失败 findings 原样交给持久 Run Repair Development Thread；每次真实
代码修改先形成不可变 repair Candidate，由全新的 Candidate Run Acceptance Reviewer 按完整 Parent 范围
检查准确 default head 与 Candidate 的预期合并结果。Candidate 通过后由程序核验准确源与默认分支必要祖先关系，
发布修复分支和以 Run Branch 为 base 的 Run Repair PR，读取该 PR 目标分支的 Required Checks。
检查通过或明确未配置时，由程序自动普通合入 Run，不增加人工批准点。真实 CI 失败回到同一 Parent 修复工作，
尚未合入的 PR 原地更新；已合入记录保留，后续新修复使用新 PR，不重置累计额度。Controller 只在 Candidate tree、repair base、实际 Run tree、
default head、Parent/Graph revision 和 Ticket Completion records 全部精确匹配时提升该结论。若仅 default head
前进，Controller 在原 Repair Cycle 重新预演并验收最新组合；其余权威边界失配才废弃 Candidate，并进入新的
Run Acceptance Generation。
同一 Run Reviewer 因 `execution_failed` 或 Human Blocker 继续当前对象时，Controller 复用对应
Reviewer Thread，但仍创建新的 Validation Checkout，并以角色化短 Prompt 提供准确验收对象、适用的
当前失败或 blocker 证据以及维护者最新回复。
无代码变化不消耗预算，配置的 `N` 次仍不能通过或确实需要人工决定时才进入 `ready_for_human`。
通过只进入 `run_publication_pending`，尚未完成最终 PR 发布或默认分支合入。`run` 随后推进
正常 Run Publication Attempt：由新的、只读的 Run Publication Codex 根据
Parent Issue、累计 diff 与 Fresh Run Acceptance 生成最终 PR 叙事；同一 Publication Agent 因
`execution_failed` 或 Human Blocker 继续当前对象时复用对应 Run Publication Thread，并只补充当前必要
发布证据、blocker 与维护者最新回复。Publisher 维护同一个
Run Branch → 默认分支的最终 PR，并渲染 Parent Issue、Delivery Type 及每张已完成 Ticket 的链接；它将
Parent/Graph revision、Run/default/PR head 与预期 merge tree 写入独立 Publication Record。
Publication Agent 返回合法叙事时 Semantic Attempt 即完成；之后 GitHub 写入、读取与对账失败只增加
独立且有界的 Publication Operation Retry。该重试耗尽后进入硬 `publication_pending` 边界，`resume`
显式继续只开启该耗尽操作的新有界窗口，保留旧窗口历史、原成果、发布文案及未解决的写入意图，
不重置开发、集成或 CI 额度，不替代最终批准。重启先核实准确 Git/远端结果：成功写入不重复，
原边界仍有效且操作未发生时才继续；无法核实的结果保持有界等待和具体缺口。
Ticket 的未知推送/PR 结果在暂停期间仍保留集成席位，其他 Ticket 可以继续开发，
只能在对账确定后接纳新的集成交付。Publication Commit 在 Git 对象写入前保存准确对象意图，
恢复时直接核验对象并安装原提交，避免重复生成提交。详见 [正式写入对账合同](adr/0022-publication-write-reconciliation.md)。
Required Checks 全部通过（或没有配置）后状态才变为
`run_approval_pending`；即使此时所有自动检查通过，也只有 `approve` 会执行普通 merge
commit。合并结果与已验收的预期 merge tree 一致后，Publisher 记录可重试的 Parent
closeout 审计评论并显式关闭 Parent Issue。

`approve` 每次都会重新读取 Parent/Graph revision、Run Branch、默认分支、PR head、Fresh
Acceptance 与 Required Checks。任一漂移都会拒绝旧批准：可合并的默认分支漂移重新验收准确最新组合，
默认分支或任务分支的追加先进入共同集成流程，真实冲突按原始 Git 证据解决且不扣需求或集成问题修复额度。
新组合仍先完成整体 Run Acceptance；其 Findings 与最终 PR Required Checks 失败进入已有有界 Run Repair。Run Repair 遵循
Repair → Candidate Run Acceptance → 严格 promotion 的路径；若 promotion 的 Candidate、repair base、实际
Run tree、Parent、Graph 或 Completion 任一非 default 绑定失配，才回退到 fresh Run Acceptance。若只有
default head 前进，则在同一 Repair Cycle 验收最新组合且不重置代码修改预算；准确写入后的成果在重新验收中又收到 Finding 时，
Controller 保留原修复工作身份、Repair Thread、受管 Worktree、本地分支与累计计数，继续当前 Parent 开发和集成循环。

两种 Parent 的 `approve` 先在终端说明自动更新规则：若默认分支或原任务分支新增可接纳的提交，将自动更新与复验，并重新请求批准。确认存在这类变化后，会向已启用的飞书提交更新通知，无需再次确认。旧批准段结束后，同一 Run 自动同步；真实冲突携原始证据交给修复，再完成 Reviewer 与适用检查，更新原 PR。此时可用 `stop` 和 `resume`，保留成果及有效预算历史。`actions` 中的 `status=updating` 仅表示更新已启动；新组合通过后仍需再次 `approve`，这次最终合并及必要收尾仍不可取消。通知不可用不阻止更新；需求或图谱变化仍按原门禁处理，身份、权限或非追加历史仍暂停处理。resync 额度耗尽时暂停并通知；显式 `resume` 只续期该额度，不重置需求、集成验收或 CI 的有效预算。

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
`unsupported_scope_change`，保留已接受边界与新观测，不自行替换任务图。
通过 `status --json` 与 `history --json` 查看 gate、需求观测及历史证据。

该状态不允许 `run`、`resume`、`approve` 或
`revise` 越过边界，不自动 Requeue，不创建 Codex Thread，也不执行 Git/GitHub Publisher
mutation。MVP 不提供 `confirm-structure`；操作者只能恢复 GitHub 原图后重新核验，或执行
`abandon` 停止 Run。

没有可执行工作时，通过 `subjects[].gates`、`scheduling`、`operator_actions` 和 `next_action`
区分依赖等待、额度、局部人工门禁或全局暂停，不从空队列推断全部完成。
`run_acceptance_pending` 只表示已进入完整 Parent 的集成、修复或审核，尚未交付默认分支。

## 权限边界

Worker host/PAT/App 使用原生 `gh`，不挂载替换入口、不经过命令代理或本地 allowlist。host 复用宿主认证；PAT 使用所选引用的当前值。它们的 GitHub 能力由用户凭据决定，需要较小权限时由用户在 GitHub 收窄权限。Worker 得到选定身份需要的凭据，程序不会主动向其注入独立 Runner 秘密；host 身份可能与程序端相同。凭据秘密值不进入 Run 快照、普通输出和诊断。

App 在程序端签发并自动续签，短期 token 通过每次 Invocation 隔离的 `GH_CONFIG_DIR/hosts.yml` 原子更新；同一 Agent 正常继承环境的后续 gh 子进程读取新 token，无需重建会话。不同权限和仓库范围的凭据实例隔离；不再替换 gh、解析命令或运行 socket 代理。临时续签失败有界重试，有效旧 token 可继续使用；失效后进入 Execution Failure。App 私钥不主动提供给 Worker。Runner 选择 App 时复用签发及续签实现，使用独立凭据实例；后续 GitHub API 与远端 Git 调用消费当前有效 token。程序端使用安装授权的完整范围，不继承 Worker 的只读或当前仓库收窄；权限不足明确失败，不自动扩大授权。

Codex 使用 YOLO，可读写宿主文件系统、联网及使用真实 Git CLI。外层 bubblewrap 继续只读保护权威 Git metadata 和 Runner 控制状态；启动前拒绝 local Git config 中含 userinfo 的 HTTP(S) remote URL，并返回固定脱敏错误，不改写 config。这不是通用安全沙箱，不承诺防御恶意进程、主动搜索宿主凭据、宿主污染或数据外泄。

GitHub 写能力不替代角色职责：开发只整理文件，审查不改交付内容，Publication Agent 只生成文案；不得自行提交、正式发布、自我批准或关闭任务。Controller 保留验收、Currentness、正式批准和完成判断，正式发布由 Publisher 程序执行。Controller 向 Agent 提供适用的需求 URL 与原始失败证据，Agent 使用 Git/gh 自行读取事实。

已有 Run 的凭据故障保留进度和诊断，修复同一凭据引用后显式 `resume`，不隐式换来源、不归为代码 Finding。停止、失败和正常退出释放本轮临时资源。普通测试通过 gh、Codex 和服务替身验证配置、凭据传递与恢复；替身通过不代表真实外部工具兼容性已经验证。

Development Codex 使用 `skill:implement` 完成实现、自测和真实核心路径验证；当前角色 Prompt 的
风险相称验证、预检与只修改工作树边界优先于该通用 skill 的完整测试、Review 或 commit 建议。Development
先自行检查当前完整工作树、已知风险与未处理问题。低风险局部改动可以直接收口；独立风险预检能增加价值时，Prompt
规定默认最多一个 Development Preflight Round，一轮可按风险派发多个审查型 subagent。审查型
subagent 使用 `fork_turns: "none"`，由 Development 自行提供必要的中立任务事实、当前范围和真实证据；
探索、调研与并行实现等其他 subagent 是否继承上下文仍由 Development 判断。内部审查遵循
`skill:code-review` 的 Standards/Spec 方法并覆盖未提交和未跟踪交付内容；Development 汇总 findings、
修复并自行复验后收口，不常规启动下一轮。已有 Acceptance、Required Checks、Git Integrity、人工修订
或合并冲突等权威 Repair Evidence 时不启动内部 Reviewer。Fresh Acceptance
不接收 Development Summary 或开发侧验证结论，在独立只读 Validation Checkout 中形成
E2E、Standards 和 Spec 三条独立验收 lane；Reviewer 必须调用 `skill:code-review` 并对三条 lane
的最终判断负责。所有审查或评价型 subagent 使用 `fork_turns: "none"`。需要写入的验证中间产物
必须放在 checkout 外可定位、只服务本轮并在结束前清理
的临时路径。Prompt 不固定预检任务包字段、subagent 数量、检查命令、调用顺序或嵌套层级。
Controller 不解析 Codex 内部事件流来审计 subagent 身份或 skill 调用；它信任上述
Prompt 合同，并确定性校验 Fresh 父 Reviewer 不复用 Development/旧 Reviewer Thread、
三个 lane 均有合法状态和证据，以及外层 SHA/Revision 绑定。Reviewer Prompt 显示当前和剩余自动
验收次数，定向 Repair 显示已完成次数；两者都以“根据当前可用额度，最多还可自动启动”表达
剩余独立验收次数。该投影不改变门禁或预算计数。

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
Invocation；具体上限由该窗口冻结的 `N+1/N` Policy Snapshot 决定。准确 Required Checks 失败回到同一开发循环，不要求先分类为代码可修。Ticket 使用独立累计 CI Development 额度 `ticket_ci_development_limit`（默认 3），新失败版本与 PR 更新不重置；`ticket_ci_review` 默认关闭，开启时所有轮次一致审核。免审以绑定准确旧发布授权、失败证据和新候选的收据更新原 PR，不生成 Acceptance。CI 工作完成后再集成，集成不增加 Review。Development Attempt 在首次
分配时占用预算；进程失败、Output Repair、Human Blocker Resume 和 `--new-thread` 只继续同一
Semantic Agent Attempt，不重复计数。普通需求预算耗尽且仍需修改时进入
`modification_budget_exhausted`；Ticket CI 累计额度耗尽产生 `ci_development_budget_exhausted`，仅显式 resume 续期对应 CI 窗口。维护者显式 `resume` 开启新的编号 Budget Window。Reviewer
额度耗尽后的 Candidate 则继续按 Fallback Publication Receipt 和实际 Required Checks 规则处理。

## Source Runner 安装

首次公开接入在所选源码目录执行 `./setup.sh`，集中检查与确认后准备宿主并调用底层安装器。
补齐人工待办后重跑同一入口；详细说明见 [安装参考](install.md#首次安装)。
以下描述保留的 `./install.sh` 本体生命周期。release tag 是稳定使用路径；
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
会清理候选并保留原 Active/previous、Task 位置记录、用户配置和运行数据。

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
受管 PATH 块，保留固定锁、GitHub App profile、Task 位置记录、私钥文件、Runner 独立仓库和运行数据。
同名非受管 `~/.local/bin/agent-run` 永不覆盖；用户替换入口时保留用户内容并报告清理未完成。

安装完成后，稳定入口只是指向 Active Snapshot console entry 的 symlink；安装器不驻留、不启动
Manager/Launcher/daemon。安装、更新、rollback 和 uninstall 不迁移、修改或绑定 Delivery Run。
生产生命周期使用安装后的 Active Runner；源码或 editable checkout 不作为生产入口。
源码来源、安装身份不替代目标仓库的 Required Checks、Worker 读取权限、Publisher 写凭据、
当前状态合同或最终人工批准。当前安装与接入步骤统一见[安装指南](install.md)。

## 开源用户 Quickstart 与 doctor

源码仓库用于构建 Runner；Runner 从目标远端独立克隆代码，并在自己的数据目录保存 Delivery Run、Run Branch 和 worktree。用户仓库仅供只读识别远端，未推送内容不参与交付，合并后由用户自行更新本地代码。稳定使用先选择 release tag，开发者才选择 branch、fork 或 dirty source：

```bash
git clone https://github.com/GRD-Chang/grill-engineering.git
cd grill-engineering
git checkout <release-tag>
./install.sh

# 重新打开登录 shell 后，可在任意目录执行
agent-run doctor --json
agent-run run <parent-issue> --repo OWNER/REPO
```

`agent-run doctor` 是可选、只读的诊断入口，不要求当前目录是 Git 仓库。它报告 Python 版本、Git、Codex、
宿主 `gh` 登录、OpenSSL、Linux `bubblewrap`、Active Runner、PATH 和 Worker GitHub 身份；JSON
输出只包含 Python 版本、路径、状态、provider 和布尔值等非敏感信息。缺少依赖只报告问题，不安装软件、不
修复 PATH、不触发生命周期，也不修改 shell、auth profile、Task 位置记录、Delivery Run、Thread、PR、
branch 或运行数据。

后续更新仍从源码目录显式执行 `./install.sh`。如果当前为 A，安装 B 后保留 B/A，再安装 C 后只保留
C/B；相同内容重复安装不会重新 probe 或增加 Snapshot，候选失败会保留旧 Active。一次回退执行
`./install.sh --rollback`，它不重建、不调用 Codex、不检查或修改 Delivery Run；卸载执行
`./install.sh --uninstall`，它清理受管 Runner、入口和 PATH 块但保留固定 `install.lock`、App profile、
私钥、Task 位置记录、Runner 独立仓库和运行数据。PATH 变化需要重新打开登录 shell；重复卸载安全，用户替换
的同名入口会被保留并报告清理未完成。

Worker host/PAT/App 来源通过 `settings show/configure` 管理，详情见[个人运行配置](user-defaults.md)。App 配置迁入 settings 不删除用户私钥；App 故障不回退 host。程序端可独立选择 host/PAT/App，默认 host。

首版范围为 Linux、用户级 `~/.profile` 和单用户安装；尚未完成发行版、版本与架构组合的完整真实宿主验证。底层安装器要求 CPython 3.11+、`venv`、
`pip`、Git、Codex、OpenSSL、兼容 `gh`、user systemd 254+、Linux `bubblewrap`、修复版 SQLite 与所选身份的目标仓库权限；Windows、macOS、
系统级/多用户安装、PyPI/pipx、常驻 Manager/Launcher、自动更新和跨平台支持不属于本版本。

## 本地状态与清理

长期数据默认位于 `~/.local/share/agent-run/`，设置 `XDG_DATA_HOME` 时使用其下的 `agent-run/`；该数据目录必须位于用户仓库之外。目录按需创建，失败时明确报错，不向用户仓库回退。

| 位置（相对于数据主目录） | 内容 |
| --- | --- |
| `repositories/<owner>/<repo>/repository/` | 从远端独立克隆的 Git 仓库；owner/repo 统一小写 |
| `repositories/<owner>/<repo>/state/kernel.sqlite3` | 仓库内Task/Run、控制、Host、Profile、预算与历史关系 |
| `repositories/<owner>/<repo>/worktrees/<run_id>/` | 准确属于该 Run 的开发、集成与发布工作区 |
| `native-task-locations/<task-fingerprint>.lock` | 原Task mutex与撤权FD；不发布或清理JSON绑定 |
| `repositories/<owner>/<repo>/state/notifications/` | 独立通知投递记录，与业务数据库和只读查询分工 |
| `repositories/<owner>/<repo>/state/runs/<Run SHA-256>/` | 准确Run的raw产物，Host临时carrier另按Run哈希分区 |
| `snapshots/`、`generations/` 等安装目录 | 安装快照和当前版本选择，与任务数据分别管理 |

配置继续位于 `$XDG_CONFIG_HOME/agent-run/`（默认 `~/.config/agent-run/`），临时环境交接文件使用运行时目录。卸载仅清理安装内容，保留任务数据。用户仓库的文件、索引、引用和 Git 元数据均由用户自行管理。

本开发候选唯一状态协议是 **`independent-delivery-repository-v23`**（尚待同版独立验收，非已部署声明），详见[状态存储](agents/state-storage.md)。
旧 JSON/v6、native v17/v18、来源封印及导入状态不升级、不转换，不提供兼容查询或恢复；
原数据、Candidate 和工作区保留，拒绝继续不授予清理权限。
稳定 Development Checkout 在 Required Checks pending 或 Worker/Publisher 普通失败、超时、
进程异常或 Ctrl-C 时保留，以恢复未提交成果；Development/Repair Codex 报告 Human Blocker 时也保留，供同一 Thread 在 `resume` 后重新核验并继续。普通 cleanup 只有在 Git 元数据证明 checkout 属于本 Run 且 `git status --porcelain` 为空时才删除。tracked/untracked 修改、元数据缺失或归属不一致都 fail closed，并在 `status`、`run`、`resume` JSON 中给出路径、原因和恢复命令。
`abandon` 在任何 GitHub mutation 之前执行同样的全 Run preflight；若维护者确认不再需要本地成果，必须显式使用 `abandon --discard-worktree`。Ticket 完成和明确 abandonment 才进入清理。
Checkout 尚未准备完成时产生的部分目录也会清理。每轮独立 Validation Checkout 在验收
结束后按归属清理；Reviewer 的构建、测试和诊断中间产物放在 checkout 外，
只清理自身产物，不修改交付内容。Codex 的临时 schema、输出
文件和空 GitHub 配置目录也会随子进程调用清理。

Graph drift fail closed 时，Controller 保留当前 Candidate、Acceptance、checkout、branch、
PR 与 Ticket completion，不静默清理或改写。只有正常完成或 `abandon` 路径可以按既有
Publisher/cleanup 权限处理托管资源；观察到的新 Graph 本身不产生任何 mutation。

每个 Ticket 的角色工作、Thread、PR、merge、gate 与预算保存在准确 subject 及关联表中；
调度使用持久 FIFO 和集成单席，不另存 `active_ticket_job` 或可变 Job JSON 快照。
成功恢复保留原工作、预算、历史和适用证据；已保存的角色返回先由程序消费，不能重复调用或计费。
进程中断先核验准确退出，Git/GitHub 未知结果沿原 intent/receipt 读回，不能重复关闭或合并。

### Ticket 集成 Development 循环

Ticket 原开发—审核与规定上限后的末轮跳过保持原合同；取得有效审查或 fallback 凭据后进行本地集成，集成不另调用 Review。Git 合并、整理后的检查或提交失败携具体操作、准确 source/target/candidate、HEAD/MERGE_HEAD 和原始输出回到 Development；处理后程序重新检查、提交及集成，随后普通 PR/CI 合入 Run Branch。Agent 自述不能作为 Git 成功证据。

`--ticket-integration-development-limit` 默认 3，为独立累计额度，包含冲突工作。新失败和远端追加不清零，重复读状态及同一 Semantic Attempt 的中断续接不额外扣费。额度 gate 保留工作树及 integration 用途，`resume <run-id> --ticket <number>` 只续期对应耗尽窗口并保留历史，不重置需求、CI 或 resync 额度；重复恢复不重复授权。`--new-thread` 使用完整集成 Prompt，`--message` 继续进入同一工作来源。普通未知原因恢复不续期其他额度，也不解除其他 Ticket 的人工门禁。

status/history 区分需求开发与集成开发累计用量、额度窗口及语义工作。Git 机械操作限时 30 秒，超时保留现场和输出；工作使用原共享 Ticket Agent 名额与 FIFO，集成交付继续单席，正常 CI 等待不占 Agent 名额。新状态不迁移旧 Run。详见 [ADR 0020](adr/0020-ticket-integration-development-loop.md)。

完整 Parent 修复 PR 与最终 Run PR 分别遵守各自目标分支的真实检查规则。修复 PR 发布或合入响应丢失时，先按原意图读回，未知结果不当作未发生，也不重建 PR 逃避原记录。权限、触发或规则不满足时保全成果，说明准确缺口，经处理后通过 `resume` 继续；Runner 不修改 Workflow、规则或强推绕过。最终合入默认分支仍须当前整体验收、该最终 PR 的检查与明确人工批准。
