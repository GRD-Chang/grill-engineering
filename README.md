# agent-run

`agent-run` 是一个显式启动的本地 Codex 自动交付控制器。维护者以 GitHub Parent Issue
定义交付范围；Controller 依次驱动开发、独立验收、Required Checks、PR 发布和恢复，
最终是否进入默认分支仍由维护者通过 `approve` 决定。

## 从源码安装

v0.1 的唯一规范安装入口是源码目录中的 `./install.sh`。安装器只使用当前目录实际内容，
不会跟随后续源码变化，也不会调用 `sudo`、系统包管理器、`pipx`、daemon、cron 或后台更新组件。
它要求宿主已经提供 CPython 3.11+、`venv`、`pip`、当前 Codex CLI；Git 只用于可选的来源说明，
没有 Git metadata 也可以安装。安装失败会保留旧 Active Runner。

稳定版本建议明确选择 release tag：

```bash
git clone https://github.com/GRD-Chang/grill-engineering.git
cd grill-engineer
git checkout <release-tag>
./install.sh
```

branch、fork 和包含未提交修改的本地目录也使用同一个命令。没有 Git metadata 的源码目录只要
包含有效的 Python build backend，同样可以安装；Git ref、commit 和 dirty 状态只写入有界
Runner Provenance，不授予权限或拒绝生命周期命令。

安装器会把当前源码 non-editable 安装到用户级 Snapshot，执行一次独立的最小 Codex
Structured Outputs Compatibility Check，成功后原子激活 Runner。`agent-run` 入口位于
`~/.local/bin`，并由 `~/.profile` 中唯一、幂等的受管 PATH 块加入新登录 shell；重新打开
登录 shell 后可从任意目标仓库调用：

```bash
cd /path/to/delivery-repository
agent-run run <parent-issue> --repo OWNER/REPO
```

`agent-run` 不要求工具源码目录和目标交付仓库相同。源码修改后，已安装 Snapshot 的行为不变；
要使用新源码，回到所选源码目录再次运行 `./install.sh`。系统只保留当前和紧邻上一个 Snapshot，
相同内容重复安装不会重新 probe 或创建重复 Snapshot：

`run` 会在目标仓库创建和维护受管 Run Branch；该交付状态与 Runner Snapshot 相互独立。

```bash
./install.sh --rollback
./install.sh --uninstall
```

rollback 不重建、不调用 Codex，也不读取或修改 Delivery Run。uninstall 只删除 Runner、入口和
安装器写入的 PATH 块，保留 GitHub App 配置、Run locator、私钥文件以及所有目标仓库的
`.agent-run`；重复卸载安全。若用户替换了 `~/.local/bin/agent-run`，安装器会保留该用户内容并
报告清理未完成。

安装器不是常驻 Manager、Launcher、daemon 或独立包；它只在用户显式执行时运行。它不审查 source
trust，不要求 clean checkout、detached SHA、`origin/main` ancestor 或 promotion audit，也不
提供旧 promotion 兼容入口。规范生命周期入口是安装后得到的 Active Runner；直接从 source 或
editable checkout 运行生产生命周期不受支持，但 Active Runner 不会因 branch、fork、dirty source
或非官方 provenance 被旧 gate 拒绝。

### 首次运行、更新与诊断

源码目录和目标交付仓库是两个不同角色。首次安装完成并重新打开登录 shell 后，从任意目录检查
本机状态，再进入目标仓库运行：

```bash
agent-run doctor
cd /path/to/delivery-repository
agent-run doctor
agent-run run <parent-issue> --repo OWNER/REPO
```

`doctor` 可以从任意目录调用，只读报告 Python 版本、Git、Codex、宿主 `gh` 登录、OpenSSL、Linux
`bubblewrap`、Active Runner、PATH 和当前 Worker read provider。缺少依赖只会显示为问题，不会
安装、修复、触发生命周期或改写任何仓库和用户配置；需要脚本消费时使用 `agent-run doctor --json`。

要重复构建，回到源码目录再次执行 `./install.sh`。安装 B 后 Active 是 B、previous 是 A；再安装
C 后只保留 C/B。相同源码重复执行是幂等的，候选构建或 Compatibility Check 失败会保留原 Active。
需要回退时在源码目录执行 `./install.sh --rollback`；它只交换 Active 与 previous，不调用 Codex，
也不读取或修改 Delivery Run。确认不再使用本机 Runner 后执行 `./install.sh --uninstall`；它保留
固定安装锁、Run locator、App profile、私钥文件和目标仓库 `.agent-run`。卸载后重新打开登录 shell
以取得 PATH 变化，重复卸载安全成功。

## 目标仓库前置条件

运行生命周期命令仍需要目标仓库具备：

- Python 3.11+、已登录的 `codex` 和 `gh` CLI、Git、OpenSSL 与 Linux `bubblewrap`；
- Worker 使用的 GitHub 读取权限：`actions: read`、`checks: read`、`contents: read`、
  `issues: read`、`metadata: read`、`pull_requests: read` 和 `statuses: read`；
- Publisher 使用宿主 `gh` 登录的仓库写权限；
- 与仓库 Ruleset 对应的 Required Check（通常为 `quality`）。

Runner 安装本身不访问 GitHub、目标仓库或 GitHub App；Compatibility Check 只验证当前 Codex
结构化输出链路。完整生命周期、权限边界和恢复语义见
[`docs/agent-run.md`](docs/agent-run.md)。

Worker 默认复用宿主已经登录的 `gh` 进行固定只读请求。需要独立只读身份时，可从任意目录一次性配置
GitHub App；配置只保存元数据和仓库外私钥路径，不复制私钥或 installation token：

```bash
agent-run auth status
agent-run auth app configure \
  --app-id <app-id> \
  --installation-id <installation-id> \
  --private-key /secure/agent-run-app.pem
agent-run auth app remove
```

没有 App profile 时默认使用宿主 `gh`；有效 App profile 存在时明确使用 App provider，损坏或不可读
时不会静默回退。`agent-run auth status` 只显示 provider 和非敏感状态，`auth app remove` 删除
profile 并恢复 host `gh`，不会删除私钥文件。Worker 永远不会看到宿主 token、App 私钥或 Publisher
凭据。

v0.1 支持 Linux/WSL 的用户级 `~/.profile` PATH 管理，需要用户自行提供 CPython 3.11+、`venv`、
`pip`、Codex CLI、Git、OpenSSL、已登录的 `gh` 和 Linux `bubblewrap`。不提供 Windows、macOS、
系统级/多用户安装、PyPI/pipx、常驻 Manager/Launcher 或自动更新。

## 开发

editable 安装仅用于本仓库开发和测试，不是普通用户的安装入口：

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
python -m pytest -q
python -m mypy src/agent_run
```

## 常用命令

```bash
agent-run run <parent-issue> --repo OWNER/REPO
agent-run status [--parent <parent-issue>]
agent-run history [--parent <parent-issue>]
agent-run runs [--repo OWNER/REPO]
agent-run resume <parent-issue> --repo OWNER/REPO
agent-run doctor [--json]
agent-run approve <parent-issue> --repo OWNER/REPO
agent-run revise <parent-issue> --message '维护者反馈' --repo OWNER/REPO
agent-run requeue <parent-issue> --repo OWNER/REPO
```

`run` 是创建或继续 Delivery Run 的唯一普通入口；当前仓库只有一个进行中 Run 时，
`status`、`history` 可省略选择参数，也可以使用 `--parent`；在任意目录用 `--repo` 时必须同时
提供 `--parent`。`runs` 用于发现候选。普通 mutation 和 Run-scoped `configure` 都用 Parent
Issue 定位唯一 Run：零匹配、多匹配或仓库不匹配时拒绝猜测；完整 Run ID 与显式 `--state-dir`
只保留给自动化和精确排障。`resume` 不创建新 Run，`approve` 是进入默认分支前的显式人工批准。
Lifecycle mutation 默认输出不含 Action ID、Run ID、PID 或 digest 的人类回执；需要稳定机器审计事实时显式使用 `--json`。安装、更新、rollback 和
uninstall 都不迁移、修改或绑定既有 Delivery Run；不兼容 state 仍返回
`incompatible_run_state`。
