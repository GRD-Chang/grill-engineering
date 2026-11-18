# Worker、调用与身份

开发与发布准备如何分配给 Worker，以及程序如何保留会话和凭据边界。Agent 的输入和输出见[Prompt 合同](../agents/agent-prompts.md)，本机配置见[个人运行默认配置](../user-defaults.md)。

[按任务查阅术语与合同](../../GLOSSARY-MAP.md)

## 角色与执行约束

### Codex Worker（Codex 工作器）

在隔离工作区内进行规划、代码编辑、验证或审查，并返回结构化 Artifact 的智能执行者。其 GitHub 能力由所选凭据决定；拥有写能力不授予正式发布、自我批准或完成交付的职责。
_Avoid_: GitHub Bot、Publisher、Mutation Authority

**Worker GitHub Identity（Worker GitHub 身份）**:
支持 host/PAT/App 原生 `gh`；无配置默认 host，已有 App 元数据须显式迁入统一配置，保留 App 来源。App 默认采用安装授权的权限和仓库范围，可独立收窄为只读或当前仓库。新 Run 固定非秘密来源、凭据引用与范围，恢复不随个人默认改变。
操作者选择供 Worker 访问 GitHub 的身份，确定该身份在 GitHub 上获授的能力。该授权与 Worker 的角色职责、Runner 的交付授权分别定义，不因拥有 GitHub 写权限就取得 Runner 的发布或完成权威。
_Avoid_: Publisher、Mutation Authority

**Runner GitHub Identity（程序端 GitHub 身份）**:
支持 host/PAT/App，默认 host；与 Worker 独立选择，共享 PAT 引用和 App 定义。程序端 App 使用安装已授权的完整权限及仓库范围，不继承 Worker 的收窄选项，按调用上下文与实际范围隔离 token。
Controller 与 Publisher 执行程序端 GitHub 读取、正式发布及远端 Git 操作所使用的身份。它与 Worker GitHub Identity 的职责分开；Worker 的选择不改变程序端身份，也不替代交付批准。双方选择同一来源时，可能使用同一 GitHub 操作身份；凭据实例隔离不代表不同账号。
发布者核验支持个人用户与 App bot；评论、事件、写入意图及恢复读回均保留作者核验。结果未知时按固定身份、准确 head、意图与远端事实对账，不盲目重放或认领外部操作。App 续签对后续程序端 GitHub API 和远端 Git 调用生效，不回退宿主身份；不可恢复故障保留进度并从同一引用公开 resume。
_Avoid_: Publication Agent、独立 Publisher 账号、Mutation Authority

**Worker Credential Delivery（Worker 凭据传递）**:
程序端签发 App 短期 token，以每次 Invocation 独立的 `GH_CONFIG_DIR/hosts.yml` 原子更新交付给原生 `gh`；同一 Agent 正常继承环境的后续子进程读取更新值，无需重建 Agent。私钥只保留文件引用，不主动提供给 Worker。不同权限和仓库范围的凭据实例隔离；传递不解析 gh 命令，不承担 GitHub 授权。
临时续签失败有界重试，有效旧 token 可继续使用；不可恢复失效进入 Execution Failure，保留进度，修复同一身份引用后公开 resume。停止、失败和取消回收续签线程、签名进程及临时凭据目录。
_Avoid_: gh 请求代理、命令白名单、向 Worker 注入 App 私钥、静默切换来源、无限重试

**Execution Guard（执行约束）**:
Codex 以 YOLO 运行，并可自由读写宿主文件系统、联网及使用真实 Git CLI；Execution Guard 不提供通用 filesystem、network、审批或命令隔离。所有 Worker 使用原生 `gh`，GitHub 能力由所选凭据决定；App 在程序端签发和续签短期 token。Execution Guard 通过只读权威 Git metadata、Runner 控制状态保护和不主动注入独立程序端秘密保留正式 Mutation Authority；Worker 启动前会拒绝 local Git config 中带 userinfo 的 HTTP(S) remote URL，并只返回固定脱敏错误，不改写权威 config。该边界不承诺抵抗恶意进程、主动凭据搜索、宿主污染或数据外泄。
_Avoid_: hardened security sandbox、恶意代码隔离、全局 Git 配置、Publisher 权限

**Trusted Subagent Contract（受信任 Subagent 契约）**:
Initial Development 与各类定向 Repair 统一按风险选择最多一轮 Standards/Spec 独立预检，使用 `fork_turns: "none"` 和中立事实；处理结果后自行检查和复测，不反复通用审查。Ticket、Parent-only 与 Run Acceptance 的主 Reviewer 使用 `code-review`，分别组织 Spec、Standards、E2E 三个独立子 Agent，并负责资源协调、核实证据、合并同根因及最终结果。E2E 承担适用完整测试，其他方向定向验证具体风险。每轮依据固定 Review Context 和适用原始结果形成新结论，不能以开发自述、旧结论或背景扩大当前范围；Controller 不审计内部命令或子 Agent 拓扑，保留父 Reviewer 与准确对象的返回绑定。
_Avoid_: 固定每轮双开发侧预审、Controller 内部 Agent 编排器、Finding closure 状态机、subagent provenance ledger、把 Prompt SOP 当作可审计调用图、父 Agent 自签

### Controller（控制器）

本地 `agent-run` 单进程中的确定性编排层。它读取 GitHub 与本地事实、维护状态机和 Revision、选择可执行 Job、启动 Codex Threads、校验 Artifacts、执行预算与门禁，并调用 Publisher 完成允许的写操作；它不替 Agent 做需求、代码或修复方案的语义判断。
_Avoid_: Codex Worker、独立 daemon、GitHub Mutation Authority

## 开发工作与产物

**Agent Artifact（Agent 产物）**:
Codex Worker 返回的结构化意图、判断与证据。它可以包含代码变更的语义说明及待发布内容，但本身不授权任何外部写入或完成状态。
Development 用 `development` 表示工作已完成，用 `incomplete`、非空 summary 与 null human_blockers 如实表示没有外部人工阻塞的未完成工作。Controller 不解析摘要措辞；准确最终返回、Thread 和真实退出证明齐全时，同一 Attempt 最多自动语义续接一次，保留原 Thread 与绝对截止，并与异常恢复、容量等待及只读格式修复分别计数。缺证、到期或用尽后保留原证据并暂停，显式 Resume 不重扣本次 Development。完成但无代码变化可复用准确现有 commit 进入独立 Review，不制造空提交，也不借 Ticket fallback 跳过这次必要复核；Human Response 原文不授予免审或合并权限。
_Avoid_: GitHub 状态、完成证明、自由文本交接

**Prompt 用语约定（Prompt Language Convention）**:
面向 Codex Worker 的角色、任务、范围与完成条件采用所选语言的清楚直接工作用语（默认中文，支持英文），不预设它理解本项目的领域术语；内部领域定义继续保留，Prompt 用具体责任与动作表达对应含义。必须与程序、JSON、命令或 Skill 精确匹配的名称保留原文。自然语言输出只由 Prompt 引导，不做语言识别、比例验收、纠正、翻译或额外调用；合法另一语言输出、原始标题、用户反馈及技术证据原样保留。
_Avoid_: 用 Ticket 或 Review Boundary 等名称代替任务说明、要求 Worker 理解 Delivery Run 的全局流转、翻译机器字段

**Development Brief（开发简报）**:
提供给 Development Codex 的最小任务输入，包括明确标注用途的需求 URL、不可重建的原始失败证据、GitHub 读取约定和输出要求，需求正文由 Codex 自主读取。有当前 Ticket 时，其 URL 标为本次具体任务，Parent URL 标为整体需求背景；Parent-only 与 Run Job 按各自范围标明本次负责的完整需求。
_Avoid_: 并列 URL 却不说明哪个是当前任务、自动注入需求正文或摘要、Change Job Record、完整环境快照、实现计划

**Delivery Hygiene（交付卫生）**:
Development 与 Repair 在当前 checkout 中承担完整交付整理责任，因为当前工作树的修改与未忽略的新增文件会整体纳入 Candidate Commit；这一后果与整理责任必须在角色 Prompt 中说明。具体责任是检查全部未提交内容，保留本任务必须交付的代码、测试、文档和配置，删除本次产生的临时、构建和测试产物；仅长期、可再生且不应版本控制的项目产物可进入 `.gitignore`。Fresh Acceptance、Run Acceptance 与 Publication 只能清理自己创建的验证或临时产物，不得整理交付内容或修改源码、测试、配置和 `.gitignore`。任一 Codex 在 checkout 外创建的临时路径必须可定位、只服务本次任务并在完成前清理，不得进行宽泛删除。Codex 不提交，最终 Git/GitHub 写入仍属于 Publisher。
Fresh Acceptance 与 Run Acceptance 优先使用项目支持的外部缓存和运行目录；确实无法在只读 checkout 运行时，允许创建仓库外的临时运行副本。副本必须对应当前实际待验收内容，包括未提交修改与未跟踪交付文件；运行前后核对原始交付文件一致，不得修复或改写副本中的源码、测试、配置及依赖声明后将其通过用于原交付。Reviewer 在 evidence 中说明对应对象及一致性核验情况，并关闭本轮进程、清理副本和运行产物。该能力沿用受信任 Prompt 合同，不增加副本状态机或 Controller 自动复制流程。
_Avoid_: 用 `.gitignore` 隐藏交付、验证者修改交付、只复制 HEAD 遗漏当前验收内容、修改运行副本后宣称原交付通过、留下不应交付的中间产物、Codex 自行提交

准确 Run 的身份读取、目录定位与命令准入分工见[Run 选择与定位](../agents/run-selection.md)。

**Managed Development Checkout（受管开发工作区）**:
Controller 为一个 Change Job 创建并在 Development Attempt、Repair 与 Resume 间复用的专属 worktree。它与主工作区、其他 Change Job 和一次性 Validation Checkout 隔离；系统只允许该 Job 的 Codex 整理其未提交内容，并在 Candidate 创建后交由 Publisher 写入 Git 历史。系统运行约束禁止向其中人工混入无关改动。普通 `run`、`resume` 或自动 cleanup 发现其中存在 tracked modifications 或 untracked files 时必须 fail closed，保留工作区并给出路径、保留原因与可执行恢复动作，不得因 `execution_failed`、`blocked`、预算检查点或状态合同错误自动强制删除。
_Avoid_: 主工作区、共享 scratch worktree、Validation Checkout、以终态名称代替 dirty check、自动强制删除未提交交付成果

**Development Attempt（开发尝试）**:
Development Codex 在一个 Change Job 的准确 Effective Revision 上完成内部规划、代码编辑和开发验证的一轮工作，其与前后轮的会话关系由 Development Thread Policy 决定。一次 Attempt 只占用一次 Development Budget；为完成该轮工作的执行恢复、人工回应续接或输出格式修复均不构成新开发轮。
_Avoid_: Change Job、Plan Artifact、Acceptance Attempt、Codex 进程启动次数

**Development Preflight Round（开发预检轮）**:
Development 在实现或修复完成验证与自行检查后的内部质量预检。首次开发及 Findings、CI、集成等修复均按风险决定是否组织最多一轮独立 Standards/Spec 预检，不要求同时改变原方案并新增风险。子 Agent 使用 `fork_turns: "none"` 和中立需求、范围、代码与真实证据，覆盖未提交修改和未跟踪交付文件；处理该轮问题后自行检查与复测，不反复启动通用审查。该轮不产生 Acceptance Artifact，不替代独立 Fresh Acceptance；非审查协作仍由 Development 按任务组织。
_Avoid_: 内部验收、只审已提交 HEAD、遗漏未跟踪文件、把一轮等同于一个 Reviewer、把开发结论作为审查前提、修复后常规再派 Reviewer、无新增关键风险仍重复审查、Fresh Acceptance

**Development–Acceptance Engine（开发验收引擎）**:
Controller 复用 Development–Review 的共同变更循环：Development 修改 checkout，程序创建准确 Candidate，Fresh Reviewer 输出 Acceptance Artifact，Findings 原样返回开发。Ticket 与 Parent-only 验收后调用 Publish 并由程序推送对应 PR，等待准确 Required Checks 与 Published-Head Gate，再按对象门禁合入；Ticket 兜底只有在准确 head 形成 Deterministic Integration Record 后取得合入权限。完整 Parent 的修复共用开发、Review、预算与结果续接能力，在适用集成和验收后由程序通过 Run Repair PR 的检查与合入更新 Run Branch；不另建一套修复预算或重复审核流水线。Parent-only 的人工批准只授予准确对象的合并权限，批准后的重新核验、合并与恢复仍由同一交付能力执行。
Development–Review 阶段由同一有限循环推进：Ticket 与 Parent-only 从 Development 开始，完整 Parent 在既有远端集成后从 Review 开始。候选提交、审核结果恢复、Findings 后修复和预算暂停共用推进与记账规则；完整 Parent 仍独立审查完整需求、Ticket Set、累计变化及准确预期合并结果。完整 Parent 的 Repair 保留准确集成、Run Branch 写入与验收提升边界，当前候选通过不能自行代替最终交付。已返回的 Run Review 结果先与 Semantic Attempt、预算一同落盘；版本回读中断时续接该结果，边界变化则丢弃结论。

集成交付提供单步推进结果（可继续、等待、需要 Agent、暂停、完成），完整交付入口调用同一能力。编排方未提供 Agent 名额时，程序仍可进行机械集成、已发布版本的 CI 观测和合入对账；真实冲突、适用审核或新文案在分配 Attempt 前交还编排。一轮准入最多执行一次 Agent 工作，后续工作再次交还。该接口不自行调度其他 Ticket，也不改变最终完成权威。
_Avoid_: Ticket 专用流水线、Run Repair 专用流水线、动态 Agent 编排

**Development Thread（开发线程）**:
Development Codex 完成一轮或连续多轮开发工作的独立会话，其跨 Development Attempt 的复用关系由 Development Thread Policy 决定。它保存本会话的开发与修复上下文，不与任何 Reviewer Thread 共享身份；新会话必须独立建立当前任务与权威需求上下文，不能假定掌握旧会话历史。
_Avoid_: Development Attempt、Reviewer Thread、Delivery Run 全局会话

**Development Thread Policy（开发会话策略）**:
Run 创建时从个人默认与显式覆盖解析并固定：`reuse`（默认）跨轮复用；
`new-per-attempt` 在共享 Development–Acceptance Engine 分配每个新 Semantic Development Attempt 时
归档旧 Thread 身份并选择新会话，Ticket、Parent-only 与 Run Repair 一致。
验收 Finding、Required Checks、Git Integrity、人工需求修订、合并冲突、独立 CI 恢复及新预算窗口
分配的新开发轮均适用。轮换不改变 checkout、已有及未提交代码、Candidate/PR、Generation 或预算授权。
同一 pending Attempt 的异常、容量恢复、人工回应与 Output Repair 保留原 Thread；
显式人工替换是同轮例外，仍继承已用恢复及输出修复次数。新 Attempt 独立计数。
选择与 Attempt 一同先保存，运行中的 Thread 身份经 Invocation 事件持久化，历史身份继续参与 Reviewer 隔离。
新 Thread 绑定当前 Run 当时有效的 Profile Revision，不重读个人默认；已有 Thread 的模型与 effort 不变。
新自主开发和新定向修复分别取得完整合同、需求链接、checkout 和适用原始证据，
续接使用相应紧凑合同，不复制历史对话或交接包。Reviewer 与 Publication 会话规则不变。
该策略不能在已有 Run 热切换，也不承诺减少 Token、成本或提升质量。
_Avoid_: 每次进程启动新会话、每张 Ticket 新会话、单次人工替换 Thread、Reviewer Thread 策略

**Development Summary（开发摘要）**:
Development Codex 在 Development Attempt 结束时返回的普通、可读最后回复。它说明完成的工作、运行过的验证、已知阻塞及值得人类注意的后续影响，但不要求符合 JSON Schema，也不构成候选状态、测试通过或完成的权威证明。
_Avoid_: Publication Artifact、Controller Evidence、完成证明

## Attempt 与 Invocation

### Agent Invocation（Agent 调用）

Ticket、Parent-only 和 Run Repair 的
Development、Fresh Acceptance 与 Publication Invocation 都在首个 Output Attempt 前成为 active，
并绑定 Work Subject、Generation、输入指纹、机械 Currentness Boundary、当前 Semantic Agent Attempt 与实际 Thread Execution Binding；记录只保存输入指纹、有界边界事实、model、reasoning effort 与 Agent Profile Revision，
不保存逐次完整动态 Prompt、transcript 或 Acceptance Artifact；Run 根状态只保存一次静态 Prompt 资源。`thread.started` 在进程运行中
立即保存。零退出但不符合完整阶段 contract 的输出可在同一 Thread 中最多修复两次；repair
checkout 只读，使用按角色区分的短格式 Prompt，且不增加领域 Development Attempt、Reviewer Invocation 或 Publication Attempt。一个 Invocation 只有一个覆盖初始 Output Attempt 与其 Output Repair 的按角色总 Deadline；Development 默认五小时，Review 默认两小时，Publication 默认一小时，均可由 Run Policy Snapshot 中的角色覆盖值替代。successor Invocation 获得新的完整角色 Deadline，但仍可属于同一个 Semantic Agent Attempt。进程失败、缺失或
不匹配的 Thread 结束当前 Invocation；只有符合 Automatic Invocation Recovery 边界的异常才允许自动同 Thread 恢复，普通异常最多一次，准确容量不足可持续恢复，不自动创建替代 Thread。自动或人工同 Thread 续接均按当前角色使用短 Prompt；操作者可用 `--new-thread` 明确以相应角色和任务模式的完整标准 Prompt 新开 Thread。
_Avoid_: Development Attempt、自动替代 Thread、领域 retry

### Semantic Agent Attempt（语义 Agent 尝试）

一个 Attempt 绑定有效 Currentness Boundary 与准确角色序号。它在首个 Codex 进程前分配，可跨越初始 Invocation、successor Invocation、Thread 替换与 Output Repair；执行失败、Human Blocker 或前台中断都不创建新 Attempt。Development 在凭据就绪、实际 Worker 最终放行准入时占用一次 Development Budget，或由准确实际返回工作证据补齐尚未记账的一次用量；凭据准备失败不计费，Reviewer 在形成合法 Acceptance Artifact 时占用一次 Review Budget，Publication 在分配时计入一次 Publication Attempt；三者的 Resume 均不重复计数。已持久且尚未收口的 pending Attempt 优先于新 Attempt 的预算门禁：即使窗口计数已达上限，Controller 也必须先继续该 pending Attempt，不得将它拒绝为预算耗尽。只有当前 Attempt 已形成完整角色结果，或其 Currentness Boundary 失效并按规则收口后，才能分配后继 Attempt。
_Avoid_: Agent Invocation、Output Attempt、Codex 进程启动次数、Thread 序号

**Reviewer Invocation（审查调用）**:
Controller 为一个准确 Candidate tree 或 Run 预期合并结果启动、并成功形成合法 Acceptance Artifact 的一次顶层 Fresh Acceptance 或 Run Acceptance Agent Invocation。Reviewer 在该 Invocation 内自主读取项目、运行工具、派发 subagent，以及最多两次机械 Output Repair，都只计一次 Reviewer Invocation；新 Candidate 需要新 Reviewer Thread 且成功形成合法 Artifact 时才增加一次。进程、凭据、sandbox、timeout、signal、Thread 错配或最终输出仍不合法等执行失败不消耗 Review Budget，按 `execution_failed` 暂停且不会自动无限重试。Ticket 与 Run 分别按自己的 Review Budget Window 计数。
_Avoid_: Reviewer 内部工具调用、subagent 数量、Output Attempt、Development Attempt、Required Check 重跑

**Publication Attempt（发布尝试）**:
Publication Codex 针对一个准确、仍 current 的发布边界生成合法发布 Artifact 的一轮语义工作。一次 Publication Attempt 可以包含初始 Agent Invocation、进程失败后的 successor Invocation 与最多两次 Output Repair；这些过程只用于完成同一语义 Attempt，不因再次启动 Codex 进程而重复计数。
_Avoid_: Output Attempt、Codex 进程重试次数、Publication Context Fallback

**Agent Run User Defaults（个人运行默认配置）**:
用户创建新 Delivery Run 时采用的统一运行偏好，包括开发与审查轮数、角色调用时限、模型与推理强度、Publication 配置引用，以及 Worker GitHub 来源和非秘密 PAT 引用，统一保存在个人 `user-defaults.json`，由 `settings show/configure` 读写；Development Thread Policy 同样由该入口配置。修改这些默认值不改变已有 Run 或其未来预算窗口。它不同于显式调整某次 Run 的实际配置，也不保存凭据秘密值、项目质量规则、运行状态或内部轮询与重试机制。
_Avoid_: Codex 用户全局配置、活动 Run 快照、项目共享默认、认证配置合并文件

**Agent Execution Profile（Agent 执行配置）**:
Delivery Run 为未来创建的各顶层 Codex Thread 保存的 model 与 reasoning effort 选择，可以来自命名预设或用户自定义值。它只约束 Controller 直接启动的顶层 Codex，不约束这些 Codex 自行派发的 subagent。
_Avoid_: Codex 用户全局默认、Subagent Profile、Acceptance Policy

**Agent Execution Preset（Agent 执行预设）**:
创建 Delivery Run 时可选的一组内置 Development、Review 与 Publication Agent Execution Profile。预设只提供初始值；选择时其准确 model、reasoning effort 与角色引用会被解析进 Agent Profile Revision，已有 Run 不随预设定义更新。
_Avoid_: 动态模型别名、Acceptance Policy、Codex 用户全局配置

**Agent Profile Revision（Agent 配置修订）**:
Delivery Run 当前 Development、Review 与 Publication 三组 Agent Execution Profile 的一个有序、不可变快照。用户修改配置会创建新的 Revision；既有 Thread 保留原绑定，后来创建的新 Thread 绑定修改时已生效的最新 Revision。
_Avoid_: Thread Execution Binding、Job Generation、Codex 用户全局配置版本

**Thread Execution Binding（线程执行绑定）**:
一个顶层 Codex Thread 创建时从当时有效的 Agent Profile Revision 解析并绑定的不可变角色、model 与 reasoning effort。同一 Thread 的后续 Invocation、Resume 与 Output Repair 始终使用该绑定；配置修改只影响后来创建的新 Thread。
_Avoid_: Invocation 级切换模型、运行中切换模型、Codex 用户全局默认

**Output Attempt（输出尝试）**:
一个 Agent Invocation 内的一次 `codex exec` 进程执行。初始输出是第一个 Output Attempt；仅当进程
零退出、Thread 身份正确而本地完整 contract 不合法时，Controller 才在同一 Thread、只读 checkout 中
最多追加两次按 Development、Reviewer 或 Publication 角色区分的机械 Output Repair；短 Prompt 只提供 contract 错误并要求重发本角色合法输出，不重新执行语义工作。Repair 不产生新的 Invocation、不消耗领域 Development Attempt、Reviewer Invocation 或
Publication attempt，也不适用于进程、凭据、sandbox、timeout、signal 或 Thread 错配失败。
_Avoid_: Invocation Resume、智能重试、独立持久 journal
