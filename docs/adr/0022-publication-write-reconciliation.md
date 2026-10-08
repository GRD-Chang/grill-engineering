# 正式写入中断后的对账与发布续接

<!-- status: accepted -->

## 决定

Publish 只准备文案。程序在正式 Git/GitHub 写入前保存准确操作意图，响应或业务结果落盘中断后，
从同一 Job、Semantic Attempt 和 StateStore 继续；不增加发布调度 Agent 或第二份任务状态。

| 操作 | 续接依据 |
| --- | --- |
| Candidate Commit | 原 checkout head、Attempt 和持久化 Candidate intent token；检查真实提交身份后收口 |
| Publication Commit | 写前持久化的准确 SHA、提交内容、验收 tree、parents 与文案；对象已存在则核验并安装同一提交，没有该对象时才写入 |
| 推送 | 准确源/目标分支与持久化 expected/head SHA；远端等于新 head 则收口，仍为原边界则按原授权继续 CAS 推送 |
| PR 创建 | 仓库、源/目标分支及准确 head/base；找到原 PR 则核验并复用，没有准确匹配对象才能继续创建，歧义或关闭对象不创建替代品 |
| PR 文案更新 | 准确 PR 身份及写前、目标文案；新值证明完成，旧值允许续写，其他值保留未知结果 |
| 合并 | 已发送的 merge intent、准确 PR/head/base、实际合入提交/tree 与 Required Checks；Parent 同时保留准确人工批准 |
| 交付收尾 | 已核验的合入结果、程序身份、关闭意图/事件及收尾记录；已完成的评论、关闭与分支删除不重复写入 |

读失败不能当成对象不存在；异常不能清除未知写入意图。远端写入成功后本地 save 失败时，
Final Run 回读最后持久化的发布边界，再记录有界等待，不以已经修改的内存对象覆盖恢复依据。
Change PR 的业务结果保存失败时，恢复保存前的 Job 边界，避免后续异常处理清除未知写入意图。
Change Delivery 在集成、需求失效和 Run Repair 前置观察之前，先只读核验原 PR 写入。
目标分支前进不替代原操作对账：原准确 PR 的旧值或目标值可证明结果，第三种文案或身份、读取缺口
保留原意图及适用席位并暂停。该读回不重发旧写入；收口后才重新集成、准备文案并核验新版本门禁。
无法证明的结果保留准确对象、具体缺口与原恢复入口，不猜测外部结果或认领他人操作。
必要状态或工作区缺失不在本合同中重建。

Final Run 因关闭 PR 或未知写入结果暂停时保存对账续接阶段。外部事实恢复后，公开 `resume`
回到原对账路径，保留意图和文案，并继续当前版本 Required Checks 与人工批准门禁。

## 文案、验收与授权

同一成果及需求、验收、集成边界未变时，复用落盘的 Publish 结果；机械重试、CI 观察及收尾不再调用 Publish。
准确候选、需求版本、Ticket Completion 或集成事实变化时，按已有 currentness 合同重新准备文案和适用验收。
tree 相同不单独证明完整边界有效。新 head 不继承旧 CI 或 Parent 人工批准；合并前继续执行原准确版本门禁。

## 并发与展示

Ticket 的推送/PR 意图尚未核实时，即使进入执行失败、监督超时或发布重试暂停，仍持有原集成席位。
其他 Ticket 可以继续开发，不能抢占该交付边界；对账确定后再按既有 Stop、修复、排队或完成合同让出席位。
普通无未知写入的暂停继续沿用原释放规则。Parent-only 和多 Ticket Parent 共用正式写入责任，
保留各自完整验收和人工批准入口。

发布操作重试耗尽后保留写入意图和文案，显式 `resume` 仅为该对象开启新的有界操作窗口，
记录耗尽窗口历史，不恢复其他额度，也不自动授予新的合并批准。

`status`/`history` 只投影既有事实：Ticket 的 `multi_ticket.tickets[].pending_write` 显示待核实操作及原 head/base，
`holds_integration` 表示实际席位；历史 `github_write_action` 同时覆盖 Change Job 与 Final Run 的写入意图。
查询不运行对账、不写 StateStore、不消费预算。

## 验证与来源

使用真实 Git、StateStore 和可控 GitHub 替身注入写入后/业务落盘前故障；覆盖未执行、已执行及无法核实的结果。
代表性真实 CLI 重启继续覆盖合并及收尾，真实模型测试保持显式 opt-in。
沿用 [追加发布](0014-append-source-publication.md)、[并发与监督](0019-ticket-work-concurrency-and-supervision.md)
及 [测试指南](../agents/testing.md)。

远端事实查询沿用 Git 的 [ls-remote](https://git-scm.com/docs/git-ls-remote) 接口：读取准确 ref 和 commit ID，
成功读取且无匹配 ref 与查询失败必须区分。这里深化已有发布边界，不引入独立通用事务框架。
