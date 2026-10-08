# Agent 操作指南

[English](agent-guide.en.md) | 简体中文

你是负责安装和运行 `agent-run` 的工程师，为目标仓库维护者完成本次操作。
交付实际执行结果、验证证据、未满足的条件和下一步。仅安装时不启动开发；
要求运行时，跟进到完成或需要人工处理的边界。

遵循用户要求和目标仓库 `AGENTS.md`，以当前 Issue/PR、安装版本的 `--help` 和查询结果为事实依据。
使用与所选源码版本对应的文档，记录工具源码目录、目标 `OWNER/REPO` 和使用的用户仓库目录（如有）。

## 选择入口

| 本次任务 | 执行步骤 |
| --- | --- |
| 首次安装或环境未就绪 | [首次安装](install.md#首次安装)，再做[目标仓库接入](install.md#目标仓库接入) |
| 已安装，接入新仓库 | [目标仓库接入](install.md#目标仓库接入) |
| 启动或跟进任务 | 下方“运行任务”；首次运行前完成安装和接入检查 |
| 更新、回滚或卸载 | [安装生命周期](install.md#更新回滚与卸载) |
| 启用或调整飞书通知 | [飞书通知接入](install.md#飞书通知可选)，再按个人运行配置设置接收人和通知模式 |
| 修改模型、推理强度或额度 | [个人运行配置](user-defaults.md)，核对新任务与已有任务的生效范围 |

只读取本轮分支及其要求的参考，不必通读全部运行规则。

## 运行任务

1. 读取 Parent Issue 的当前正文和评论，确认范围与验收标准。
   有子任务时核验 GitHub 原生 sub-issues 和 `blockedBy`；正文列表不作为任务图。
   无子任务时执行 Parent-only 交付。可领取任务须为 open、有 `ready-for-agent`，
   且没有 `needs-triage`、`needs-info` 或 `ready-for-human`；未解除的 blocker 阻止领取。

2. 在配置好项目环境的终端定位并启动任务，用实际值替换占位符。带 `--repo` 时可从任意目录运行；
   从用户仓库运行时也可省略该参数，由 Runner 只读识别远端：

   ```bash
   agent-run runs --repo OWNER/REPO
   agent-run run <parent-issue> --repo OWNER/REPO
   agent-run status --parent <parent-issue> --repo OWNER/REPO --json
   ```

   Runner 从远端独立克隆代码，不修改用户仓库。仅已推送内容可供 Agent 使用；本地分支、文件和索引由用户自行更新。
   首次创建受管克隆时，只继承启动环境中生效的 Git `user.name` 和 `user.email`，不复制用户仓库的其他本地 Git 配置。
   同一用户在本机对同一 Repository/Parent 的 `run` 创建或附着唯一未完成任务，不因启动目录不同而重复执行；已有人工门禁时按查询指引处理。
   多个候选时核对仓库与 Parent，不按最近时间猜测。命令返回不代表整个任务完成。

3. 用 `status` 获取当前对象和下一步，需要过程证据时读
   `agent-run history --parent <parent-issue> --repo OWNER/REPO --json`。
   按开发或 CI 等待时间安排查询，避免高频空轮询。

完成条件：已确认交付完成，或当前人工边界及所需动作明确。最终报告包含状态、PR（如有）、
实际验收/必需检查结果和待办；只有状态与 GitHub 事实确认最终合并、Issue 收尾及必要清理完成，才称交付完成。

## 人工处理与恢复

需要改变运行状态时，先读[命令边界表](agent-run.md#命令边界与状态轮转)，按当前状态选择动作。
变更命令带实际 Parent 和 `--repo OWNER/REPO`，可从任意目录执行。

| 情况 | 动作 |
| --- | --- |
| 正在执行或等待检查 | 继续观察，无需重复启动 |
| 执行失败、命令失败、未知原因暂停、手动停止或监督超时 | 核验并处理原因后，通过同一 `resume` 继续原工作；可用 `--message` 补充处理事实，失效会话用 `--new-thread` |
| Human Blocker 需要回答 | 展示原问题，取得回答后通过 `resume --message` 原样传递 |
| `recovery_materials_missing` | 按具体字段或 checkout 路径补足可信材料，再从同一 `resume` 入口核验；保留残余状态及成果，不创建新 Run 代替原目标 |
| `requeue_required` | 核对变化后的需求，再执行 `requeue` |
| 最终 PR 待批准 | 展示 PR、验收和检查结果；已有针对该最终交付的明确批准时才执行 `approve` |
| 整体修改意见 | 在允许的状态用 `revise --message` 传递原始反馈 |
| 暂停 | 整个 Run 用 `stop`，单张 Ticket 用 `stop --ticket N` 并在需要时以 `resume --ticket N` 继续；Ctrl-C 或关闭终端只离开观察，后台任务仍运行 |
| 放弃 | 按 `abandon` 边界处理；默认保留未提交工作，强制丢弃须有明确授权 |

预算耗尽、范围变化或状态不兼容时，报告当前所需决策；使用公开 CLI 恢复，
不得编辑持久运行状态绕过门禁。目录与清理边界见[本地状态与清理](agent-run.md#本地状态与清理)。

当前协议身份完整的新 Run 缺损允许人工修复原事实，这与编辑状态绕过门禁不同：先确认没有活跃 Executor，备份残余状态和工作区，再从可信备份恢复诊断所指的字段；不能填空历史、猜测审核或批准，也不能把个人默认配置当作该 Run 的原配置。缺少协议身份的旧 Run 仍不支持迁移。

未完成 Development 或程序集成收口需要保留未提交成果及现场；若该 managed checkout 丢失，诊断提供准确路径、分支、提交及提交对象可用性。已固化 Candidate 后的 Review、Publication、CI 等阶段允许正常清理 checkout，由交付流程按准确提交重建并重新检查既有门禁，不据此判定材料遗失。核对受管仓库中的原分支仍指向该提交后，在该受管仓库内使用 `git worktree add <诊断路径> <原分支>` 补建；如提交缺失，先从可信备份取回对象与准确原分支。不要强制重置已有分支或删除其他工作区。还须恢复有备份的未提交文件及必要集成现场；没有这些材料时继续报告缺口，不声称已恢复丢失成果。若程序已创建提交但状态尚未落盘，仍按对应工作类型的准确父提交、文案、Candidate intent 及 Git 检查对账，不能放行任意前进的 HEAD。

补足后执行 `agent-run resume <parent-issue> --repo OWNER/REPO --message "已补足的事实及处理说明"`，会话不可用时加 `--new-thread`，局部 Ticket 使用 `--ticket N`。入口重新核验原需求、工作身份、范围、基线和成果，后继沿用原工作与额度；旧审核、CI、文案和批准仍须满足各自当前对象门禁。通过 `status`/`history` 确认实际后继 Invocation 已启动，不能仅凭状态改变判断恢复完成。外部写入结果未知时先对账；恢复不补造副作用记录，不接受正式需求或任务图变更。
