# Agent 协作说明

本页及链接文档中的源码、测试命令以本文件所在目录为工作目录。
安装、接入目标仓库或运行交付时，先读 [Agent 操作指南](docs/agent-guide.md)。
探索或修改代码前，按任务读取 [CONTEXT.md](CONTEXT.md) 与 `docs/adr/` 中的相关决策；领域术语沿用产品上下文。

## 测试

本地默认 `make test` 运行快速回归，并补测本次修改涉及的模块；CI 与最终验收使用 `make test-full` 和 `make typecheck`。
运行测试、编写或重构用例、修改 fixture 或 CI 前，必须阅读[测试指南](docs/agents/testing.md)的对应章节，落实选测、成本控制、环境隔离、验证责任与资源清理要求。

## Prompt 角色合同

新增或修改 Prompt 时，将 Agent 定义为对明确交付负责的员工角色，明确负责对象、本轮完成定义、权威事实、工作边界与唯一交付物。

从被调用 Agent 的局部员工视角书写，只提供完成本轮交付所需的任务事实和相邻边界。
预算、状态迁移、后继阶段等 Controller 内部流程，仅在改变 Agent 本轮判断或动作时写入 Prompt。
新增或修改 Worker Prompt 时，必须阅读并同步 [Prompt 合同](docs/agents/agent-prompts.md)中对应角色与分支的目标合同。
