# Prompt 资源合并清单

中英文采用相同角色及场景划分。实际资源位于 `src/agent_run/resources/{zh,en}/`；个人仅覆盖四份 `methods/*.md` 主体。此清单记录用途与合并去向，不以文件数量衡量 Token、速度或质量。

## 四份完整主体

| 当前主体 | 原有内容去向 | 一份正文可维护的范围 |
| --- | --- | --- |
| `methods/development.md` | `development-common`、`development-initial`，内部 `development-role`、`development-delivery-boundary`、`development-completion` | 开发职责、Skill、按风险验证、单轮审查、交付清理与完成标准 |
| `methods/repair.md` | `development-common`、`development-repair`，内部 `repair-role`、`development-delivery-boundary`、`repair-completion` | 修复职责、根因与直接回归验证、修复审查例外、交付清理与完成标准 |
| `methods/acceptance.md` | `review`，内部 `review-role`、`review-finding-contract` 中的方法指导 | 独立验收职责、E2E、Standards/Spec、证据要求、Finding 边界与完成标准 |
| `methods/publishing.md` | `publication`、内部 `publication-role` | 文案职责、四部分写作方法、证据限制、完成标准 |

主体不含全部动态分支。程序仍选择修复来源、验收对象和调用方式，并附加当前任务、证据、工作区、固定权限及输出合同。个人正文可修改全部角色指导；程序权限、Artifact 校验与交付门禁由代码保持。

## 内部资源

| 合并后的用途 | 原有内容去向 |
| --- | --- |
| `prompt-copy.json` | checkout、任务/背景/完整需求 URL、identity 系列、前次验收标签、修复证据、验收对象及发布验收证据等短标签；键保持普通字符串，不执行模板语言 |
| `internal/requirements-read.md` | requirements-read-order、requirements-read-command、requirements-source-authority 合并为权威需求读取指导 |
| `internal/human-response.md` | human-response-label 与 human-response-boundary 合并；原始人工回复填入正文槽位 |
| `internal/integration-evidence.md` | integration-evidence-label 与 integration-evidence-boundary 合并；当前集成证据填入正文槽位 |
| `internal/publication-fallback.md` | publication-fallback-evidence 与 publication-fallback-boundary 合并；回退证据填入正文槽位，仅文案回退分支使用 |
| `internal/*-resume.md` | 开发、修复、验收、发布分别按完整续接用途组织；原对应 resume-output 并入续接正文，保持当前角色结果格式提醒 |
| `internal/development-review-budget.md`、`review-attempt-budget.md` | 各自预算上下文与原 review-budget-boundary 的适用约束放在一起 |
| `internal/read-only-validation.md`、`publication-boundary.md`、`git.md` | 集中维护验收、发布与开发/修复的真实权限；完整预览包含适用边界 |
| `internal/output.md`、`review-output.md`、`publication-output.md` | 固定结构化结果合同保留独立用途，不由个人正文替换 |
| `internal/*-output-repair.md` | 开发、验收与发布的输出格式修复；仅修复结构化交付，不重新开展语义工作 |
| `internal/probe.md`、`publication-handshake.md` | 安装兼容性探针与文案 schema 握手，继续作为独立调用用途 |
| 其余范围、修复来源、验收对象与基线资源 | 保留现有条件选择，仅发送当前适用内容；没有为合并而把所有分支发送给 Worker |

`messages.json` 继续负责 CLI 等界面文案；`prompt-copy.json` 负责 Worker Prompt 的短标签。新 Run 固定全部所选静态正文和 Prompt 短标签。旧 Run 缺少所需资源时明确不兼容，不补齐状态。旧个人文件的手工处理见[个人配置](../user-defaults.md#个人-markdown-方法)。

## 可读性核对范围

维护时逐份阅读两种语言的四份主体，核对职责、方法、验证要求和完成标准是否独立可理解；再通过 `show-prompts.py` 或 `agent-run prompts preview` 检查开发、五类修复、各验收对象、文案、Resume、输出修复及探针的完整组装。动态权限和输出合同在完整预览核对，主体无需复制另一份隐藏角色指导。资源清单说明合并去向；测试保护公开行为和打包可读性，不镜像目录树或限制文件数。
