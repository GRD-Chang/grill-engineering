# Run 选择与定位

Run 的身份读取由 `run_selection.py` 维护，CLI 负责参数解析、命令准入、Action 附着和结果呈现。
这是一条只读边界：发现候选不会创建受管目录、登记 Locator、修复状态或接受 Action。

| 责任 | 维护位置 |
| --- | --- |
| Repository 对应的受管 checkout 与 state 路径、目录所有权 | `managed_workspace.py`；CLI 上下文推断使用 `workspace_cli.py` |
| 有界索引的格式、登记与准确 Run ID 定位 | `run_locator.py` |
| 索引与持久身份核对、已知受管目录补查、候选诊断、准确状态读取、修改目标的目录绑定 | `run_selection.py` |
| Parent 参数解析、按命令筛选候选、Resume/Mutation 已接受 Action 附着 | `cli.py` |
| Action 准入、Receipt、generation 与执行恢复 | 现有 `run_lifecycle.py`、Task Control 与 Executor |

`status`、`history` 和设置预览的准确 Run 查询使用 `load_exact_run`；准确 Run 的生命周期命令使用同一个
`load_selected_state` 核验文件名与 Run ID、支持的状态合同及显式 Repository。只读查询保留可展示的旧状态，
修改仍要求当前合同。两者不能因共享读取而共享修改权限。

Parent 查询与生命周期命令使用 `selector_records`：读取 Locator，并补查已知受管目录中未登记的 Run。
补查不覆盖已有索引；无关失效记录只有在身份足以证明无关时才能排除。匹配或未知身份的失效记录仍需显式定位，
多个匹配候选不按时间猜测。查询允许显式 state directory；修改必须绑定当前受管 checkout 的准确 state directory。

命令特有规则继续由 CLI 维护：Resume 可附着已接受且正在推进的 Action，Stop/Abandon 在没有 active 候选时
可选择非 active Run。准入后的 Receipt、退出码、握手、环境载体、Runner lease 和 generation 检查不属于定位层，
不能因早期选择成功而省略。
