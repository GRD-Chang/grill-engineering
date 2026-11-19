# Prompt 维护边界

每种语言只将四份用户方法保存在 `src/agent_run/resources/{zh,en}/methods/`。内部指令、字段标签和场景说明由代码维护；不将每句话映射为一个文件，也不以文件数量衡量 Token、速度或模型质量。

## 按修改目的定位

| 修改目的 | 维护位置与边界 |
| --- | --- |
| 用户可定制的开发、修复、验收、发布方法 | `methods/development.md`、`repair.md`、`acceptance.md`、`publishing.md`；分别包含职责、方法、验证与完成标准 |
| 开发/修复的内部权限、修复来源、续接和输出指导 | `contracts/text_development.py`；相同 Git 约束在开发与修复之间共用 |
| 验收对象、只读验证、证据和输出指导 | `contracts/text_review.py`；保留不同验收对象的实际身份与证据边界 |
| 发布证据、权限、续接和输出指导 | `contracts/text_publication.py`；区分当前通过独立验收和仅有旧审查证据 |
| 需求范围、权威来源、人工回复及探针 | `contracts/text_context.py`；只共享真正相同的责任，不强行统一不同角色的权限 |
| 个人方法读取与首次冻结 | `config/resources.py::resolve_resources` 只由预览、展示及 `config/creation.py::freeze_creation` 等入口调用；入口固定语言和完整资源集合后才进入渲染 |
| 冻结资源读取 | `contracts/prompt.py::resource` 只读取请求快照；`core/prompts/` 不读取个人配置，也不缺省装配资源 |
| 场景选择与当前事实 | `core/prompts/role.py` 为运行与预览提供唯一组装入口，按职责使用 `development.py`、`review.py`、`publication.py`、`context.py` |
| 模型输出结构 | `contracts/artifact.py::role_schema`；通过输出 schema 独立传入，预览另列同一 schema，不在内部文案中重复手写完整 JSON 样例 |
| 输出的业务合法性 | `contracts/artifact.py`；例如结果类别与字段空值、验收状态与 findings 的关系仍由程序校验 |

内部输出指导说明如何陈述真实改动、验证、证据及人工阻塞。schema 不能替代这些语义要求；也不从 schema 自动生成全部自然语言规则。安装探针保持自己的最小 schema 和候选代码边界。

## 双语与动态信息

内部文案以完整用途为单位，将中文与英文相邻定义。两种语言共用场景判断和事实投影；缺少语言、空白正文或占位符不匹配明确失败，不回退另一语言。参数只替换一次，原始证据中的花括号等文本不作为模板再次解释。

用户主体按语言分别编辑。修改角色方法时核对另一语言是否需要同步。CLI、History 与通知的短文案通过 [`contracts.copy.text`](../../src/agent_run/contracts/copy.py) 从所选语言的 `messages.json` 读取。语言选择见[展示事实](display-facts.md)。

程序根据当前场景选择适用文案，注入 Issue URL、工作区、提交身份、原始证据及最新人工回复。需求正文由 Agent 根据 URL 读取。新修复使用完整修复主体。[同次执行续接与输出格式修复](attempt-lifecycle.md#各类续接的边界)只发送各自的短指令。

## 固定内容与验证

新 Run 创建时固定语言、四份主体和所选语言的内部静态内容。之后修改个人方法或升级内置文案不改写已有快照；当前对象、证据和人工回复逐次注入，不保存逐次完整 Prompt 或对话。本次不迁移旧 Run，旧快照缺少所需用途时明确拒绝。

通过 `show-prompts.py` 或 `agent-run prompts preview` 检查实际正文及独立 schema：覆盖三类对象的合法开发/修复/验收/发布、Ticket CI 审核开启与关闭、适用集成、续接、人工回复、格式修复及只读探针展示。`show-prompts.py --language en` 可检查英文方法；原始示例证据不翻译。预览与执行使用同一组装函数，示例不证明真实任务执行或模型遵循合同；发布范围示例使用 Core 同形的 `publication_context`：本次提交范围、完整 PR 范围及已有 PR 原文，
不替代 Reviewer 的唯一增量范围或增加发布权限。

测试验证双语与占位符完整性、适用分支、固定权限及输出语义、个人覆盖和快照稳定；真实安装验证包内主体与代码文案可用。人工逐份阅读中英文关键约束，不能用键集合或关键词命中代替语义审查，也不以目录树镜像测试限制合理重构。
