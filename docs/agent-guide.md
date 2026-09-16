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
| 可恢复执行失败、手动停止或监督超时 | 排除已知原因后，按状态指引执行 `resume` |
| Human Blocker 需要回答 | 展示原问题，取得回答后通过 `resume --message` 原样传递 |
| `requeue_required` | 核对变化后的需求，再执行 `requeue` |
| 最终 PR 待批准 | 展示 PR、验收和检查结果；已有针对该最终交付的明确批准时才执行 `approve` |
| 整体修改意见 | 在允许的状态用 `revise --message` 传递原始反馈 |
| 暂停 | 使用 `stop`；Ctrl-C 或关闭终端只离开观察，后台任务仍运行 |
| 放弃 | 按 `abandon` 边界处理；默认保留未提交工作，强制丢弃须有明确授权 |

预算耗尽、范围变化或状态不兼容时，报告当前所需决策；使用公开 CLI 恢复，
不得编辑持久运行状态绕过门禁。目录与清理边界见[本地状态与清理](agent-run.md#本地状态与清理)。
