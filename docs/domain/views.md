# 进度、历史与通知

操作者视图、活动证据和通知的产品语义。字段与投影实现的维护归属见[展示事实](../agents/display-facts.md)，通知配置和投递见[通知指南](../notifications.md)。

[按任务查阅术语与合同](../../GLOSSARY-MAP.md)

## 查询与展示

**Delivery Progress View（交付进度视图）**:
面向用户的当前工作摘要与关键工作历程；Agent 角色统一称为“开发 Agent／验收 Agent／发布 Agent”，工作动作使用“整体修复／整体验收／发布”等可理解名称。已确认的新版展示合同发布于 相关设计记录。展示要求：status 完整显示当前相关问题，通过后移除旧问题；history 按语义轮次归组并保留问题原文，`--details` 展开证据和续接过程；创建 PR 属于发布阶段，PR 检查、合并及收尾保留独立记录。这些规则优先于下文旧版展示细节；设计合同不代表实现已完成。
`status` 与 `history` 默认提供的面向操作者 CLI 文本视图，采用分段摘要组织，而非前端面板、原始字段表或内部状态转储。`status` 只读返回一次当前视图，不创建、恢复、停止、持续观察或修复 Run。`status` 依次回答运行对象与状态、整体及当前轮次进度、总时长、当前 Agent 的模型与推理强度、当前 Findings、系统下一步与用户是否需要操作；显示当前门禁时，说明准确 Ticket/Parent Issue 或 Run 对象、角色与阶段；Ticket 局部门禁需区分其他独立 Ticket 的进度，Run-wide Operator Gate 才说明整个 Delivery Run 已暂停。Task Control Record 不可读取或无法与 Host 对账时，`status` 与 `history` 仍展示 Delivery Run 中可独立验证的进度和历史，但明确说明当前 Agent 是否运行暂时无法确认，且生命周期动作会在提交前先执行 Task Control Reconciliation；它们不得猜测执行状态或把故障控制事实写回。触发 blocker 的 Invocation 已结束，因此阻塞项显示“触发阻塞的 Agent”及其角色、模型、推理强度与本轮时长，不将其误写为当前仍在运行的 Agent。当前 blocker 原文完整展示。`history` 按设备本地时间叙述 Development、Review、Required Checks、集成、Human Blocker、Human Response、恢复与完成等关键里程碑，并在结尾汇总轮次和总时长；较长的历史 blocker 与 response 只做简单、明确标记的确定性截断，完整原文留在 Machine Audit View，不引入摘要 Agent 或新的语义处理。内部身份和完整审计事实不属于该视图。
_Avoid_: 前端面板、调试转储、机器审计接口、完整内部状态、仅对齐字段的运维表格

**Operator Action View（操作者动作视图）**:
Delivery Progress View 在 Run 需要维护者介入时采用的统一区块，固定说明准确类型、所在 Ticket/Parent Issue 或 Run 对象与阶段、原因、已保留成果、影响范围，以及唯一允许的下一步；Human Blocker 额外显示触发阻塞的 Agent 角色、模型、推理强度与本轮时长。Human Blocker、Review Budget Checkpoint、Execution Failure、Supervision Timeout Pause、Requeue Required 与最终批准继续保留各自真实语义，不因共享版式而被统称为 Human Blocker。
全部 Ticket、Parent-only、Run Acceptance/Repair、Run Publication 和独立 Run 边界逐对象派生同一份只读事实，保留 Work Subject、适用 Generation、原因/原始证据、局部或全局范围及结构化动作。Status、History、操作指引和飞书共同消费；JSON 的 `operator_actions` 包含全部当前待办，单项 `operator_action` 仅为首项视图。非当前 Ticket 暂停不遮蔽其待办，也不阻止独立工作；全局与局部待办可以并存，各自恢复。正常等待与暂时失效的旧批准不成为人工待办。
_Avoid_: 把所有暂停叫作 Human Blocker、只显示内部状态码、缺少恢复动作、把局部门禁说成整项 Run 暂停

**Human Run Selector（人类运行选择器）**:
操作者为只读 `status`、`history` 与 Run 发现命令选择 Delivery Run 的面向用户身份。当前仓库仅有一个进行中 Run 时可省略选择参数；当前仓库内可用 `--parent <issue-number>` 选择 Parent，任意目录可用 `--repo <owner/name> --parent <issue-number>` 组成跨项目选择器，`runs` 可按当前仓库或显式 `--repo` 列出候选。若同一仓库与 Parent 仍匹配多个 Run 或多个本地 clone，Controller 不根据最近时间猜测，而是显示仓库工作目录、Parent、状态与开始时间并要求进一步消歧。完整不透明 Run ID 与显式 `--state-dir` 继续用于自动化、精确排障和歧义恢复，但不是普通用户需要记忆或默认展示的身份。
_Avoid_: 把 Parent Issue 当成全局唯一身份、全局 `latest` 猜测、要求用户记住哈希式 Run ID、模糊前缀匹配

**Lifecycle Parent Selector（生命周期 Parent 选择器）**:
操作者为当前仓库中的 mutation 命令选择 Delivery Run 的统一用户合同：`run`、`resume`、`requeue`、`approve`、`revise`、`stop`、`abandon` 与 Run-scoped `configure` 都以位置参数形式接收 Parent Issue number，并可用显式 repository identity 校验目标。`run` 可以在不存在未完成 Run 时创建后继 Run；其他命令只选择一个已经存在且满足该命令当前适用边界的准确 Run，不创建新 Run。尤其是 `resume` 只匹配唯一可恢复边界，`approve` 只匹配唯一最终批准门禁；其余命令也必须遵守各自状态合同。零匹配或多匹配都拒绝，不根据最近时间猜测。完整不透明 Run ID 只保留给自动化、精确排障和歧义恢复，不是普通操作者流程的必填身份。
_Avoid_: 各命令使用不同 Parent 语法、把 Parent Issue 当成跨仓库身份、忽略命令专属门禁、mutation 隐式创建 Run、最近 Run 猜测、要求普通用户记忆 Run ID

**Machine Audit View（机器审计视图）**:
`status --json`、`history --json` 与公开生命周期命令的 `--json` Action Receipt 提供的稳定机器可读视图，保留诊断、自动化和精确审计所需的内部身份、版本绑定、调用记录、Failure Diagnostic Evidence 与计数层级；Host Diagnostic Log 仍通过宿主诊断入口查看，不复制进该视图。
_Avoid_: 默认交付进度视图、面向用户的叙事摘要

**Operator Display Time（操作者显示时间）**:
Delivery Progress View 中的事件时间使用执行 `status` 或 `history` 的设备本地时区渲染，并明确标注所用时区；设备时区不可用时回退到 UTC 并明确提示。
_Avoid_: 直接展示持久化 UTC、无时区时间、根据仓库推断时区

## 通知事实

**Notification Facts（通知事实）**:
通知只读消费 History 已关联的 Semantic Attempt、Invocation、Acceptance Artifact 和当前交付事实。History 的 `status_code` 是与 `status_text` 同源的稳定结果分类；通知不比较展示后的状态、标题或角色文案，当前动作与 Status 共用原有动作判定。事件身份仅包含业务身份与结构化结果，不包含语言或翻译文本；恢复筛选使用稳定 Attempt 身份。卡片使用 Run 固定语言及统一短文案入口，原始标题、摘要、Findings 和证据不翻译。关键节点采用下述精简规则，详细过程对同一事实只描述一次。重要告警的 unknown 允许在持久化次数和期限内重试，普通进展 unknown 不重放；领取前核验准确当前对象，解除后再发生使用新身份。异步发送、正常退出有界收尾、只读查询与默认关闭边界见[通知合同](../notifications.md#发送记录与恢复)。outbox 只记录投递，不拥有业务状态或恢复权限。
子任务完成仍以完成并进入集成成果为准。其卡片从 History 的 Semantic Attempt 身份与关联 Invocation 汇总开发（包括修复）和验收投入：同轮续接、Output Repair 与 Thread 更换不增加轮数，预算窗口 ordinal 重置不替代累计身份计数。执行时间只累计实际执行段，排除暂停及人工／CI 等待；原有包含 Publication 的全部 Agent 执行总时间保留，分项之和不冒充全部总计或任务历时。模型和推理强度取真实 Invocation／Thread Binding，角色内一致时简洁展示，多组合按参与轮数和执行段汇总，同轮跨配置明确标记参与数不可相加。缺失或裁剪时省略无法证明的累计项，独立可靠字段保留，不补零或猜测。仅统计顶层角色，不推断内部 subagent、Token 或费用；卡片不列逐轮时间表，不增加模型调用或平行统计存储，明细继续由 History 追溯。
_Avoid_: 反向解析翻译文案、通知独立状态机、按发送时个人默认选择语言、语言变化重发

**精简通知**:
面向任务发起人的重要进展通知。有子任务时保留任务创建、每个子任务首次实际开发开始及完成、首次整体验收开始和转入整体修复、正式整体验收通过、独立待批准及最终完成；Parent-only 保留任务创建、首次实际开发开始、完整需求正式验收通过、独立待批准及完成。通过只依据当前有效的正式对应验收结论，不把局部修复或未提升候选当成整体通过。人工 Resume 反馈实际启动、Controller 推进、失败或立即人工待办，已运行只终端提示；自动续接不算人工恢复。失败与对应故障去重。保留人工待办、预算暂停、停止、放弃和合并后待清理。首次节点使用稳定身份，返工、Resume、换线程不重复开始，后续修复和重验保持安静。规则统一适用于新版 Runner，不增加旧策略迁移。Agent 角色在相关消息中保留，阶段使用可理解的工作动作。
任务创建与首次实际开发分别表达；后者要求 Invocation 已报告真实 Thread，排队、准备进程和接受 Resume 不构成启动。History、Status 与通知共用可信 Worker 执行段和完整性判定，累计同轮续接与 Output Repair，排除 Controller 检查、准备及段间等待；Invocation 生命周期起止时间不替代执行段边界。缺结束时间、缺段或关联缺失时显示“耗时记录不完整”，可靠部分累计标为“已记录执行时长”；未知不补零，不以发现中断时间、后继调用起点或任务历时补造结束。模型、推理强度与轮次按独立证据保留。
_Avoid_: 仅成功时通知、隐藏人工待办
