# Run 选择与定位

Run 的身份读取由 [`cli.selection`](../../src/agent_run/cli/selection.py) 维护，CLI 负责参数解析和结果呈现，Core 负责命令准入。
这是一条只读边界：发现候选不会创建受管目录、登记 Locator、修复状态或接受 Action。

| 责任 | 维护位置 |
| --- | --- |
| Repository 对应的受管 clone 与 state 路径、目录所有权 | [`adapters.workspace`](../../src/agent_run/adapters/workspace.py) |
| Task、历史Run及current绑定的唯一关系 | [`storage.tasks`](../../src/agent_run/storage/tasks.py)、[`storage.routing`](../../src/agent_run/storage/routing.py)；创建与替换由Core同事务决定 |
| 有界受管仓库注册目录发现、准确命名空间及唯一候选选择 | `managed_repository_routes` / `repository_candidates` / `select_repository`；只读Task/Run元数据 |
| 查询参数及输出、Parent或准确Run的生命周期分流 | [`cli.main`](../../src/agent_run/cli/main.py)、[`cli.lifecycle`](../../src/agent_run/cli/lifecycle.py) |
| Action、Receipt、代次、恢复和冻结配置 | [`core.kernel`](../../src/agent_run/core/kernel.py)；Host/exit-hook必须携带准确Run |

源码候选只接受 `independent-delivery-repository-v22`；旧协议拒绝保留，不迁移。
发现只枚举有界受管仓库注册目录，不遍历clone、Run产物目录或安装快照，不读取JSON locator。
每仓库同一数据库按Task/current选择；准确Run ID可读取历史Run，Parent选择当前绑定。
多匹配不按时间猜测。显式 `--repo` 查询仍须同时指定Parent或准确Run，`runs --repo`可列举。
查询可显式指定数据库文件或状态目录；它们只读schema内命名空间，不初始化缺失状态。
修改入口仍核验该仓库的规范数据库路径、准确Task/current、Action和原执行权限。
JSON显示正文不参与选择；数据库丢失或损坏时不能从退休locator捏造已知Run事实。
单库物理损坏会影响同库所有Task，逻辑隔离不等于独立物理数据库的容错。

生命周期位置参数接受 Parent 编号或完整 Run ID；`resume --ticket N` 选择准确局部暂停对象。
附着原 Host 不代表新请求已应用，须读取回执的请求动作及应用事实。
准入后的 Receipt、退出码、握手、环境载体、Runner lease 和 generation 检查不属于定位层，
不能因早期选择成功而省略。
