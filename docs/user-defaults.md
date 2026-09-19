# 个人运行默认配置

`agent-run settings` 统一查看和编辑轮数、调用时限、模型、推理强度及开发会话策略。无需自定义时无需创建文件，直接使用内置默认。

运行参数文件为 `$XDG_CONFIG_HOME/agent-run/user-defaults.json`，未设置 XDG 时为
`~/.config/agent-run/user-defaults.json`。文件是 JSON，可只填写需要覆盖的字段；直接编辑后下一次查询或创建 Run 即生效，无需导入或同步。

```bash
agent-run settings show
agent-run settings show --json
agent-run settings configure --ticket-review-rounds 4 --review-deadline 90m
agent-run settings configure --development-model gpt-6-astra --development-effort low --json
agent-run settings show --parent 228
agent-run settings show --run <完整Run-ID> --json
```

查询个人默认返回文件位置、实际来源、稀疏设置、完整解析值及逐字段来源。Run 查询返回保存的策略、有效 Profile Revision 和不可变 Thread bindings，不读取个人默认，不写文件，不查询凭据。支持既有 `--repo` / `--state-dir` 定位规则。
保存结果包含 `changed`、保存后的解析值及 `notice`：**仅影响之后创建的新 Run，已有 Run 保持原设置**。`--json` 成功输出为单个 JSON 对象；失败沿用 CLI 的结构化诊断或参数错误。

## 轮数

| JSON `policy` 字段 / 同名连字符 CLI 选项 | 内置默认 | 范围与含义 |
| --- | --- | --- |
| `ticket_review_rounds` / `--ticket-review-rounds` | 3 | 正整数 N；普通开发最多 N+1，审核最多 N，另有一次有条件的 Final CI-fix |
| `parent_only_paired_rounds` / `--parent-only-paired-rounds` | 10 | 正整数 N；开发与审核各最多 N 次配对 |
| `run_repair_rounds` / `--run-repair-rounds` | 10 | 正整数 N；先审核一次，最多 N 次修复开发，合计最多 N+1 次审核 |

Ticket 最后一轮 Findings 经开发处理后可走确定性 Fallback；Final CI-fix 只处理适用 CI 失败。Parent-only 与 Run 窗口用尽后需要显式恢复，不自动续期。等待、内部子 Agent 和输出格式修复不增加这些业务轮数。

## 开发会话策略

`policy.development_thread_policy`（CLI `--development-thread-policy`）允许 `reuse`（默认，跨开发轮复用）与 `new-per-attempt`（每个新 Development Attempt 新建 Thread），一致作用于 Ticket、Parent-only 和 Run Repair。

```bash
agent-run settings configure --development-thread-policy new-per-attempt
agent-run run 229 --development-thread-policy reuse
```

该字段在创建 Run 时固定；查询个人默认与指定 Run 可看到各自实际值，已有 Run 不开放策略切换，后续预算窗口也保留它。同一轮的执行异常、容量恢复、人工回应续接和 JSON 修复继续原 Thread；启动新一轮的需求修订才受轮换策略影响，显式人工替换 Thread 的例外保留。新 Thread 继续当前 checkout 和已有代码，不重置预算。它绑定该 Run 当前有效的 Profile Revision，不重读个人默认；显式 `configure <Run>` 的 Profile 修订仍只影响后续新 Thread。不承诺必然减少 Token、成本或提高质量。

## 单次调用时限

`policy.invocation_deadlines` 包含 `development`（默认 `5h`）、`review`（`2h`）、`publication`（`1h`）。CLI 分别使用 `--development-deadline`、`--review-deadline`、`--publication-deadline`。
数值单位为秒，也可填带 `s/m/h/d` 的正数字符串，支持小数；必须有限且大于零。解析查询统一显示秒。
同一 Invocation 的初始输出和 Output Repair 共用时限；恢复调用按原有恢复规则获得新的调用时限。这不是全任务总时限，不提供内部轮询或异常重试开关。

## 模型与推理强度

`profile.preset`（CLI `--preset`）允许 `economy`、`premium`，默认 `economy`：

| 预设 | Development | Review | Publication |
| --- | --- | --- | --- |
| economy | gpt-5.6-luna / xhigh | gpt-6-astra / low | 引用 Development |
| premium | gpt-6-astra / low | gpt-6-astra / low | gpt-5.6-luna / xhigh，独立 |

各角色使用 `development_model`、`review_model`、`publication_model` 与对应的
`development_effort`、`review_effort`、`publication_effort`；CLI 将下划线替换为连字符。
model 为非空字符串（去除首尾空格）；effort 允许 `minimal/low/medium/high/xhigh/max/ultra`。
配置阶段遵循既有标识校验，实际模型可用性由执行端验证；模型不可用时明确失败，不换模型或降低推理强度。

`publication_from_development: true`（`--publication-from-development`）让 Publication 跟随 Development 的模型和强度，不能在同一修改中同时指定 Publication 独立值。指定任一 Publication 字段即独立设置，另一个字段采用当前预设基线；恢复引用会删除个人文件内旧的独立字段。省略引用配置遵循预设；`false` 表示不主动要求恢复引用，不是独立模型开关。

[完整 economy 示例](examples/user-defaults.json)覆盖默认轮数、时限和角色引用，可直接解析，但无需复制它才能使用默认值。若需独立 Publication，用以下 `profile` 替换示例的 `profile`：

```json
{
  "preset": "premium",
  "development_model": "gpt-6-astra",
  "development_effort": "low",
  "review_model": "gpt-6-astra",
  "review_effort": "low",
  "publication_model": "gpt-5.6-luna",
  "publication_effort": "xhigh"
}
```

## 飞书进度通知

通知默认关闭。CLI 安装与机器人应用配置按[飞书 CLI 官方教程](https://github.com/larksuite/cli/blob/main/README.zh.md)
完成；本节配置 `agent-run` 使用哪个应用、通知谁，以及发送哪些进展。

### 启用通知

1. 核对已有的 CLI profile 和机器人身份，用实际 profile 替换 `work`：

   ```bash
   lark-cli --profile work whoami --as bot
   ```

   确认返回的 profile 正确、机器人身份可用，记录其 `appId`。
   接收人的 `open_id` 必须属于同一个应用；获取方式见官方教程。`app_id` 是应用标识，不是凭据。

2. 用实际值替换示例中的接收人、profile 和应用标识，保存个人配置：

   ```bash
   agent-run settings configure --notifications --notification-open-id ou_RECIPIENT --notification-profile work --notification-app-id cli_APP_ID
   agent-run settings show
   ```

   核对 `notifications` 中的开关、接收人、profile、应用标识和模式。
   首次启用未指定模式时使用精简模式（`concise`）。应用凭据由飞书 CLI 管理，不写入项目或个人运行配置。

### 选择模式或关闭通知

| 需要 | 命令 |
| --- | --- |
| 后续新任务使用精简模式 | `agent-run settings configure --notification-mode concise` |
| 后续新任务使用详细模式 | `agent-run settings configure --notification-mode detailed` |
| 仅本次新任务使用详细模式 | `agent-run run <parent-issue> --repo OWNER/REPO --notification-mode detailed` |
| 仅本次新任务关闭通知 | `agent-run run <parent-issue> --repo OWNER/REPO --no-notifications` |
| 后续新任务关闭通知 | `agent-run settings configure --no-notifications` |

设置模式也会启用通知，需先完成接收人和应用配置。精简模式保留关键进展与人工待办；
详细模式增加每轮开发、验收和自动修复的过程，具体内容见[飞书进度通知](notifications.md#收到什么)。

### 生效范围与检查

修改个人配置仅影响之后创建的任务。已有任务继续使用创建时的开关、模式、接收人、profile 和应用绑定；
旧任务未保存模式时继续使用详细行为。`--no-notifications` 也不能改变已有任务的设置。
使用 `agent-run settings show --run <完整Run-ID>` 查看已有任务的配置快照。

任务启动后，通过 `agent-run status <完整Run-ID> --json` 或 `agent-run history <完整Run-ID> --json`
查看 `notifications` 中的发送记录和未送达原因。保存配置、身份核对成功不代表消息已经送达。
通知不可用或发送失败不改变开发结果。

应用发生变化时，重新核对接收人的 `open_id` 与 `app_id`，再更新个人配置供新任务使用。
已有任务不支持更换接收人或应用；恢复其通知需要恢复原 profile 对应的应用与凭据。

## 优先级与已有 Run

新 Run 按内置默认、个人默认、本次显式覆盖解析。`run --preset` 明确重选整个预设基线，随后应用本次角色覆盖；不带 preset 时只覆盖对应的个人默认字段。`settings configure --preset` 是文件字段编辑，保留文件中其他显式设置，查询可看到这些覆盖。
创建动作一次读取文件并保存配置，Executor 使用该动作保存的配置；初始配置也随 Run 创建回执保存，以便 Task Control 丢失时只用原事实对账。已有 Run 后续调用和后续获准开启的预算窗口不再读取个人默认。

```bash
agent-run run 228 --ticket-review-rounds 2 --development-model gpt-6-astra
agent-run configure 228 --development-model gpt-6-astra --development-effort low
agent-run resume 228 --ticket-review-rounds 4 --development-deadline 6h
```

`configure <Run>` 仍仅创建该 Run 的新 Profile Revision，影响之后新建的 Thread；已有 Thread、Resume 和 Output Repair 保持绑定。`resume` 只有在预算检查点才接受显式策略覆盖，并以 Run 当前快照补齐未覆盖字段；普通恢复不会更换策略或新增预算。各旧窗口保存自己的策略与用量。开发会话策略不能通过 `resume` 覆盖。

## 兼容与错误处理

无新文件时，只读兼容旧 `delivery-policy.json`，查询的 `source` 为 `legacy-delivery-policy`。首次通过任一配置命令保存时，将全部旧有效策略保留到新文件。新文件存在后它是唯一权威，旧文件不再参与解析；查询 `legacy_ignored` 明示这一点，可以自行归档旧文件。旧 `policy show/configure` 命令保留，指向同一新文件，不维护另一套默认。

策略和模型中的未知字段、null、非法值及文件损坏 JSON 均明确报错，诊断含文件或配置项；不静默恢复默认、不覆盖损坏文件。CLI 更新加文件锁，先完整校验再原子替换；并发 CLI 更新保留彼此未修改字段。写入失败恢复原配置并清理临时文件；若底层存储连回滚也拒绝，错误会报告保留的恢复备份。手工编辑请保存完整 JSON，避免与 CLI 同时写入；外部编辑器不参与 CLI 锁。

账号凭据由 `auth` 管理，项目 CI 由仓库管理，Run 状态与 Profile 保留在各自状态目录；这些内容均不合并到个人默认文件。

发送、恢复与本地记录的边界见[飞书进度通知](notifications.md)。

## 个人 Markdown 方法

`agent-run prompts` 管理当前系统用户跨项目共用的中文方法。个人目录为
`$XDG_CONFIG_HOME/agent-run/prompts/zh`，未设置 XDG 时为
`~/.config/agent-run/prompts/zh`；没有项目级覆盖。

```bash
agent-run prompts init
agent-run prompts diff
agent-run prompts diff --json
```

初始化一次生成五份完整 Markdown：

| 文件 | 适用工作 |
| --- | --- |
| `development-common.md` | 所有开发与修复共用方法 |
| `development-initial.md` | 初次开发，与共用方法组合 |
| `development-repair.md` | 各类修复，与共用方法组合 |
| `review.md` | 子任务、完整需求与整体验收 |
| `publication.md` | 交付说明与最终发布说明 |

重复初始化只补缺失文件，保留已有正文。可自由编辑标题与排版，程序按文件职责选择方法，不解析 Markdown 标题。缺少个人文件时回退内置默认；存在但无法读取时明确失败。用户方法不改变 Worker 的权限、任务范围和结构化输出接口，内部续接、输出格式修复与探针由程序资源维护。

升级会保留个人文件；**定制副本不会自动继承默认正文更新**。`diff` 只读比较当前内置正文与个人生效正文，不合并、不改写文件。无差异时返回成功，JSON 中每份文件对应空字符串。

### 预览实际 Prompt

准备角色请求 JSON，例如 `request.json`：

```json
{"acceptance_scope":"ticket","task_issue_url":"https://github.com/OWNER/REPO/issues/2","parent_issue_url":"https://github.com/OWNER/REPO/issues/1"}
```

```bash
agent-run prompts preview --role development --request request.json
agent-run prompts preview --role review --request request.json --json
agent-run prompts preview --role development --request request.json --continuation
```

角色可选 `development`、`review`、`publication`、`final-publication`、`output-repair`。输出格式修复需要 `output_name`（`Development result`、`Acceptance Artifact` 或 `Publication Artifact`）和 `contract_error` 字符串，不接受 `--continuation`。请求使用真实角色的字段：`repair_source` 指定修复来源，`acceptance_scope` 区分 `ticket`、`parent_only`、`run`，相关证据字段按实际工作提供；最终发布需要 `acceptance_artifact`。`--continuation` 预览同轮续接；请求中的 `_invocation_mode` 可指定 `resume` 或 `new-thread`，遵循真实组装规则。预览和执行共用资源读取与角色组装，不访问 GitHub、不调用模型，也不推进 Run。

没有资源快照的请求使用当前个人方法与内置资源。请求包含 `_prompt_resources` 时使用该固定资源集合，可从 Run 的 `prompt_resources` 字段取得；动态任务和证据仍由请求提供。新 Run 创建时一次固定所有静态资源，此后个人或内置修改只影响新 Run，原 Run 恢复和阶段切换沿用创建时资源。缺少必需资源的旧 Run 按不兼容状态明确失败，不自动补齐或迁移。不保存逐次完整动态 Prompt 或对话历史。
