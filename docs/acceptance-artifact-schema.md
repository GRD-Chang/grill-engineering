# Acceptance Artifact Schema

本文记录 Ticket Fresh Acceptance 与 Run Acceptance 共用的目标结构化输出契约。两者使用同一 schema；区别只在 Reviewer Prompt 中的验收范围：Ticket 验收针对当前 Ticket，Run 验收针对累计 diff、跨 Ticket 交互与 Parent Issue。

本文件是 schema 形状和语义校验的设计合同。实现 `acceptance_schema()`、`AcceptanceArtifact.parse()`、对应 Prompt 与测试时必须同时遵守它。

## 迁移边界

这是一次直接替换。实现时删除旧的顶层 `verdict`、`findings`、`human_blockers` schema、parser 分支、Prompt 表述及测试样例；不得保留双 schema、兼容读取或状态迁移。旧结构的持久 Run 记录不进入新 parser；需要继续时从当前 GitHub 权威事实显式创建新的可执行 Run。旧记录不自动删除，以保留事故审计；清理由维护者显式执行。

## Structured Outputs Schema

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["checks"],
  "properties": {
    "checks": {
      "type": "object",
      "additionalProperties": false,
      "required": ["e2e", "standards", "spec"],
      "properties": {
        "e2e": { "$ref": "#/$defs/lane" },
        "standards": { "$ref": "#/$defs/lane" },
        "spec": { "$ref": "#/$defs/lane" }
      }
    }
  },
  "$defs": {
    "lane": {
      "type": "object",
      "additionalProperties": false,
      "required": ["status", "evidence", "findings"],
      "properties": {
        "status": {
          "type": "string",
          "enum": ["pass", "fail", "blocked"]
        },
        "evidence": { "type": "string" },
        "findings": {
          "type": "array",
          "items": { "$ref": "#/$defs/finding" }
        }
      }
    },
    "finding": {
      "type": "object",
      "additionalProperties": false,
      "required": [
        "severity",
        "summary",
        "evidence",
        "required_fix",
        "verification"
      ],
      "properties": {
        "severity": {
          "type": "string",
          "enum": ["critical", "high", "medium", "low"]
        },
        "summary": { "type": "string" },
        "evidence": { "type": "string" },
        "required_fix": { "type": "string" },
        "verification": { "type": "string" }
      }
    }
  }
}
```

该形状使用 OpenAI Structured Outputs 支持的 object、array、string、enum 与 `$defs`/`$ref`；根是 object，所有对象属性均 required，且每个 object 都拒绝额外字段。它刻意不使用 `allOf`、`if`、`then` 或 `else`。

## Deterministic Semantics

JSON Schema 只定义形状；以下跨字段规则由本地 parser 强制：

- `pass`：`findings` 必须为空，`evidence` 必须使用当前 lane 的可复核标记：E2E 为“操作或命令：…；退出码：…；结果：…”，Standards 为“审查范围或基线：…；结论：…”，Spec 为“已核对的验收标准：…；覆盖结论：…”。
- `fail`：`findings` 必须非空。
- `blocked`：`findings` 必须为空；`evidence` 必须写明发生了什么、已尝试什么、以及人必须做什么。
- 任一 lane 为 `fail`，Controller 将完整 Artifact 原样交给 Development。
- 没有 `fail` 但至少一个 lane 为 `blocked`，Controller 进入 Human Blocker。
- 三个 lane 均为 `pass`，才构成验收通过。

Finding 是一个自包含对象：`severity`、`summary`、`evidence`、`required_fix`、`verification` 均为非空字符串；`severity` 仅用于排序，任意严重度的 Finding 均使所属 lane `fail`。同一问题只进入最合适的一个 lane。只有同时处于当前 Review Boundary 内、违反当前需求或造成明确工程风险、有可复核证据、且能由当前 Job 修复的问题才进入 Finding。纯主观偏好、未来建议、基线已有问题、已完成 Ticket 的问题和其他非阻塞建议不得进入 Acceptance Artifact；需要产品决定、权限、凭据或不可替代外部操作时使用 `blocked` evidence。

## Reviewer Prompt Contract

主 Reviewer Agent 自行派发 E2E、Standards、Spec 三条 lane，并汇总其结果；Harness 不记录、审计或限制其内部 subagent 调用。主 Reviewer 不得用自身判断替代缺失 lane。

每个 lane 的 `evidence` 最低应包含：

- E2E：`操作或命令：…；退出码：…；结果：…`；
- Standards：`审查范围或基线：…；结论：…`；
- Spec：`已核对的验收标准：…；覆盖结论：…`。

不得因为问题严重度低而隐瞒一个有证据且当前范围必须修复的问题；纯主观偏好、风格偏好、未来改进、基线问题和其他非阻塞建议不构成 Finding。
