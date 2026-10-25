# First Ticket / 第一个任务

这是可复制的小项目，不是自动建库工具。仅需 Python 3.11+，不需要 pip 包、服务或秘密。
在此目录运行 `python3 -m unittest discover -v`，基线应有两个通过的测试。
示例 CI 尚未在真实 GitHub 上验收，见 [发行记录](../../docs/productization/release-acceptance.md)。

经操作者授权，在自己的独立仓库中放入 `greeting.py` 和 `test_greeting.py`，
把 `ci.yml.template` 复制为 `.github/workflows/test.yml`，提交并推送。
模板默认分支是 `main`；使用其他默认分支时同时修改两个 branch filter。
将已有 `test` check 按自己的规则设置为 required，并核对准确 workflow/App 绑定。
不要阻断 Runner 更新 `agent-run/**/run` 所需的合法路径；不自动改分支规则或授予 bypass。
没有 required checks 不能记作 CI 验收通过。

## 选择一种首次任务

先从 Parent-only 开始：创建一个开放的 Parent Issue，标记 `ready-for-agent`，不要建立子 Issue。
复制下面需求；没有未解决的 blockers，也不要保留 `needs-triage`、`needs-info`、`ready-for-human`。

> 将 `greeting()` 改为返回 `Hello, world!`。更新原基线断言并增加必要验证；
> 保留返回字符串的合同，不引入依赖。交付包括实现、测试、真实 CI 和当前最终 PR 的人工批准。

需要验证完整 Parent 时，另建一次新任务，仍从原基线开始：

- Parent：增加具名问候和大写问候，原 `greeting()` 无参数继续返回 `Hello`，无外部依赖。
- Ticket A：`greeting(name=None)` 在省略参数时返回 `Hello`，非空姓名返回 `Hello, NAME!`，
  姓名前后空白去除，空白姓名抛出 `ValueError`；增加兼容和边界测试。
- Ticket B：增加 `loud_greeting(name=None)`，返回 `greeting(name).upper()`，
  复用空白姓名错误；增加测试，不复制问候格式化逻辑。

把 A/B 建为 Parent 的 **GitHub 原生 sub-issues**，用原生 `blockedBy` 令 B 依赖 A，
两张 Ticket 标记 `ready-for-agent`。正文复选框不是任务图。Parent 也保持可运行标签状态。
操作者检查实际关系后按 [quickstart](../../docs/productization/quickstart.md) 启动。

Only this example's target repository receives the copied files. Do not copy Runner's source,
private handoff records or account configuration. The workflow is a template, not an active
workflow in the Runner repository. Creating repositories/Issues, changing rules and approving
PRs require the operator's corresponding authorization; nothing here performs those actions.

## CI 修复覆盖

上面的正常任务可能一次通过，不能因此宣称已验证 CI repair。
发行维护者应在隔离的示例验收仓库里预先准备可复现的失败场景，固定测试输入与预期，
确保独立 CI 检查不依赖秘密或基础设施故障，并记录一次真实 failure→repair→fresh checks。
未实际触发时填 `unrun`，不能故意破坏生产仓库、临时放宽 checks、伪造状态，或把手工修复算成 Runner 修复。
现有私有 `tools/real-e2e` 可复用其准备/观察能力；本示例不提供第二套调度或自动批准器。
