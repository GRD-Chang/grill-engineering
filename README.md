# agent-run

`agent-run` 是一个显式启动的本地 Codex 自动交付控制器。维护者以 GitHub Parent Issue
定义交付范围；Controller 依次驱动开发、独立验收、Required Checks、PR 发布和恢复，
最终是否进入默认分支仍由维护者通过 `approve` 决定。

完整状态机、权限边界和恢复语义见
[`docs/agent-run.md`](docs/agent-run.md)。

## 前置条件

- Python 3.11 或更高版本；
- 已登录的 `codex` 和 `gh` CLI；
- Git、OpenSSL 和 Linux `bubblewrap`；
- 安装在目标仓库上的专用 GitHub App，权限严格限定为
  `metadata: read`、`issues: read`、`pull_requests: read`；
- Publisher 使用的宿主 `gh` 登录具有目标仓库写权限。

GitHub App 的私钥必须保存在仓库外。运行前注入：

```bash
export AGENT_RUN_GITHUB_APP_ID="<app-id>"
export AGENT_RUN_GITHUB_APP_INSTALLATION_ID="<installation-id>"
export AGENT_RUN_GITHUB_APP_PRIVATE_KEY="$(</secure/agent-run-app.pem)"
```

Controller 会为每个 Worker 创建短期只读 installation token；不要把 Publisher token
复用为 Worker token。

## 开发

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
python -m pytest -q
python -m mypy src/agent_run
```

CI 在最低支持版本 Python 3.11 上执行同一组测试和类型检查，Required Check 名称为
`quality`。

## 使用

在需要交付的目标 Git 仓库中运行：

```bash
agent-run run <parent-issue> --repo OWNER/REPO
```

`run` 会创建或恢复同一 Parent Issue 的未完成 Delivery Run，自动推进到 Required
Checks、结构变化、Human Blocker 或最终人工批准等边界。使用返回的 `run_id` 查看状态：

```bash
agent-run status <run-id>
agent-run history <run-id>
```

检查最终 Run PR 后，只有下面的命令会把结果合入默认分支：

```bash
agent-run approve <run-id> --repo OWNER/REPO
```

## 用 agent-run 开发自身

自托管时必须让控制器版本与被开发版本分离：

1. 从已验证的 commit 创建一个非 editable、按 commit SHA 命名的独立 Python 环境；
2. 从专用干净 clone 启动 Delivery Run，不使用日常脏工作区；
3. 整个 Run 始终使用同一个 Runner 环境；
4. Run 完成并合入 `main` 后，才从新 commit 创建下一版 Runner；
5. 新版完成一次完整验证后，只保留当前版和上一版，清理更旧环境。

示例：

```bash
# 先确认来源 checkout 干净，并记录完整 commit SHA。
git -C /path/to/clean/grill-engineer status --short
git -C /path/to/clean/grill-engineer rev-parse HEAD

# <commit-sha> 必须逐字使用上一条命令输出的 SHA。
python -m venv ~/.local/share/agent-run/runners/<commit-sha>
~/.local/share/agent-run/runners/<commit-sha>/bin/python \
  -m pip install /path/to/clean/grill-engineer

# agent-run 从当前目录发现本地仓库；启动和后续操作都要在该干净 clone 中执行。
cd /path/to/clean/grill-engineer
~/.local/share/agent-run/runners/<commit-sha>/bin/agent-run \
  run <parent-issue> --repo GRD-Chang/grill-engineer
```

目标仓库应通过 GitHub Ruleset 将 `quality` 设为 Required Check，并覆盖默认分支及
Delivery Run 使用的 Run Branch。没有 Required Checks 时，`agent-run` 会按设计继续，
不能把这种状态误认为已有托管 CI 门禁。当前版本适用的 Ruleset 条件与兼容性约束见
[`docs/agent-run.md`](docs/agent-run.md#自托管开发)。

`bubblewrap` 只保护权威 Git metadata 和 Publisher 凭据，并不是抵抗恶意进程、宿主污染
或数据外泄的通用安全沙箱。只应在受信任的 Issue、代码和主机环境中运行。
