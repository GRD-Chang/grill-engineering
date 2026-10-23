# Run 选择与定位

Run 的身份读取由 [`cli.selection`](../../src/agent_run/cli/selection.py) 维护，CLI 负责参数解析和结果呈现，Core 负责命令准入。
这是一条只读边界：发现候选不会创建受管目录、登记 Locator、修复状态或接受 Action。

| 责任 | 维护位置 |
| --- | --- |
| Repository 对应的受管 clone 与 state 路径、目录所有权 | [`adapters.workspace`](../../src/agent_run/adapters/workspace.py) |
| Task → Run ID / 数据库位置记录的读写 | [`storage.tasks`](../../src/agent_run/storage/tasks.py)；替换决定由 Core 作出 |
| 有界受管目录发现、位置记录与数据库身份核对、唯一候选选择 | `cli.selection._candidates` / `_select`，[`queries.run`](../../src/agent_run/queries/run.py) 的 `discover` / `task_published` |
| 查询参数及输出、Parent 或准确 Run ID 的生命周期分流 | [`cli.main`](../../src/agent_run/cli/main.py)、[`cli.lifecycle`](../../src/agent_run/cli/lifecycle.py) |
| Action 准入、Receipt、执行代次与恢复 | [`core.kernel`](../../src/agent_run/core/kernel.py)；生命周期层负责准确 Host 附着和交接 |

`status`、`history` 从当前 v19 数据库读取；准确 Run ID、Repository、Parent、数据库位置及
Task 发布记录须一致。旧 JSON/v6、native v17/v18 不提供兼容查询或恢复。
读取和修改不能因共享定位而共享权限。

候选来自数据主目录下的受管 state 目录和 `native-task-locations/` 的 Task 位置记录，
不遍历用户 clone、工作区或安装目录。位置记录不保存另一份业务状态，查询不补写记录。
Parent 查询优先使用该 Task 已发布的 Run；省略 Parent 时只选择唯一未终止候选。
多个匹配候选不按时间猜测。显式 `--repo` 查询须同时指定 `--parent` 或准确 Run ID，
`runs --repo` 可列出候选。查询允许显式 state directory；生命周期修改还须核验受管 state 目录及准确 Task 绑定。

生命周期位置参数接受 Parent 编号或完整 Run ID；`resume --ticket N` 选择准确局部暂停对象。
附着原 Host 不代表新请求已应用，须读取回执的请求动作及应用事实。
准入后的 Receipt、退出码、握手、环境载体、Runner lease 和 generation 检查不属于定位层，
不能因早期选择成功而省略。
