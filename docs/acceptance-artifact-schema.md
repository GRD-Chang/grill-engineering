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
        "evidence": { "type": "string", "minLength": 1, "pattern": "\\S" },
        "findings": {
          "type": "array",
          "items": { "type": "string", "minLength": 1, "pattern": "\\S" }
        }
      }
    }
  }
}
```

该形状使用 OpenAI Structured Outputs 支持的 object、array、string、enum；根是 object，所有对象属性均 required，且每个 object 都拒绝额外字段。它刻意不使用 `allOf`、`if`、`then` 或 `else`。

## Deterministic Semantics

JSON Schema 定义形状和非空文本；以下跨字段规则由本地 parser 强制，不解析自由文本措辞：

- `pass`：`findings` 必须为空。
- `fail`：`findings` 必须非空。
- `blocked`：`findings` 必须为空。
- 任一 lane 为 `fail`，Controller 将完整 Artifact 原样交给 Development。
- 没有 `fail` 但至少一个 lane 为 `blocked`，Controller 进入 Human Blocker。
- 三个 lane 均为 `pass`，才构成验收通过。

Finding 是一个非空字符串，内容要求由下文 Reviewer Prompt Contract 指导，程序不匹配固定格式。它表达一个必须处理的问题；任意 Finding 都使所属 lane `fail`。只有同时满足以下条件的问题才进入 Finding：属于当前 Review Boundary；有可复现、可定位的证据；违反明确当前需求或硬性工程合同，或者形成具体风险；保持现状会使当前验收对象不可接受；并且能由当前 Job 修复。明确需求或硬性合同的真实缺陷即使修复很小也仍是 Finding。同一根因的多个表现合并成最合适 lane 中的一条 Finding，并说明直接影响的同族场景。纯主观偏好、未来建议、基线已有问题、已完成 Ticket 的问题和其他不影响当前可接受性的建议不得进入 `findings`；实际遇到且由明确 sibling/follow-on Issue 承接的范围说明，可以用 `Deferred to #N：…` 写入最相关 lane 的 `evidence`。纯维护性建议或可选重构只有确有后续价值时才以 `Non-blocking observation：…` 写入 `evidence`，没有实际后续价值的轻微问题直接省略。两者都不改变 lane 状态或触发 Repair；需要产品决定、权限、凭据或不可替代外部操作时使用 `blocked` evidence。

## Reviewer Prompt Contract

Reviewer 必须调用 `skill:code-review`，独立形成 E2E、Standards、Spec 三个维度并对最终 Artifact 负责。Prompt 不额外要求每个维度对应一个独立 subagent；Reviewer 按当前风险组织检查。所有审查或评价型 subagent 使用 `fork_turns: "none"`，并只接收当前范围、对象身份和中立事实。Harness 不记录、审计或限制内部调用。

以下是内容指导，不是固定字符串或格式校验。Finding 应说明问题、证据、所需修复和复验方式；blocked
evidence 应说明阻塞原因、已尝试的办法及需要的人工操作。每个 lane 的验收证据应说明：

- E2E：实际操作或命令、退出码和结果；
- Standards：审查范围或基线及结论；
- Spec：已核对的验收标准及覆盖结论。

不得因为修复量小而隐瞒一个违反明确需求或硬性合同、且会使当前对象不可接受的问题；纯主观偏好、风格偏好、未来改进、基线问题和其他非阻塞建议不构成 Finding。
