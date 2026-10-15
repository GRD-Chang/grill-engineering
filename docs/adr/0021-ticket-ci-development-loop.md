# Ticket CI 回流共享开发循环与累计额度

<!-- status: accepted -->

Ticket 的准确 CI 失败回到现有 Development–Review 循环，完成后重新排队集成、发布并验证当前 PR/head 的 CI。集成不新增 Review，Ticket PR 最终进入 Run Branch。原需求提供约束，Development 自主调查失败原因和直接回归，程序不要求失败先属于代码可修分类。程序仍负责对象绑定、正式写入、结果回读与资源限制。

失败输入包括准确失败 head、检查结果与可取得的原始日志。pending、unknown 和尚未取得可靠结果是有界等待，不伪造失败或成功。匹配事实不足时等待或明确暂停，Agent 无法自行解决时返回具体人工缺口，保留公开 resume。CI Review、Findings 修改、新线程和人工 message 保留原失败来源。

配置 `ticket_ci_review`（CLI `--ticket-ci-review/--no-ticket-ci-review`）默认 false，开启时所有 CI 修复均审核；删除固定首轮特例。关闭时形成绑定准确原发布授权、失败 Observation 和新 Candidate 的免审发布收据，保留事实而不生成虚假 Acceptance。连续修复只保留上次授权的必要身份，不递归复制收据。此配置不影响初始 Ticket Review、两种 Parent 完整验收或最终人工批准。

`ticket_ci_development_limit`（CLI `--ticket-ci-development-limit`）默认 3，必须为正整数。同一 Ticket 的 CI Development 与 Review Findings 修改使用一份独立累计额度；新失败 head、提交及 PR 更新不重置。只有新语义 Development 工作计费，状态观察不计费，中断后续接同一工作不再收费。已完成但无代码修改的调查仍计费：有剩余额度时记录 `ci_investigation_paused`，显式 resume 启动新的 CI Development 并计费一次，保留原失败对象、证据、来源及其他额度；普通 run 不解除暂停。调查用尽额度时进入相同 CI 额度检查点，resume 仅续期 CI 窗口。耗尽产生 `ci_development_budget_exhausted`，显式 resume 只续期这一窗口；幂等 resume 不重复续期，历史和其他额度保留。Ticket 不再持有外层/内层双预算。两种 Parent 使用同一累计 CI 开发路径，但强制完整 Parent Review，见 [ADR 0023](0023-parent-ci-development-loop.md)。不迁移旧 Run 状态。

沿用 ADR 0019 的并发责任：CI 等待保留唯一交付席位，不占 Agent 名额；进入 CI Development 释放交付席位，之后重新排队，所有真实 Agent 工作仍共享原名额。局部暂停不停止独立 Ticket。status/history 显示累计窗口、Development 用量、审核策略和准确失败对象。

验证沿用公开 run/resume 生命周期、Change Delivery Engine、可控 Agent/GitHub 替身及真实 Git，检查最终 Run Branch、真实免审事实、全轮 Review、未知类别失败、等待、累计额度与续期；实际 Worker Prompt 分别验证中英文、新线程、Findings 和人工续接。真实模型场景仅显式 opt-in。

GitHub 事实沿用 [Actions workflow jobs API](https://docs.github.com/en/rest/actions/workflow-jobs?apiVersion=2022-11-28) 的 `head_sha`、`status` 与日志端点；只接收当前版本已完成 job，取得的原始日志有界保留，截断或不可取得时明确记录。日志端点依赖权限且可能不可用，故缺日志不等于 CI 通过或没有失败；Agent 按已有检查事实判断是否能开展调查。
