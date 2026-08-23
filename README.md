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
agent-run start <parent-issue> --repo OWNER/REPO
agent-run run <parent-issue> --repo OWNER/REPO
agent-run status <run-id>
agent-run history <run-id>
agent-run approve <run-id> --repo OWNER/REPO
```

`start` 只创建或恢复本地 Delivery Run；`run` 推进正常 Job Loop；`status`、`history` 可从已登记
的任意目录读取状态；`approve` 是进入默认分支前的显式人工批准。安装、更新、rollback 和
uninstall 都不迁移、修改或绑定既有 Delivery Run；不兼容 state 仍返回
`incompatible_run_state`。
