# Agent 交付核心词汇

按本次工作选择[专题词汇与合同](GLOSSARY-MAP.md)。这里定义跨专题共用的交付对象、执行角色和证据。

## 交付对象

**Delivery Task（交付任务）**

由准确 Repository 与 Parent Issue 标识的稳定远端交付对象。 [对应合同](docs/domain/run-scope.md#仓库与任务)

**Delivery Run（交付运行）**

经维护者授权在本地工作区为一个 Delivery Task 开展的、具有独立身份和集成边界的交付运行。 [对应合同](docs/domain/run-scope.md#仓库与任务)

**Work Subject（工作主体）**

跨 Job Generation 保持稳定、用于审计与当前性核验的交付目标。 [对应合同](docs/domain/run-scope.md#工作主体与当前性)

**Job Generation（任务世代）**

同一 Work Subject 在一组准确需求、基线和候选当前性边界下的一次可执行身份。 [对应合同](docs/domain/run-scope.md#工作主体与当前性)

## 执行与授权

**Controller（控制器）**

读取权威事实、推进交付状态并决定允许执行哪些动作的确定性编排层。 [对应合同](docs/domain/worker.md#角色与执行约束)

**Codex Worker（Codex 工作器）**

按角色进行规划、编辑、验证或验收，并返回结构化 Agent Artifact 的执行者。 [对应合同](docs/domain/worker.md#角色与执行约束)

**Publisher（发布器）**

在 Executor 中持有正式写入权限并执行 Controller 已授权 Git 或 GitHub 操作的模块。 [对应合同](docs/domain/ticket-delivery.md#候选与正式写入)

**Executor Host（执行会话宿主）**

按工作区和 Delivery Task 托管唯一 Run Executor Session 的本机进程边界。 [对应合同](docs/domain/execution-control.md#执行会话与宿主)

**Lifecycle Action（生命周期动作）**

维护者通过命令提交、按身份接受并消费的一次生命周期意图。 [对应合同](docs/domain/execution-control.md#动作与控制对账)

**Semantic Agent Attempt（语义 Agent 尝试）**

一个角色在准确 Work Subject、Job Generation 与当前性边界下的一轮语义工作。 [对应合同](docs/domain/worker.md#attempt-与-invocation)

**Agent Invocation（Agent 调用）**

一次绑定角色、语义 Attempt、Thread 和截止时间的阶段级 Codex 调用。 [对应合同](docs/domain/worker.md#attempt-与-invocation)

## 成果与证据

**Retained Work（保留的当前工作）**

暂停或中断时仍归准确 Work Subject 和 Generation 的工作阶段、成果、证据及继续入口。 [状态归属](docs/agents/state-storage.md) · [调用续接](docs/agents/attempt-lifecycle.md)

**Candidate Commit（候选提交）**

Controller 为一轮工作创建、可按 SHA 核验的不可变代码候选快照。 [对应合同](docs/domain/ticket-delivery.md#候选与正式写入)

**Acceptance Record（验收记录）**

将独立验收结果绑定到准确范围、候选版本和 Reviewer 身份的权威记录。 [对应合同](docs/domain/acceptance.md#验收记录与修复)
