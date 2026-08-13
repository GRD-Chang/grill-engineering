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
5. 新版必须先通过一次真实 Structured Outputs promotion handshake；API schema rejection 为 failed，认证、网络或 rate limit 只算 inconclusive，均不得启动新的 self-hosting Run；
6. 新版完成 promotion 后，只保留当前版和上一版，清理更旧环境。

必须遵循 `docs/agent-run.md` 的 promotion gate：从干净 detached checkout 的完整 40 位 SHA 安装，
再运行一次 `promotion-handshake` 并保存新的 audit 文件。只有 audit verdict 为 `passed`，才允许在
专用干净 clone 中启动 Run。

在首次 `run` 前必须完成并保存真实 promotion handshake 的脱敏审计记录。该记录要包含 Runner SHA、
Codex CLI 版本、Publication schema SHA256、时间、凭据脱敏结果及 `passed` / `failed` /
`inconclusive` verdict；它不能保存 token、私钥、Prompt、完整 stdout 或 transcript。完整的 immutable
SHA pin、`promotion-handshake` 命令、Output Repair、Resume 与 Requeue 操作边界见
[`docs/agent-run.md`](docs/agent-run.md)。

目标仓库应通过 GitHub Ruleset 将 `quality` 设为 Required Check，并覆盖默认分支及
Delivery Run 使用的 Run Branch。没有 Required Checks 时，`agent-run` 会按设计继续，
不能把这种状态误认为已有托管 CI 门禁。当前版本适用的 Ruleset 条件与兼容性约束见
[`docs/agent-run.md`](docs/agent-run.md#自托管开发)。

`bubblewrap` 只保护权威 Git metadata 和 Publisher 凭据，并不是抵抗恶意进程、宿主污染
或数据外泄的通用安全沙箱。只应在受信任的 Issue、代码和主机环境中运行。
