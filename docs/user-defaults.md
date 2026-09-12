# 个人运行默认配置

`agent-run settings` 统一查看和编辑轮数、调用时限、模型及推理强度。无需自定义时无需创建文件，直接使用内置默认。

唯一文件为 `$XDG_CONFIG_HOME/agent-run/user-defaults.json`，未设置 XDG 时为
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

## 优先级与已有 Run

新 Run 按内置默认、个人默认、本次显式覆盖解析。`run --preset` 明确重选整个预设基线，随后应用本次角色覆盖；不带 preset 时只覆盖对应的个人默认字段。`settings configure --preset` 是文件字段编辑，保留文件中其他显式设置，查询可看到这些覆盖。
创建动作一次读取文件并保存配置，Executor 使用该动作保存的配置；初始配置也随 Run 创建回执保存，以便 Task Control 丢失时只用原事实对账。已有 Run 后续调用和后续获准开启的预算窗口不再读取个人默认。

```bash
agent-run run 228 --ticket-review-rounds 2 --development-model gpt-6-astra
agent-run configure 228 --development-model gpt-6-astra --development-effort low
agent-run resume 228 --ticket-review-rounds 4 --development-deadline 6h
```

`configure <Run>` 仍仅创建该 Run 的新 Profile Revision，影响之后新建的 Thread；已有 Thread、Resume 和 Output Repair 保持绑定。`resume` 只有在预算检查点才接受显式策略覆盖，并以 Run 当前快照补齐未覆盖字段；普通恢复不会更换策略或新增预算。各旧窗口保存自己的策略与用量。按开发轮新 Thread 的个人选项尚未交付，本文件不接受该字段。

## 兼容与错误处理

无新文件时，只读兼容旧 `delivery-policy.json`，查询的 `source` 为 `legacy-delivery-policy`。首次通过任一配置命令保存时，将全部旧有效策略保留到新文件。新文件存在后它是唯一权威，旧文件不再参与解析；查询 `legacy_ignored` 明示这一点，可以自行归档旧文件。旧 `policy show/configure` 命令保留，指向同一新文件，不维护另一套默认。

未知字段、null、非法值及损坏 JSON 均明确报错，诊断含文件或配置项；不静默恢复默认、不覆盖损坏文件。CLI 更新加文件锁，先完整校验再原子替换；并发 CLI 更新保留彼此未修改字段。写入失败恢复原配置并清理临时文件；若底层存储连回滚也拒绝，错误会报告保留的恢复备份。手工编辑请保存完整 JSON，避免与 CLI 同时写入；外部编辑器不参与 CLI 锁。

账号凭据由 `auth` 管理，项目 CI 由仓库管理，Run 状态与 Profile 保留在各自状态目录；这些内容均不合并到个人默认文件。
